# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klientmodul för OSF Preprints (Open Science Framework).

OSF Preprints samlar ett stort antal ämnesspecifika preprint-servrar under
samma API — bl.a. SocArXiv (samhällsvetenskap), LawArXiv (juridik),
EdArXiv (utbildningsvetenskap), PsyArXiv och många fler.

Sökningen går mot SHARE (share.osf.io/trove), den sökmotor som osf.io:s
egen söksida använder. Den söker fritext i titel, abstract, ämnen och
författare över samtliga OSF-preprintservrar i ett anrop. OSF:s REST-API
har ingen fritextsökning — bara filter[title][icontains], en exakt
delsträng mot titeln och en leverantör per anrop — och används därför bara
för att läsa en enskild post (hamta).

REST-API:et delar utan token en gräns på 100 anrop/timme (≈ ett var 36:e
sekund); med DISCOVERY_OSF_API_NYCKEL (ett Personal Access Token) är
gränsen betydligt högre. SHARE omfattas inte av den gränsen.

API-referens: https://developer.osf.io/ och https://share.osf.io/trove/docs
"""

from __future__ import annotations

import datetime
import os

import requests

import kallkonfig
from kallhjalp import DiscoveryKallaFel, ny_taktbegransare

BASE_URL = os.environ.get("OSF_BASE_URL", "https://api.osf.io/v2").rstrip("/")
SOK_URL = os.environ.get("OSF_SOK_URL", "https://share.osf.io/trove/index-card-search")
USER_AGENT = kallkonfig.user_agent("OSF_USER_AGENT")
TIMEOUT = float(os.environ.get("OSF_TIMEOUT", "30"))
API_NYCKEL = kallkonfig.api_nyckel("osf")

KALLA = "osf"

# SHARE saknar intervallfilter för datum; ett årsintervall skickas som en
# lista år (any-of). Taket håller adressen rimligt kort.
_MAX_AR_I_FILTER = 60


class OsfFel(DiscoveryKallaFel):
    """Fel vid anrop mot OSF-API:et."""


_session = requests.Session()
_headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.api+json"}
if API_NYCKEL:
    _headers["Authorization"] = f"Bearer {API_NYCKEL}"
_session.headers.update(_headers)

_sok_session = requests.Session()
_sok_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/vnd.api+json"})

# Utan nyckel: 100/timme delat av alla anropare, dvs ~36 s mellan anrop för
# att hålla god marginal. Med nyckel: klart högre kvot, 1 s räcker gott.
_vanta = ny_taktbegransare(1.0 if API_NYCKEL else 36.0)


def _hamta(path: str, params: dict) -> dict:
    _vanta()
    try:
        svar = _session.get(f"{BASE_URL}{path}", params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise OsfFel(f"Kunde inte nå OSF ({BASE_URL}{path}): {type(exc).__name__}") from exc
    if svar.status_code == 429:
        raise OsfFel(
            "OSF avvisade anropet (429, för hög trafik). Utan "
            "DISCOVERY_OSF_API_NYCKEL delas en gräns på 100 anrop/timme av "
            "alla anropare — sätt en nyckel (Personal Access Token) för en "
            "egen, högre kvot."
        )
    if svar.status_code == 404:
        raise OsfFel("OSF hittar inte den angivna posten.")
    if svar.status_code != 200:
        raise OsfFel(f"OSF svarade {svar.status_code}: {svar.text[:300]}")
    try:
        return svar.json()
    except ValueError as exc:
        raise OsfFel(f"OSF gav ett svar som inte är JSON: {exc}") from exc


def _bar_doi(item: dict) -> str | None:
    attr = item.get("attributes", {})
    if attr.get("doi"):
        return attr["doi"]
    lank = (item.get("links") or {}).get("preprint_doi")
    if lank and "doi.org/" in lank:
        return lank.split("doi.org/")[-1]
    return None


def _forma(item: dict) -> dict:
    attr = item.get("attributes", {})
    doi = _bar_doi(item)
    lankar = item.get("links") or {}
    datum = attr.get("date_published") or attr.get("date_created") or ""
    ar = int(datum[:4]) if datum[:4].isdigit() else None

    return {
        "kalla": KALLA,
        "kalla_id": item.get("id"),
        "doi": doi,
        "titel": attr.get("title"),
        # OSF:s bidragsgivarnamn kräver ett separat anrop per träff
        # (embed=bibliographic_contributors ger bara relationer, inga namn
        # utan ytterligare ett nästlat embed) — utelämnas för att hålla
        # anropen få, i linje med källans egen, hårda hastighetsgräns.
        "forfattare": [],
        "ar": ar,
        "typ": "preprint",
        "url": lankar.get("html") or (f"https://doi.org/{doi}" if doi else None),
        "oa_lank": lankar.get("html"),
    }


def _ar_lista(fran_ar: int | None, till_ar: int | None) -> str | None:
    """SHARE-filtret för dateCreated tar enskilda år, kommaseparerade."""
    if not fran_ar and not till_ar:
        return None
    hog = int(till_ar) if till_ar else datetime.date.today().year
    lag = int(fran_ar) if fran_ar else hog - _MAX_AR_I_FILTER + 1
    lag = max(lag, hog - _MAX_AR_I_FILTER + 1)
    if lag > hog:
        return None
    return ",".join(str(ar) for ar in range(lag, hog + 1))


def _forsta_varde(poster: list | None, nyckel: str = "@value") -> str | None:
    for post in poster or []:
        if isinstance(post, dict) and post.get(nyckel):
            return post[nyckel]
    return None


def _forma_kort(kort: dict) -> dict:
    """Formar ett SHARE index-card till discoveryhubbens träffschema."""
    attr = kort.get("attributes") or {}
    meta = attr.get("resourceMetadata") or {}
    identifierare = attr.get("resourceIdentifier") or []
    doi = next((i.split("doi.org/", 1)[1] for i in identifierare if "doi.org/" in i), None)
    osf_url = next((i for i in identifierare if i.startswith("https://osf.io/")), None) or meta.get("@id")
    datum = _forsta_varde(meta.get("dateCreated")) or ""
    forfattare = [
        {"namn": _forsta_varde(c.get("name"))}
        for c in meta.get("creator") or []
        if isinstance(c, dict) and _forsta_varde(c.get("name"))
    ]
    return {
        "kalla": KALLA,
        "kalla_id": osf_url.rstrip("/").split("/")[-1] if osf_url else None,
        "doi": doi,
        "titel": _forsta_varde(meta.get("title")),
        "forfattare": forfattare,
        "ar": int(datum[:4]) if datum[:4].isdigit() else None,
        "typ": "preprint",
        "url": osf_url,
        "oa_lank": osf_url,
        "sammanfattning": _forsta_varde(meta.get("description")),
        "leverantor": (_forsta_varde(meta.get("publisher"), "@id") or "").rstrip("/").split("/")[-1] or None,
    }


def sok(
    q: str | None = None,
    *,
    limit: int = 20,
    fran_ar: int | None = None,
    till_ar: int | None = None,
    leverantor: str | None = None,
) -> dict:
    """Söker OSF Preprints via SHARE (fritext i titel, abstract m.m.).

    q              - fritextfråga.
    limit          - max antal träffar (1-100, standard 20).
    fran_ar/till_ar- skapandeårsintervall (inklusive).
    leverantor     - begränsa till en OSF-preprintserver, t.ex. "lawarxiv"
                     eller "socarxiv". Utelämnad = alla.
    """
    if not q:
        raise OsfFel("Ange en fritextfråga (q).")
    params = {
        "cardSearchText": q,
        "cardSearchFilter[resourceType]": "Preprint",
        "page[size]": str(max(1, min(limit, 100))),
    }
    ar = _ar_lista(fran_ar, till_ar)
    if ar:
        params["cardSearchFilter[dateCreated]"] = ar
    if leverantor:
        params["cardSearchFilter[publisher]"] = f"https://osf.io/preprints/{leverantor.strip().lower()}"

    try:
        svar = _sok_session.get(SOK_URL, params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise OsfFel(f"Kunde inte nå OSF:s sökning ({SOK_URL}): {type(exc).__name__}") from exc
    if svar.status_code != 200:
        raise OsfFel(f"OSF:s sökning svarade {svar.status_code}: {svar.text[:300]}")
    try:
        data = svar.json()
    except ValueError as exc:
        raise OsfFel(f"OSF:s sökning gav ett svar som inte är JSON: {exc}") from exc

    # Träffarna ligger som index-card under "included", i den ordning
    # search-result-posterna pekar ut; ordningen bevaras via relationerna.
    kort = {i["id"]: i for i in data.get("included", []) if i.get("type") == "index-card"}
    ordning = [
        ((r.get("relationships") or {}).get("indexCard") or {}).get("data", {}).get("id")
        for r in data.get("included", []) if r.get("type") == "search-result"
    ]
    ordnade = [kort[i] for i in ordning if i in kort] or list(kort.values())
    traffar = [_forma_kort(k) for k in ordnade][: int(params["page[size]"])]
    totalt = ((data.get("data") or {}).get("attributes") or {}).get("totalResultCount")
    if isinstance(totalt, dict):  # "trove:ten-thousands-and-more" o.d.
        totalt = None
    return {"kalla": KALLA, "totalt": totalt, "antal": len(traffar), "traffar": traffar}


def hamta(preprint_id: str) -> dict:
    """Läser en enskild preprint via dess OSF-id (t.ex. "8jmu2_v3")."""
    if not preprint_id or not preprint_id.strip():
        raise OsfFel("Tomt OSF-id angavs.")
    data = _hamta(f"/preprints/{preprint_id.strip()}/", {})
    item = data.get("data")
    if not item:
        raise OsfFel(f"Hittar ingen OSF-preprint med id '{preprint_id}'.")
    return _forma(item)
