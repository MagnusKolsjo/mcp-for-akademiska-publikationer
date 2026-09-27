# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klientmodul för OpenAlex REST-API:et.

OpenAlex är ett öppet index över vetenskapliga verk med brett ämnestäckning
(samtliga fält, inte bara naturvetenskap) och rik metadata: ämnen (topics),
öppen tillgång och citeringsgrafen. Modulen formar OpenAlex svar till
discoveryhubbens gemensamma träffschema direkt (se providers.py), eftersom
den här källan — till skillnad från Libris/Crossref/DataCite/arXiv — inte
har något eget dedikerat MCP-verktyg.

Kostnad: sedan februari 2026 kostar OpenAlex-anrop pengar (sök 0,001 USD,
lista/filter 0,0001 USD, hämtning gratis; ett dagligt tak, högre med
DISCOVERY_OPENALEX_API_NYCKEL). Svaret har därför alltid ett kostnad_usd-fält.

API-referens: https://docs.openalex.org/
"""

from __future__ import annotations

import os
import time

import requests

import kallkonfig
from kallhjalp import DiscoveryKallaFel, ny_taktbegransare

BASE_URL = os.environ.get("OPENALEX_BASE_URL", "https://api.openalex.org").rstrip("/")
USER_AGENT = kallkonfig.user_agent("OPENALEX_USER_AGENT", med_kontakt=True)
TIMEOUT = float(os.environ.get("OPENALEX_TIMEOUT", "30"))
API_NYCKEL = kallkonfig.api_nyckel("openalex")

KALLA = "openalex"

# De fält som faktiskt används av _forma(); select= håller svaren små och
# sänker inte kvaliteten på normaliseringen.
_SELECT = (
    "id,doi,title,display_name,publication_year,type,authorships,"
    "primary_location,open_access,cited_by_count,abstract_inverted_index"
)


class OpenAlexFel(DiscoveryKallaFel):
    """Fel vid anrop mot OpenAlex-API:et."""


_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

# OpenAlex tål betydligt mer, men discovery_sok kan fråga flera källor
# parallellt från flera trådar — en försiktig, jämn takt håller kostnaden
# (och risken för att träffa det dagliga taket) förutsägbar.
_vanta = ny_taktbegransare(0.15)


# Längsta väntan vid 429 innan ett omförsök ges upp.
_MAX_VANTA_VID_429_S = float(os.environ.get("OPENALEX_MAX_VANTA_VID_429", "5"))


def _get(path: str, params: dict, headers: dict | None) -> requests.Response:
    try:
        return _session.get(f"{BASE_URL}{path}", params=params, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise OpenAlexFel(f"Kunde inte nå OpenAlex ({BASE_URL}{path}): {exc}") from exc


def _retry_after(svar: requests.Response) -> float | None:
    """Väntetid i sekunder ur Retry-After-headern eller JSON-fältet retryAfter."""
    varde = svar.headers.get("Retry-After")
    if varde is None:
        try:
            varde = svar.json().get("retryAfter")
        except ValueError:
            return None
    try:
        return max(0.0, float(varde))
    except (TypeError, ValueError):
        return None


def _hamta(path: str, params: dict) -> dict:
    """GET mot OpenAlex. Kastar OpenAlexFel vid problem."""
    _vanta()
    alla = dict(params)
    # Nyckeln skickas i headern, inte i adressen: en adress med nyckel hamnar
    # annars i felmeddelanden, loggar och proxyloggar.
    headers = {"Authorization": f"Bearer {API_NYCKEL}"} if API_NYCKEL else None
    if kallkonfig.KONTAKT_EPOST:
        alla.setdefault("mailto", kallkonfig.KONTAKT_EPOST)
    svar = _get(path, alla, headers)
    if svar.status_code == 429:
        vanta_s = _retry_after(svar)
        # Ett kort omförsök ryms inom discovery_soks tidsgräns per källa; ett
        # långt gör det inte, och då är ett tydligt fel mer användbart än
        # att hela källan tidsgränsas utan förklaring.
        if vanta_s is not None and vanta_s <= _MAX_VANTA_VID_429_S:
            time.sleep(vanta_s)
            svar = _get(path, alla, headers)
    if svar.status_code == 429:
        tips = "" if API_NYCKEL else (
            " Utan nyckel delar anonyma anrop en hårt belastad kvot — sätt "
            "DISCOVERY_OPENALEX_API_NYCKEL (gratis) för stabil åtkomst."
        )
        raise OpenAlexFel(f"OpenAlex avvisade anropet (429, för hög belastning).{tips}")

    if svar.status_code == 404:
        raise OpenAlexFel(f"OpenAlex har ingen post för {path}.")
    if svar.status_code != 200:
        # OpenAlex svarar ibland med en HTML-sida; den säger inget för användaren.
        detalj = "" if svar.text.lstrip().startswith("<") else f": {svar.text[:300]}"
        raise OpenAlexFel(f"OpenAlex svarade {svar.status_code}{detalj}")
    try:
        return svar.json()
    except ValueError as exc:
        raise OpenAlexFel(f"OpenAlex gav ett svar som inte är JSON: {exc}") from exc


def _kort_id(openalex_id: str | None) -> str | None:
    """https://openalex.org/W123 -> W123."""
    if not openalex_id:
        return None
    return openalex_id.rstrip("/").split("/")[-1]


