# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klientmodul för CORE REST-API:et (v3).

CORE aggregerar fulltext och metadata från open access-arkiv och
tidskrifter världen över. Fri nivå: icke-kommersiell användning, ~5
sökningar/10 sekunder utan nyckel, en nyckel (DISCOVERY_CORE_API_NYCKEL,
gratis registrering) ger högre kvot. Källan är avstängd som standard
(DISCOVERY_CORE_AKTIV=false) — slå på uttryckligen i .env.

API-referens: https://api.core.ac.uk/docs/v3
"""

from __future__ import annotations

import os

import requests

import kallkonfig
from kallhjalp import DiscoveryKallaFel, ny_taktbegransare

BASE_URL = os.environ.get("CORE_BASE_URL", "https://api.core.ac.uk/v3").rstrip("/")
USER_AGENT = kallkonfig.user_agent("CORE_USER_AGENT")
TIMEOUT = float(os.environ.get("CORE_TIMEOUT", "30"))
API_NYCKEL = kallkonfig.api_nyckel("core")

KALLA = "core"


class CoreFel(DiscoveryKallaFel):
    """Fel vid anrop mot CORE-API:et."""


_session = requests.Session()
_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
if API_NYCKEL:
    _headers["Authorization"] = f"Bearer {API_NYCKEL}"
_session.headers.update(_headers)

# ~5 sökningar/10 s utan nyckel enligt fri nivå — 2 s mellan starter håller
# god marginal. Med nyckel: dubbelt så snabbt (fortfarande försiktigt).
_vanta = ny_taktbegransare(1.0 if API_NYCKEL else 2.0)


def _hamta(path: str, params: dict) -> dict:
    _vanta()
    try:
        svar = _session.get(f"{BASE_URL}{path}", params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise CoreFel(f"Kunde inte nå CORE ({BASE_URL}{path}): {type(exc).__name__}") from exc
    if svar.status_code == 429:
        raise CoreFel(
            "CORE avvisade anropet (429, för hög trafik). Fri nivå tillåter "
            "~5 sökningar/10 sekunder utan nyckel — sätt "
            "DISCOVERY_CORE_API_NYCKEL för en högre kvot."
        )
    if svar.status_code == 404:
        raise CoreFel("CORE hittar inte den angivna posten.")
    if svar.status_code != 200:
        raise CoreFel(f"CORE svarade {svar.status_code}: {svar.text[:300]}")
    try:
        return svar.json()
    except ValueError as exc:
        raise CoreFel(f"CORE gav ett svar som inte är JSON: {exc}") from exc


def _forma(post: dict) -> dict:
    ar = post.get("yearPublished")
    oa_lank = post.get("downloadUrl") or next(iter(post.get("sourceFulltextUrls") or []), None)
    return {
        "kalla": KALLA,
        "kalla_id": str(post.get("id")) if post.get("id") is not None else None,
        "doi": post.get("doi"),
        "titel": post.get("title"),
        "forfattare": [{"namn": a["name"]} for a in post.get("authors", []) or [] if a.get("name")],
        "ar": int(ar) if ar else None,
        "typ": post.get("documentType"),
        "url": oa_lank or (f"https://doi.org/{post['doi']}" if post.get("doi") else None),
        "oa_lank": oa_lank or None,
        "citeringar": post.get("citationCount"),
    }


def sok(
    q: str | None = None,
    *,
    limit: int = 20,
    fran_ar: int | None = None,
    till_ar: int | None = None,
) -> dict:
    """Söker CORE.

    q              - fritextfråga (CORE:s egen frågesyntax stöds, t.ex.
                     `title:"..."` eller `yearPublished:2020`).
    limit          - max antal träffar (1-100, standard 20).
    fran_ar/till_ar- läggs till i frågan som ett yearPublished-intervall.
    """
    if not q:
        raise CoreFel("Ange en fritextfråga (q).")
    fraga = q
    if fran_ar and till_ar:
        fraga = f"({q}) AND yearPublished>={int(fran_ar)} AND yearPublished<={int(till_ar)}"
    elif fran_ar:
        fraga = f"({q}) AND yearPublished>={int(fran_ar)}"
    elif till_ar:
        fraga = f"({q}) AND yearPublished<={int(till_ar)}"

    data = _hamta("/search/works/", {"q": fraga, "limit": str(max(1, min(limit, 100)))})
    traffar = [_forma(p) for p in data.get("results", [])]
    return {"kalla": KALLA, "totalt": data.get("totalHits"), "antal": len(traffar), "traffar": traffar}


def hamta(core_id: str) -> dict:
    """Läser en enskild post via dess CORE-id."""
    if not core_id or not core_id.strip():
        raise CoreFel("Tomt CORE-id angavs.")
    data = _hamta(f"/works/{core_id.strip()}", {})
    if not data:
        raise CoreFel(f"Hittar ingen post med id '{core_id}' hos CORE.")
    return _forma(data)
