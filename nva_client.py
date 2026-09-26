# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klientmodul för NVA (Nasjonalt vitenarkiv), Norges nationella forskningsarkiv.

NVA aggregerar publikationer från norska lärosäten och institutioner.
Sökning: `/search/resources`. En enskild post läses via dess egen `id`-URL
ur ett sökträffs `id`-fält (`/publication/<identifier>`), som svarar med en
omdirigering (303) följd av innehållsförhandling — kräver `Accept:
application/json` och att omdirigeringen följs.

API-referens: https://api.nva.unit.no/swagger (ingen nyckel krävs; NVA ber
om en identifierbar User-Agent med kontaktuppgift).
"""

from __future__ import annotations

import os

import requests

import kallkonfig
from kallhjalp import DiscoveryKallaFel, ny_taktbegransare

BASE_URL = os.environ.get("NVA_BASE_URL", "https://api.nva.unit.no").rstrip("/")
# NVA ber uttryckligen om en User-Agent med kontaktuppgift — bakas in när
# DISCOVERY_KONTAKT_EPOST är satt, precis som Crossrefs polite pool.
USER_AGENT = kallkonfig.user_agent("NVA_USER_AGENT", med_kontakt=True)
TIMEOUT = float(os.environ.get("NVA_TIMEOUT", "30"))

KALLA = "nva"


class NvaFel(DiscoveryKallaFel):
    """Fel vid anrop mot NVA-API:et."""


_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

# Ingen publicerad hastighetsgräns; en försiktig, egen takt.
_vanta = ny_taktbegransare(0.5)


def _hamta(path: str, params: dict | None = None) -> dict:
    _vanta()
    try:
        svar = _session.get(f"{BASE_URL}{path}", params=params, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException as exc:
        raise NvaFel(f"Kunde inte nå NVA ({BASE_URL}{path}): {type(exc).__name__}") from exc
    if svar.status_code != 200:
        raise NvaFel(f"NVA svarade {svar.status_code}: {svar.text[:300]}")
    try:
        return svar.json()
    except ValueError as exc:
        raise NvaFel(f"NVA gav ett svar som inte är JSON: {exc}") from exc


def _forfattare(contributors: list | None) -> list[dict]:
    ut = []
    for c in contributors or []:
        namn = ((c or {}).get("identity") or {}).get("name")
        if namn:
            ut.append({"namn": namn})
    return ut


def _landningssida(post: dict) -> str | None:
    for ident in post.get("additionalIdentifiers", []) or []:
        if ident.get("type") == "HandleIdentifier" and ident.get("value"):
            return ident["value"]
    return post.get("id")


def _oa_lank(post: dict) -> str | None:
    for artefakt in post.get("associatedArtifacts", []) or []:
        if isinstance(artefakt, dict) and artefakt.get("type") in ("PublishedFile", "OpenFile"):
            lank = artefakt.get("downloadUrl")
            if lank:
                return lank
    return None


def _forma(post: dict) -> dict:
    ed = post.get("entityDescription") or {}
    ref = ed.get("reference") or {}
    doi = ref.get("doi")
    if doi and "doi.org/" in doi:
        doi = doi.split("doi.org/")[-1]
    typ = (ref.get("publicationInstance") or {}).get("type")
    ar_raw = (ed.get("publicationDate") or {}).get("year")
    ar = int(ar_raw) if ar_raw and str(ar_raw).isdigit() else None

    return {
        "kalla": KALLA,
        "kalla_id": post.get("identifier"),
        "doi": doi,
        "titel": ed.get("mainTitle"),
        "forfattare": _forfattare(ed.get("contributors") or ed.get("contributorsPreview")),
        "ar": ar,
        "typ": typ,
        "url": _landningssida(post),
        "oa_lank": _oa_lank(post),
    }


def sok(
    q: str | None = None,
    *,
    limit: int = 20,
    fran_ar: int | None = None,
    till_ar: int | None = None,
) -> dict:
    """Söker NVA via /search/resources.

    q              - fritextfråga.
    limit          - max antal träffar (1-100, standard 20).
    fran_ar/till_ar- stöds inte av NVA:s enkla söktjänst och ignoreras.
    """
    if not q:
        raise NvaFel("Ange en fritextfråga (q).")
    data = _hamta("/search/resources", {"query": q, "results": str(max(1, min(limit, 100)))})
    poster = data.get("hits", []) or []
    traffar = [_forma(p) for p in poster]
    return {"kalla": KALLA, "totalt": data.get("totalHits"), "antal": len(traffar), "traffar": traffar}


def hamta(identifierare: str) -> dict:
    """Läser en enskild post via dess NVA-identifierare (UUID-formen ur kalla_id)."""
    if not identifierare or not identifierare.strip():
        raise NvaFel("Tomt NVA-id angavs.")
    data = _hamta(f"/publication/{identifierare.strip()}")
    return _forma(data)
