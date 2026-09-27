# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klientmodul för Crossref REST-API:et.

Crossref är registreringsbyrån för DOI:er på vetenskapliga artiklar, böcker,
konferensbidrag och liknande. Modulen kapslar in HTTP-anropen mot
api.crossref.org och formar om de verbosa svaren till kompakta dictar.

Sökträffarna normaliseras till samma form som datacite_client använder, så att
discovery-hubbens enade sökning kan slå ihop träffar från flera DOI-källor.

Modulen är självständig från MCP — går att importera och använda i ett vanligt
skript för dataladdning eller felsökning.

API-referens:
  https://api.crossref.org/swagger-ui/index.html
  https://www.crossref.org/documentation/retrieve-metadata/rest-api/

Crossref kör en "polite pool" med bättre svarstider för anropare som
identifierar sig med en kontakt-e-post (CROSSREF_MAILTO). Det är frivilligt
men rekommenderas.
"""

import os
import re

import requests

from kallhjalp import kapa_text, ren_text

BASE_URL = os.environ.get("CROSSREF_BASE_URL", "https://api.crossref.org").rstrip("/")
# E-post som lägger anropen i Crossrefs polite pool. Lämnas tom om man inte
# vill identifiera sig — då hamnar trafiken i den anonyma poolen.
MAILTO = os.environ.get("CROSSREF_MAILTO", "").strip()
USER_AGENT = os.environ.get(
    "CROSSREF_USER_AGENT",
    "Discovery-MCP/0.1 (MCP-server mot Crossref)",
)
TIMEOUT = float(os.environ.get("CROSSREF_TIMEOUT", "30"))

# Källetiketten som följer med varje normaliserad träff.
KALLA = "crossref"


class CrossrefFel(Exception):
    """Fel vid anrop mot Crossref-API:et."""


# Crossref läser kontakt-e-posten både ur User-Agent och ur en mailto-parameter.
# Bakar in den i User-Agent när den är satt så att polite pool gäller även för
# anrop som inte uttryckligen skickar mailto-parametern.
_ua = USER_AGENT
if MAILTO and "mailto:" not in _ua:
    _ua = f"{_ua} (mailto:{MAILTO})"

_session = requests.Session()
_session.headers.update({"User-Agent": _ua, "Accept": "application/json"})


# ---------------------------------------------------------------------------
# Lågnivå-HTTP
# ---------------------------------------------------------------------------

def _hamta_json(url: str, params=None) -> dict:
    """Hämtar och tolkar ett JSON-svar. Kastar CrossrefFel vid problem."""
    alla = list(params or [])
    if MAILTO:
        alla.append(("mailto", MAILTO))
    try:
        svar = _session.get(url, params=alla, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise CrossrefFel(f"Kunde inte nå Crossref ({url}): {exc}") from exc

    if svar.status_code != 200:
        # Crossref returnerar ofta en förklarande text eller ett JSON-objekt
        # med "message" vid fel.
        detalj = svar.text[:300]
        try:
            kropp = svar.json()
            meddelanden = kropp.get("message")
            if isinstance(meddelanden, list) and meddelanden:
                detalj = "; ".join(m.get("message", "") for m in meddelanden) or detalj
        except ValueError:
            pass
        raise CrossrefFel(f"Crossref svarade {svar.status_code}: {detalj}")

    try:
        return svar.json()
    except ValueError as exc:
        raise CrossrefFel(f"Crossref gav ett svar som inte är JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# Id-hantering
# ---------------------------------------------------------------------------

# En DOI kan komma som bara identifieraren ("10.2307/40098626"), som doi.org-URL
# eller med "doi:"-prefix. Crossrefs läs-endpoint vill ha den nakna DOI:n.
_DOI_MONSTER = re.compile(r"(10\.\d{4,9}/\S+)", re.IGNORECASE)


def normalisera_doi(doi: str) -> str:
    """Plockar ut den nakna DOI:n ur en identifierare, URL eller doi:-sträng."""
    if not doi or not doi.strip():
        raise CrossrefFel("Tom DOI angavs.")
    traff = _DOI_MONSTER.search(doi.strip())
    if not traff:
        raise CrossrefFel(f"Hittar ingen giltig DOI i '{doi}'.")
    return traff.group(1).rstrip(".")


# ---------------------------------------------------------------------------
# Resultatformning
# ---------------------------------------------------------------------------

def _forsta(varde):
    """Returnerar första elementet om värdet är en lista, annars värdet självt."""
    if isinstance(varde, list):
        return varde[0] if varde else None
    return varde


def _ar(item: dict):
    """Plockar ut utgivningsåret ur den datumvariant som finns."""
    for nyckel in ("published", "published-print", "published-online", "issued", "created"):
        delar = item.get(nyckel, {}).get("date-parts") if isinstance(item.get(nyckel), dict) else None
        if delar and isinstance(delar, list) and delar[0]:
            return delar[0][0]
    return None


def _forfattare(item: dict) -> list:
    """Formar Crossrefs author-lista till {namn, fornamn, efternamn}."""
    personer = []
    for f in item.get("author", []) or []:
        if not isinstance(f, dict):
            continue
        fornamn = f.get("given")
        efternamn = f.get("family") or f.get("name")
        namn = " ".join(p for p in (fornamn, efternamn) if p) or efternamn
        if namn:
            personer.append({"namn": namn, "fornamn": fornamn, "efternamn": efternamn})
    return personer


# Samma gräns som arXiv-klientens kapade sammanfattning.
KORT_SAMMANFATTNING_MAX = 500


def _avkoda_abstract(rad: str | None) -> str | None:
    """Strippar JATS-XML-taggarna ur ett Crossref-abstract till ren text."""
    return ren_text(rad)


def _forma_traff(item: dict) -> dict:
    """Formar en sökträff till den normaliserade, källöverskridande formen."""
    return {
        "kalla": KALLA,
        "doi": item.get("DOI"),
        "titel": _forsta(item.get("title")),
        "forfattare": _forfattare(item),
        "ar": _ar(item),
        "typ": item.get("type"),
        "utgivare": item.get("publisher"),
        "container": _forsta(item.get("container-title")),
        "url": item.get("URL") or (f"https://doi.org/{item['DOI']}" if item.get("DOI") else None),
        "citeringar": item.get("is-referenced-by-count"),
        # Kapad i sökträffar och kort form; format="full" ger hela texten.
        "sammanfattning": kapa_text(_avkoda_abstract(item.get("abstract")), KORT_SAMMANFATTNING_MAX)[0],
    }


def _forma_full(item: dict) -> dict:
    """Formar en post med fler fält för enskild hämtning (hamta med format='full')."""
    full = _forma_traff(item)
    full.update({
        "undertitel": _forsta(item.get("subtitle")),
        "sammanfattning": _avkoda_abstract(item.get("abstract")),
        "volym": item.get("volume"),
        "nummer": item.get("issue"),
        "sidor": item.get("page"),
        "issn": item.get("ISSN"),
        "isbn": item.get("ISBN"),
        "sprak": item.get("language"),
        "amnen": item.get("subject"),
        "referenser_antal": item.get("references-count"),
        "licens": [l.get("URL") for l in item.get("license", []) or [] if isinstance(l, dict)],
    })
    return full


# ---------------------------------------------------------------------------
# Publika operationer
# ---------------------------------------------------------------------------

def sok(
    q: str | None = None,
    *,
    limit: int = 20,
    offset: int = 0,
    sort: str | None = None,
    typ: str | None = None,
    fran_ar: int | None = None,
    till_ar: int | None = None,
    forfattare: str | None = None,
    titel: str | None = None,
    tidskrift: str | None = None,
    filter: dict | None = None,
) -> dict:
    """Söker i Crossref via /works och returnerar normaliserade träffar.

    q          - allmän fritextfråga över all metadata.
    limit      - max antal träffar (1-1000, standard 20).
    offset     - antal träffar att hoppa över (paginering, max 10000).
    sort       - sorteringsfält, t.ex. "published" (utgivningsdatum),
                 "is-referenced-by-count" (citeringar) eller "relevance".
                 Utelämnad = relevans när q är satt, annars Crossrefs standard.
    typ        - begränsa till en publikationstyp, t.ex. "journal-article",
                 "book-chapter", "proceedings-article", "dataset", "posted-content".
    fran_ar    - tidigaste utgivningsår (inklusive).
    till_ar    - senaste utgivningsår (inklusive).
    forfattare - fritext som matchas mot författarfältet (query.author).
    titel      - fritext som matchas mot titelfältet (query.title).
    tidskrift  - fritext som matchas mot tidskrift/källtitel (query.container-title).
    filter     - dict med råa Crossref-filter för avancerade behov, t.ex.
                 {"has-abstract": "true", "from-online-pub-date": "2020-01-01"}.
                 Slås ihop med de filter som typ/fran_ar/till_ar bygger.

    Returnerar antal träffar totalt och en lista med normaliserade träffar.
    Varje träff har en doi som kan matas vidare till hamta().
    """
    params: list[tuple[str, str]] = []
    if q:
        params.append(("query", q))
    if forfattare:
        params.append(("query.author", forfattare))
    if titel:
        params.append(("query.title", titel))
    if tidskrift:
        params.append(("query.container-title", tidskrift))
    if sort:
        params.append(("sort", sort))
    params.append(("rows", str(max(1, min(limit, 1000)))))
    params.append(("offset", str(max(0, offset))))

    # Crossref-filter anges som en kommaseparerad lista av key:value.
    filterdelar: list[str] = []
    if typ:
        filterdelar.append(f"type:{typ}")
    if fran_ar:
        filterdelar.append(f"from-pub-date:{int(fran_ar)}-01-01")
    if till_ar:
        filterdelar.append(f"until-pub-date:{int(till_ar)}-12-31")
    for nyckel, varde in (filter or {}).items():
        filterdelar.append(f"{nyckel}:{varde}")
    if filterdelar:
        params.append(("filter", ",".join(filterdelar)))

    data = _hamta_json(f"{BASE_URL}/works", params=params)
    melding = data.get("message", {})
    traffar = [_forma_traff(it) for it in melding.get("items", [])]

    return {
        "kalla": KALLA,
        "totalt": melding.get("total-results"),
        "offset": offset,
        "antal": len(traffar),
        "traffar": traffar,
    }


def hamta(doi: str, *, format: str = "kort") -> dict:
    """Läser en post i Crossref via dess DOI.

    doi    - DOI som naken identifierare, doi.org-URL eller doi:-sträng.
    format - "kort" ger den normaliserade sammanfattningen, "full" lägger till
             abstract, volym/nummer/sidor, ISSN/ISBN, ämnen och licens.

    Returnerar postdata enligt valt format.
    """
    naken = normalisera_doi(doi)
    data = _hamta_json(f"{BASE_URL}/works/{naken}")
    item = data.get("message", {})
    return _forma_full(item) if format == "full" else _forma_traff(item)