def _bar_doi(doi: str | None) -> str | None:
    """https://doi.org/10.x/y -> 10.x/y."""
    if not doi:
        return None
    return doi.removeprefix("https://doi.org/").removeprefix("http://doi.org/")


def _forfattare(authorships: list | None) -> list[dict]:
    ut = []
    for a in authorships or []:
        namn = ((a or {}).get("author") or {}).get("display_name")
        if namn:
            ut.append({"namn": namn})
    return ut


def _oa_lank(open_access: dict, primary_location: dict) -> str | None:
    return (open_access or {}).get("oa_url") or (primary_location or {}).get("pdf_url")


def _abstract(inverterat: dict | None) -> str | None:
    """Återskapar abstractet ur OpenAlex inverterade index {ord: [positioner]}.

    OpenAlex lämnar av upphovsrättsskäl aldrig abstractet som löptext, bara
    som index; ordföljden återskapas exakt ur positionerna."""
    if not inverterat:
        return None
    ord_pa_plats: dict[int, str] = {}
    for ord_, platser in inverterat.items():
        for plats in platser:
            ord_pa_plats[plats] = ord_
    return " ".join(ord_pa_plats[i] for i in sorted(ord_pa_plats)) or None


def _forma(item: dict) -> dict:
    """Formar ett OpenAlex-verk till discoveryhubbens gemensamma träffschema."""
    primary = item.get("primary_location") or {}
    oa = item.get("open_access") or {}
    doi = _bar_doi(item.get("doi"))
    return {
        "kalla": KALLA,
        "kalla_id": _kort_id(item.get("id")),
        "doi": doi,
        "titel": item.get("title") or item.get("display_name"),
        "forfattare": _forfattare(item.get("authorships")),
        "ar": item.get("publication_year"),
        "typ": item.get("type"),
        "url": primary.get("landing_page_url") or (f"https://doi.org/{doi}" if doi else None),
        "oa_lank": _oa_lank(oa, primary),
        "citeringar": item.get("cited_by_count"),
        "sammanfattning": _abstract(item.get("abstract_inverted_index")),
    }


def _bygg_filter(
    oppen_tillgang: bool | None,
    land: str | None,
    filter: dict | None,
) -> str | None:
    delar: list[str] = []
    if oppen_tillgang is not None:
        delar.append(f"open_access.is_oa:{'true' if oppen_tillgang else 'false'}")
    if land:
        delar.append(f"authorships.institutions.country_code:{land}")
    for nyckel, varde in (filter or {}).items():
        delar.append(f"{nyckel}:{varde}")
    return ",".join(delar) if delar else None


