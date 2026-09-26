# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klientmodul för Europe PMC REST-API:et.

Europe PMC indexerar biomedicinsk och life science-litteratur (PubMed/
MEDLINE, PubMed Central, patent, kliniska riktlinjer, preprints m.m.).

API-referens: https://europepmc.org/RestfulWebService
"""

from __future__ import annotations

import os

import requests

import kallkonfig
from kallhjalp import DiscoveryKallaFel, ny_taktbegransare

BASE_URL = os.environ.get(
    "EUROPEPMC_BASE_URL", "https://www.ebi.ac.uk/europepmc/webservices/rest"
).rstrip("/")
USER_AGENT = kallkonfig.user_agent("EUROPEPMC_USER_AGENT")
TIMEOUT = float(os.environ.get("EUROPEPMC_TIMEOUT", "30"))

KALLA = "europepmc"


class EuropePmcFel(DiscoveryKallaFel):
    """Fel vid anrop mot Europe PMC-API:et."""


_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

# Ingen publicerad hastighetsgräns; en försiktig, egen takt.
_vanta = ny_taktbegransare(0.5)


def _hamta(params: dict) -> dict:
    _vanta()
    try:
        svar = _session.get(f"{BASE_URL}/search", params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise EuropePmcFel(f"Kunde inte nå Europe PMC ({BASE_URL}): {type(exc).__name__}") from exc
    if svar.status_code != 200:
        raise EuropePmcFel(f"Europe PMC svarade {svar.status_code}: {svar.text[:300]}")
    try:
        return svar.json()
    except ValueError as exc:
        raise EuropePmcFel(f"Europe PMC gav ett svar som inte är JSON: {exc}") from exc


def _forfattare(author_string: str | None) -> list[dict]:
    if not author_string:
        return []
    return [{"namn": n.strip()} for n in author_string.rstrip(".").split(",") if n.strip()]


def _oa_lank(post: dict) -> str | None:
    for lank in ((post.get("fullTextUrlList") or {}).get("fullTextUrl") or []):
        if isinstance(lank, dict) and lank.get("availabilityCode") == "OA":
            return lank.get("url")
    return None


def _forma(post: dict) -> dict:
    ar_raw = post.get("pubYear")
    ar = int(ar_raw) if ar_raw and str(ar_raw).isdigit() else None
    kalla_id = f"{post.get('source')}:{post.get('id')}" if post.get("source") and post.get("id") else post.get("id")

    return {
        "kalla": KALLA,
        "kalla_id": kalla_id,
        "doi": post.get("doi"),
        "titel": post.get("title"),
        "forfattare": _forfattare(post.get("authorString")),
        "ar": ar,
        "typ": post.get("pubType"),
        "url": f"https://europepmc.org/article/{post.get('source')}/{post.get('id')}"
        if post.get("source") and post.get("id") else None,
        "oa_lank": _oa_lank(post),
        "citeringar": post.get("citedByCount"),
    }


def sok(
    q: str | None = None,
    *,
    limit: int = 20,
    fran_ar: int | None = None,
    till_ar: int | None = None,
) -> dict:
    """Söker Europe PMC.

    q              - fritextfråga (Europe PMCs egen frågesyntax, t.ex.
                     "cancer AND SRC:MED" fungerar också).
    limit          - max antal träffar (1-1000, standard 20).
    fran_ar/till_ar- utgivningsårsintervall (inklusive), läggs till i frågan
                     som PUB_YEAR-villkor.
    """
    if not q:
        raise EuropePmcFel("Ange en fritextfråga (q).")
    fraga = q
    if fran_ar and till_ar:
        fraga = f"({q}) AND PUB_YEAR:[{int(fran_ar)} TO {int(till_ar)}]"
    elif fran_ar:
        fraga = f"({q}) AND PUB_YEAR:[{int(fran_ar)} TO 3000]"
    elif till_ar:
        fraga = f"({q}) AND PUB_YEAR:[0 TO {int(till_ar)}]"

    params = {
        "query": fraga,
        "format": "json",
        "resultType": "lite",
        "pageSize": str(max(1, min(limit, 1000))),
    }
    data = _hamta(params)
    traffar = [_forma(p) for p in (data.get("resultList") or {}).get("result", [])]
    return {"kalla": KALLA, "totalt": data.get("hitCount"), "antal": len(traffar), "traffar": traffar}


def hamta(kalla_id: str) -> dict:
    """Läser en enskild post via "KÄLLA:ID" (t.ex. "MED:42722545"), eller
    bara ID (antas då vara källan MED/PubMed)."""
    if not kalla_id or not kalla_id.strip():
        raise EuropePmcFel("Tomt id angavs.")
    if ":" in kalla_id:
        kalla, id_ = kalla_id.split(":", 1)
    else:
        kalla, id_ = "MED", kalla_id

    data = _hamta({
        "query": f"EXT_ID:{id_} AND SRC:{kalla}",
        "format": "json",
        "resultType": "core",
        "pageSize": "1",
    })
    poster = (data.get("resultList") or {}).get("result", [])
    if not poster:
        raise EuropePmcFel(f"Hittar ingen post med id '{kalla_id}' hos Europe PMC.")
    return _forma(poster[0])
