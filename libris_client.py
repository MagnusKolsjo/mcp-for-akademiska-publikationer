# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klientmodul för Libris XL-API:et.

Modulen kapslar in HTTP-anropen mot Libris sök- och läs-API samt mot
id.kb.se (KB:s länkade auktoritetsdata) och formar om de verbosa
JSON-LD-svaren till kompakta Python-dictar som MCP-verktygen returnerar.

Den är självständig från MCP — går att importera och använda i ett
vanligt skript för dataladdning eller felsökning.

API-referens:
  https://libris.kb.se/api/docs/reference/find/      (sök)
  https://libris.kb.se/api/docs/reference/crud_get/  (läs en post)
"""

import os
import re

import requests

from kallhjalp import kapa_text, ren_text

# Libris uppmanar uttryckligen anropare att skicka en beskrivande
# User-Agent. Den identifierar trafiken för KB och kan kontaktas vid
# problem — sätt en egen via miljövariabeln i drift.
BASE_URL = os.environ.get("LIBRIS_BASE_URL", "https://libris.kb.se").rstrip("/")
ID_BASE_URL = os.environ.get("LIBRIS_ID_BASE_URL", "https://id.kb.se").rstrip("/")
USER_AGENT = os.environ.get(
    "LIBRIS_USER_AGENT",
    "Libris-MCP/0.1 (MCP-server mot Libris)",
)
TIMEOUT = float(os.environ.get("LIBRIS_TIMEOUT", "30"))


class LibrisFel(Exception):
    """Fel vid anrop mot Libris- eller id.kb.se-API:et."""


_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/ld+json"})


# ---------------------------------------------------------------------------
# Lågnivå-HTTP
# ---------------------------------------------------------------------------

def _hamta_jsonld(url: str, params=None) -> dict:
    """Hämtar och tolkar ett JSON-LD-svar. Kastar LibrisFel vid problem."""
    try:
        svar = _session.get(url, params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise LibrisFel(f"Kunde inte nå Libris ({url}): {exc}") from exc

    if svar.status_code != 200:
        # Libris returnerar ofta ett JSON-objekt med "message" vid fel.
        detalj = svar.text[:300]
        try:
            detalj = svar.json().get("message", detalj)
        except ValueError:
            pass
        raise LibrisFel(f"Libris svarade {svar.status_code}: {detalj}")

    try:
        return svar.json()
    except ValueError as exc:
        raise LibrisFel(f"Libris gav ett svar som inte är JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# Id-hantering
# ---------------------------------------------------------------------------

# Ett Libris-post-id är den korta nyckeln i URI:n, t.ex. "l4x7v34x34zz5lq".
# Entiteten (verket/instansen) har suffixet "#it", posten (Record) har det inte.
_ID_MONSTER = re.compile(r"([0-9a-z]{14,17})", re.IGNORECASE)


def normalisera_id(libris_id: str) -> tuple[str, str]:
    """Plockar ut post-id och entitets-URI ur ett godtyckligt Libris-id.

    Tar emot bara nyckeln ("l4x7v34x34zz5lq"), en full URL, eller en URL
    med "#it"- eller "/data.jsonld"-suffix. Returnerar (post_id, entitets_uri)
    där entitets_uri är formen med "#it" som länkningar (t.ex. bestånd) använder.
    """
    if not libris_id or not libris_id.strip():
        raise LibrisFel("Tomt Libris-id angavs.")

    rensad = libris_id.strip()
    traff = _ID_MONSTER.search(rensad.split("?")[0])
    if not traff:
        raise LibrisFel(f"Hittar inget giltigt Libris-id i '{libris_id}'.")

    post_id = traff.group(1)
    return post_id, f"{BASE_URL}/{post_id}#it"


# ---------------------------------------------------------------------------
# Etikett-utvinning ur JSON-LD
# ---------------------------------------------------------------------------

def _etikett(nod) -> str | None:
    """Tar fram en läsbar etikett ur en JSON-LD-nod på bästa möjliga sätt."""
    if nod is None:
        return None
    if isinstance(nod, str):
        return nod
    if isinstance(nod, list):
        delar = [_etikett(d) for d in nod]
        delar = [d for d in delar if d]
        return ", ".join(delar) if delar else None
    if isinstance(nod, dict):
        for nyckel in ("prefLabel", "label", "name", "mainTitle", "value"):
            if nod.get(nyckel):
                return _etikett(nod[nyckel])
        for nyckel in ("prefLabelByLang", "labelByLang", "titleByLang"):
            sprakdict = nod.get(nyckel)
            if isinstance(sprakdict, dict) and sprakdict:
                return sprakdict.get("sv") or next(iter(sprakdict.values()))
        # Personnamn saknar ofta sammanslagen etikett.
        for_namn = nod.get("givenName")
        efter = nod.get("familyName")
        if efter or for_namn:
            return " ".join(p for p in (for_namn, efter) if p)
    return None


def _som_lista(nod) -> list:
    """Normaliserar ett fält till en lista (Libris växlar mellan dict och list)."""
    if nod is None:
        return []
    return nod if isinstance(nod, list) else [nod]


def _id_av(nod):
    """Returnerar @id ur en nod om det finns."""
    return nod.get("@id") if isinstance(nod, dict) else None


# ---------------------------------------------------------------------------
# Resultatformning
# ---------------------------------------------------------------------------

def _forma_traff(item: dict) -> dict:
    """Formar en sökträff (en instans) till en kompakt dict."""
    uri = item.get("@id")
    post_id = normalisera_id(uri)[0] if uri else None

    titel = None
    for t in _som_lista(item.get("hasTitle")):
        if isinstance(t, dict):
            huvud = t.get("mainTitle")
            ovrigt = t.get("subtitle") or t.get("titleRemainder")
            if huvud:
                titel = f"{huvud} : {ovrigt}" if ovrigt else huvud
                break

    verk = item.get("instanceOf") if isinstance(item.get("instanceOf"), dict) else {}

    upphov = []
    for bidrag in _som_lista(verk.get("contribution")):
        if not isinstance(bidrag, dict):
            continue
        namn = _etikett(bidrag.get("agent"))
        roll = _etikett(_som_lista(bidrag.get("role"))[0]) if bidrag.get("role") else None
        if namn:
            upphov.append({"namn": namn, "roll": roll})

    ar = None
    for pub in _som_lista(item.get("publication")):
        if isinstance(pub, dict) and pub.get("year"):
            ar = pub["year"]
            break

    identifierare = []
    for ident in _som_lista(item.get("identifiedBy")):
        if isinstance(ident, dict) and ident.get("value"):
            identifierare.append({"typ": ident.get("@type"), "varde": ident["value"]})

    amnen = []
    for amne in _som_lista(verk.get("subject")):
        etikett = _etikett(amne)
        if etikett:
            amnen.append({"etikett": etikett, "id": _id_av(amne)})

    sprak = [
        s.get("code") for s in _som_lista(verk.get("language"))
        if isinstance(s, dict) and s.get("code")
    ]

    # Sammanfattningen ligger oftast på instansen, ibland på verket.
    sammanfattning = next(
        (ren_text(_etikett(s)) for s in _som_lista(item.get("summary")) + _som_lista(verk.get("summary"))
         if _etikett(s)),
        None,
    )

    return {
        "libris_id": post_id,
        "uri": uri,
        "instans_typ": item.get("@type"),
        "verk_typ": verk.get("@type"),
        "titel": titel,
        "upphov": upphov,
        "ar": ar,
        "sprak": sprak,
        "identifierare": identifierare,
        "amnen": amnen,
        "sammanfattning": sammanfattning,
    }


def sok(
    q: str | None = None,
    *,
    limit: int = 20,
    offset: int = 0,
    sort: str | None = None,
    lankar_till: str | None = None,
    filter: dict | None = None,
) -> dict:
    """Söker i Libris via /find.jsonld och returnerar formade träffar.

    q          - fritextfråga (stödjer +, -, |, *, "" och ()).
    limit      - max antal träffar (Libris standard är 200).
    offset     - antal träffar att hoppa över (paginering).
    sort       - egenskap att sortera på, t.ex. "publication.year" eller
                 "-publication.year" (minus = fallande). Utelämnad = relevans.
    lankar_till- id som träffarna ska länka till (motsvarar API-parametern o).
    filter     - dict med egenskaps-filter, t.ex.
                 {"instanceOf.@type": "Text",
                  "min-publication.year": 1900,
                  "instanceOf.subject.@id": ["url1", "url2"]}.
                 Lista som värde upprepar parametern (ELLER på samma egenskap).
                 Operator-prefix (not-, or-, and-, exists-, min-, max-,
                 minEx-, maxEx-, matches-) anges som del av nyckeln.
    """
    params: list[tuple[str, str]] = []
    if q:
        params.append(("q", q))
    if lankar_till:
        params.append(("o", lankar_till))
    if sort:
        params.append(("_sort", sort))
    params.append(("_limit", str(max(1, min(limit, 200)))))
    params.append(("_offset", str(max(0, offset))))

    for nyckel, varde in (filter or {}).items():
        for v in _som_lista(varde):
            params.append((nyckel, str(v)))

    data = _hamta_jsonld(f"{BASE_URL}/find.jsonld", params=params)
    traffar = [_forma_traff(it) for it in data.get("items", [])]

    return {
        "totalt": data.get("totalItems"),
        "offset": offset,
        "antal": len(traffar),
        "traffar": traffar,
    }


def hamta_post(libris_id: str, *, framed: bool = True, embellished: bool = True) -> dict:
    """Läser en post via /<id>/data.jsonld.

    framed=True ger ett enda nästlat träd (lättare att läsa) i stället för
    den platta @graph-listan. embellished=True (Libris standard) bäddar in
    länkade entiteter; sätt False för enbart postens egna data.
    """
    post_id, _ = normalisera_id(libris_id)
    params = {
        "framed": "true" if framed else "false",
        "embellished": "true" if embellished else "false",
    }
    return _hamta_jsonld(f"{BASE_URL}/{post_id}/data.jsonld", params=params)


def sammanfatta_post(libris_id: str) -> dict:
    """Läser en post och returnerar en kompakt sammanfattning.

    Bygger på samma formning som sökträffar men hämtas från den fullständiga
    posten, vilket ger fler ifyllda fält än sökindexet.
    """
    post_id, uri = normalisera_id(libris_id)
    data = hamta_post(libris_id, framed=True, embellished=True)

    # Med framed=true ligger huvudentiteten under "mainEntity"; faller annars
    # tillbaka på toppnoden.
    nod = data.get("mainEntity") if isinstance(data.get("mainEntity"), dict) else data
    sammanfattning = _forma_traff(nod)
    sammanfattning["libris_id"] = post_id
    sammanfattning["uri"] = uri
    return sammanfattning


def bestand(libris_id: str, *, limit: int = 50) -> dict:
    """Listar vilka bibliotek som har ett verk (Item-poster via itemOf).

    Returnerar bibliotekens sigel (kortkod) och namn. Sigel kan slås upp
    vidare på https://libris.kb.se/library/<sigel>.
    """
    _, uri = normalisera_id(libris_id)
    params = [
        ("itemOf.@id", uri),
        ("@type", "Item"),
        ("_limit", str(max(1, min(limit, 200)))),
    ]
    data = _hamta_jsonld(f"{BASE_URL}/find.jsonld", params=params)

    bibliotek = []
    for item in data.get("items", []):
        innehavare = item.get("heldBy")
        sigel_uri = _id_av(innehavare)
        sigel = sigel_uri.rstrip("/").split("/")[-1] if sigel_uri else None
        bibliotek.append({
            "sigel": sigel,
            "namn": _etikett(innehavare),
            "sigel_uri": sigel_uri,
        })

    return {
        "libris_id": normalisera_id(libris_id)[0],
        "antal_bibliotek": data.get("totalItems"),
        "bibliotek": bibliotek,
    }


def sla_upp_term(q: str, *, typ: str | None = None, limit: int = 10) -> dict:
    """Slår upp länkade termer/entiteter på id.kb.se för precisa filter.

    Används för att översätta fritext till id.kb.se-URI:er som sedan kan
    matas in i sok()-filtret, t.ex. ämnen (sao), genre/form (saogf),
    personer, språk och länder.

    typ - valfri @type-begränsning, t.ex. "Topic" (ämne), "GenreForm",
           "Person", "Language", "Country".
    """
    params: list[tuple[str, str]] = [("q", q), ("_limit", str(max(1, min(limit, 100))))]
    if typ:
        params.append(("@type", typ))

    data = _hamta_jsonld(f"{ID_BASE_URL}/find.jsonld", params=params)

    termer = []
    for item in data.get("items", []):
        termer.append({
            "id": item.get("@id"),
            "typ": item.get("@type"),
            "etikett": _etikett(item),
            "schema": _id_av(item.get("inScheme")),
        })

    return {"totalt": data.get("totalItems"), "antal": len(termer), "termer": termer}