def sok(
    q: str | None = None,
    *,
    limit: int = 20,
    fran_ar: int | None = None,
    till_ar: int | None = None,
    oppen_tillgang: bool | None = None,
    land: str | None = None,
    filter: dict | None = None,
) -> dict:
    """Söker OpenAlex works och returnerar redan normaliserade träffar.

    q              - fritextfråga (fulltextsökning).
    limit          - max antal träffar (1-200, standard 20).
    fran_ar/till_ar- publiceringsårsintervall (inklusive).
    oppen_tillgang - True/False för att bara visa öppen/stängd tillgång.
    land           - ISO landskod för författarnas institutioner, t.ex. "SE".
    filter         - råa OpenAlex-filter, t.ex. {"primary_topic.field.id": "...",
                     "topics.id": "...", "authorships.institutions.country_code": "SE"}.
                     Slås ihop med oppen_tillgang/land.
    """
    params: dict = {"per_page": str(max(1, min(limit, 200))), "select": _SELECT}
    if q:
        params["search"] = q
    if fran_ar:
        params["from_publication_date"] = f"{int(fran_ar):04d}-01-01"
    if till_ar:
        params["to_publication_date"] = f"{int(till_ar):04d}-12-31"
    filterstrang = _bygg_filter(oppen_tillgang, land, filter)
    if filterstrang:
        params["filter"] = filterstrang

    data = _hamta("/works", params)
    traffar = [_forma(it) for it in data.get("results", [])]
    meta = data.get("meta", {})

    return {
        "kalla": KALLA,
        "totalt": meta.get("count"),
        "antal": len(traffar),
        "kostnad_usd": meta.get("cost_usd"),
        "traffar": traffar,
    }


def hamta(id_eller_doi: str) -> dict:
    """Läser ett enskilt verk via OpenAlex-id (Wxxxx) eller DOI."""
    ident = (id_eller_doi or "").strip()
    if not ident:
        raise OpenAlexFel("Tomt id/DOI angavs.")

    if ident.lower().startswith("10.") or "doi.org" in ident.lower() or ident.lower().startswith("doi:"):
        naken = ident.split("doi.org/")[-1]
        naken = naken[4:] if naken.lower().startswith("doi:") else naken
        path = f"/works/doi:{naken}"
    else:
        path = f"/works/{_kort_id(ident) or ident}"

    data = _hamta(path, {"select": _SELECT})
    if "error" in data:
        raise OpenAlexFel(f"OpenAlex: {data.get('message') or data['error']}")
    return _forma(data)


def citeringar(id_eller_doi: str, *, riktning: str = "citerande", limit: int = 20) -> dict:
    """Citeringsgrafen runt ett verk.

    riktning - "citerande" (verk som citerar detta, filter cites:) eller
               "referenser" (verk detta citerar, filter cited_by:).
    OpenAlex citeringsfilter kräver ett OpenAlex-id, inte en DOI — en DOI
    slås därför först upp till sitt OpenAlex-id via hamta().
    """
    if riktning not in ("citerande", "referenser"):
        raise OpenAlexFel("riktning måste vara 'citerande' eller 'referenser'.")

    ident = (id_eller_doi or "").strip()
    if ident.lower().startswith("10.") or "doi.org" in ident.lower():
        post = hamta(ident)
        openalex_id = post["kalla_id"]
    else:
        openalex_id = _kort_id(ident) or ident

    filternamn = "cites" if riktning == "citerande" else "cited_by"
    params = {
        "filter": f"{filternamn}:{openalex_id}",
        "per_page": str(max(1, min(limit, 200))),
        "select": _SELECT,
    }
    data = _hamta("/works", params)
    traffar = [_forma(it) for it in data.get("results", [])]
    meta = data.get("meta", {})

    return {
        "kalla": KALLA,
        "id": openalex_id,
        "riktning": riktning,
        "totalt": meta.get("count"),
        "antal": len(traffar),
        "kostnad_usd": meta.get("cost_usd"),
        "traffar": traffar,
    }
