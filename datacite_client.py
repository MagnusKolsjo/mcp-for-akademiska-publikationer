# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klientmodul för DataCite REST-API:et.

DataCite är registreringsbyrån för DOI:er på forskningsdata, programvara,
preprints och andra forskningsutfall. Modulen kapslar in HTTP-anropen mot
api.datacite.org och formar om JSON:API-svaren till kompakta dictar.

Sökträffarna normaliseras till samma form som crossref_client använder, så att
discovery-hubbens enade sökning kan slå ihop träffar från flera DOI-källor.

Modulen är självständig från MCP — går att importera och använda i ett vanligt
skript för dataladdning eller felsökning.

API-referens:
  https://support.datacite.org/docs/api
  https://support.datacite.org/docs/api-queries
"""

import os
import re

import requests

BASE_URL = os.environ.get("DATACITE_BASE_URL", "https://api.datacite.org").rstrip("/")
USER_AGENT = os.environ.get(
    "DATACITE_USER_AGENT",
    "Discovery-MCP/0.1 (MCP-server mot DataCite)",
)
TIMEOUT = float(os.environ.get("DATACITE_TIMEOUT", "30"))

# Källetiketten som följer med varje normaliserad träff.
KALLA = "datacite"


class DataCiteFel(Exception):
    """Fel vid anrop mot DataCite-API:et."""


_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})


# ---------------------------------------------------------------------------
# Lågnivå-HTTP
# ---------------------------------------------------------------------------

def _hamta_json(url: str, params=None) -> dict:
    """Hämtar och tolkar ett JSON-svar. Kastar DataCiteFel vid problem."""
    try:
        svar = _session.get(url, params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise DataCiteFel(f"Kunde inte nå DataCite ({url}): {exc}") from exc

    if svar.status_code != 200:
        # DataCite returnerar fel som JSON:API-objekt med en "errors"-lista.
        detalj = svar.text[:300]
        try:
            fel = svar.json().get("errors")
            if isinstance(fel, list) and fel:
                detalj = "; ".join(e.get("title", "") for e in fel) or detalj
        except ValueError:
            pass
        raise DataCiteFel(f"DataCite svarade {svar.status_code}: {detalj}")

    try:
        return svar.json()
    except ValueError as exc:
        raise DataCiteFel(f"DataCite gav ett svar som inte är JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# Id-hantering
# ---------------------------------------------------------------------------

# DataCite-DOI:er har samma form som Crossrefs men ofta andra prefix.
_DOI_MONSTER = re.compile(r"(10\.\d{4,9}/\S+)", re.IGNORECASE)


def normalisera_doi(doi: str) -> str:
    """Plockar ut den nakna DOI:n ur en identifierare, URL eller doi:-sträng."""
    if not doi or not doi.strip():
        raise DataCiteFel("Tom DOI angavs.")
    traff = _DOI_MONSTER.search(doi.strip())
    if not traff:
        raise DataCiteFel(f"Hittar ingen giltig DOI i '{doi}'.")
    return traff.group(1).rstrip(".")


# ---------------------------------------------------------------------------
# Resultatformning
# ---------------------------------------------------------------------------

def _titel(attr: dict):
    """Tar fram huvudtiteln ur titles-listan."""
    for t in attr.get("titles", []) or []:
        if isinstance(t, dict) and t.get("title"):
            return t["title"]
    return None


def _forfattare(attr: dict) -> list:
    """Formar creators-listan till {namn, fornamn, efternamn}."""
    personer = []
    for c in attr.get("creators", []) or []:
        if not isinstance(c, dict):
            continue
        fornamn = c.get("givenName")
        efternamn = c.get("familyName")
        namn = c.get("name") or " ".join(p for p in (fornamn, efternamn) if p)
        if namn:
            personer.append({"namn": namn, "fornamn": fornamn, "efternamn": efternamn})
    return personer


def _utgivare(attr: dict):
    """Utgivaren kan vara en sträng eller ett objekt med name beroende på schema."""
    utg = attr.get("publisher")
    if isinstance(utg, dict):
        return utg.get("name")
    return utg


def _typ(attr: dict):
    """Den generella resurstypen, t.ex. Dataset, Text, Software, Image."""
    typer = attr.get("types")
    if isinstance(typer, dict):
        return typer.get("resourceTypeGeneral")
    return None


def _container_titel(attr: dict):
    """Container är t.ex. tidskrift eller datacentral; ofta tom för data."""
    container = attr.get("container")
    if isinstance(container, dict):
        return container.get("title")
    return None


def _forma_traff(item: dict) -> dict:
    """Formar en DataCite-post till den normaliserade, källöverskridande formen."""
    attr = item.get("attributes", {})
    doi = attr.get("doi") or item.get("id")
    return {
        "kalla": KALLA,
        "doi": doi,
        "titel": _titel(attr),
        "forfattare": _forfattare(attr),
        "ar": attr.get("publicationYear"),
        "typ": _typ(attr),
        "utgivare": _utgivare(attr),
        "container": _container_titel(attr),
        "url": attr.get("url") or (f"https://doi.org/{doi}" if doi else None),
        "citeringar": attr.get("citationCount"),
    }


def _avkoda_sammanfattning(attr: dict):
    """Plockar ut den första Abstract-beskrivningen som ren text."""
    beskrivningar = attr.get("descriptions", []) or []
    for d in beskrivningar:
        if isinstance(d, dict) and d.get("descriptionType") == "Abstract" and d.get("description"):
            return d["description"].strip()
    # Faller tillbaka på första beskrivningen oavsett typ.
    for d in beskrivningar:
        if isinstance(d, dict) and d.get("description"):
            return d["description"].strip()
    return None


def _forma_full(item: dict) -> dict:
    """Formar en post med fler fält för enskild hämtning (hamta med format='full')."""
    attr = item.get("attributes", {})
    full = _forma_traff(item)
    full.update({
        "sammanfattning": _avkoda_sammanfattning(attr),
        "version": attr.get("version"),
        "sprak": attr.get("language"),
        "amnen": [s.get("subject") for s in attr.get("subjects", []) or [] if isinstance(s, dict)],
        "format": attr.get("formats"),
        "storlekar": attr.get("sizes"),
        "rattigheter": [r.get("rights") for r in attr.get("rightsList", []) or [] if isinstance(r, dict)],
        "registrerad": attr.get("registered"),
        "referenser_antal": attr.get("referenceCount"),
        "nedladdningar": attr.get("downloadCount"),
    })
    return full


# ---------------------------------------------------------------------------
# Frågebyggare
# ---------------------------------------------------------------------------

def _bygg_query(
    q: str | None,
    fran_ar: int | None,
    till_ar: int | None,
    utgivare: str | None,
) -> str | None:
    """Bygger en Lucene-fråga av fritext och fältvillkor med AND mellan delarna."""
    delar: list[str] = []
    if q:
        delar.append(f"({q})")
    if fran_ar or till_ar:
        lag = int(fran_ar) if fran_ar else "*"
        hog = int(till_ar) if till_ar else "*"
        delar.append(f"publicationYear:[{lag} TO {hog}]")
    if utgivare:
        delar.append(f'publisher:"{utgivare}"')
    return " AND ".join(delar) if delar else None


# ---------------------------------------------------------------------------
# Publika operationer
# ---------------------------------------------------------------------------

def sok(
    q: str | None = None,
    *,
    limit: int = 20,
    sida: int = 1,
    sort: str | None = None,
    typ: str | None = None,
    fran_ar: int | None = None,
    till_ar: int | None = None,
    utgivare: str | None = None,
    klient_id: str | None = None,
    filter: dict | None = None,
) -> dict:
    """Söker i DataCite via /dois och returnerar normaliserade träffar.

    q          - fritextfråga (Lucene-syntax, t.ex. `climate AND ocean`).
    limit      - antal träffar per sida (1-1000, standard 20).
    sida       - sidnummer (1-baserat). DataCite paginerar i sidor, inte offset.
    sort       - sorteringsfält, t.ex. "relevance", "created", "-created"
                 (nyast först), "published", "-published". Standard = relevans.
    typ        - begränsa till en generell resurstyp; anges gement, t.ex.
                 "dataset", "text", "software", "image", "collection".
    fran_ar    - tidigaste publikationsår (inklusive).
    till_ar    - senaste publikationsår (inklusive).
    utgivare   - exakt utgivarnamn att filtrera på.
    klient_id  - begränsa till ett DataCite-repositorium (klient-id), t.ex.
                 "snd.figshare" eller "bl.imperial".
    filter     - dict med råa DataCite-frågeparametrar för avancerade behov,
                 t.ex. {"affiliation": "true", "provider-id": "snsd"}.

    Returnerar antal träffar totalt och en lista med normaliserade träffar.
    Varje träff har en doi som kan matas vidare till hamta().
    """
    params: list[tuple[str, str]] = []
    query = _bygg_query(q, fran_ar, till_ar, utgivare)
    if query:
        params.append(("query", query))
    if sort:
        params.append(("sort", sort))
    if typ:
        params.append(("resource-type-id", typ.lower()))
    if klient_id:
        params.append(("client-id", klient_id))
    params.append(("page[size]", str(max(1, min(limit, 1000)))))
    params.append(("page[number]", str(max(1, sida))))
    for nyckel, varde in (filter or {}).items():
        params.append((nyckel, str(varde)))

    data = _hamta_json(f"{BASE_URL}/dois", params=params)
    traffar = [_forma_traff(it) for it in data.get("data", [])]

    return {
        "kalla": KALLA,
        "totalt": data.get("meta", {}).get("total"),
        "sida": sida,
        "antal": len(traffar),
        "traffar": traffar,
    }


def hamta(doi: str, *, format: str = "kort") -> dict:
    """Läser en post i DataCite via dess DOI.

    doi    - DOI som naken identifierare, doi.org-URL eller doi:-sträng.
    format - "kort" ger den normaliserade sammanfattningen, "full" lägger till
             abstract, version, ämnen, format, rättigheter och statistik.

    Returnerar postdata enligt valt format.
    """
    naken = normalisera_doi(doi)
    data = _hamta_json(f"{BASE_URL}/dois/{naken}")
    item = data.get("data", {})
    return _forma_full(item) if format == "full" else _forma_traff(item)
