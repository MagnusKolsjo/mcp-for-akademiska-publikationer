"""Klientmodul för HAL (Hyper Articles en Ligne) REST-API:et.

HAL är Frankrikes nationella öppna arkiv för vetenskapliga publikationer,
starkt inom humaniora och samhällsvetenskap (inklusive arkeologi via
underarkivet HAL-SHS). Sökningen är samma Solr-baserade API som används för
att hämta en enskild post (filtrerat på halId_s).

API-referens: https://api.archives-ouvertes.fr/docs
"""

from __future__ import annotations

import os

import requests

import kallkonfig
from kallhjalp import DiscoveryKallaFel, ny_taktbegransare

BASE_URL = os.environ.get("HAL_BASE_URL", "https://api.archives-ouvertes.fr/search").rstrip("/")
USER_AGENT = kallkonfig.user_agent("HAL_USER_AGENT")
TIMEOUT = float(os.environ.get("HAL_TIMEOUT", "30"))

KALLA = "hal"

_FALT = "halId_s,title_s,authFullName_s,producedDateY_i,docType_s,doiId_s,fileMain_s,uri_s"


class HalFel(DiscoveryKallaFel):
    """Fel vid anrop mot HAL-API:et."""


_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

# Ingen publicerad hastighetsgräns; en försiktig, egen takt.
_vanta = ny_taktbegransare(0.5)


def _hamta(params: dict) -> dict:
    _vanta()
    try:
        svar = _session.get(f"{BASE_URL}/", params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise HalFel(f"Kunde inte nå HAL ({BASE_URL}): {type(exc).__name__}") from exc
    if svar.status_code != 200:
        raise HalFel(f"HAL svarade {svar.status_code}: {svar.text[:300]}")
    try:
        return svar.json()
    except ValueError as exc:
        raise HalFel(f"HAL gav ett svar som inte är JSON: {exc}") from exc


def _forsta(varde):
    if isinstance(varde, list):
        return varde[0] if varde else None
    return varde


def _forma(post: dict) -> dict:
    titel = _forsta(post.get("title_s"))
    return {
        "kalla": KALLA,
        "kalla_id": post.get("halId_s"),
        "doi": post.get("doiId_s"),
        "titel": titel,
        "forfattare": [{"namn": n} for n in post.get("authFullName_s") or []],
        "ar": post.get("producedDateY_i"),
        "typ": post.get("docType_s"),
        "url": post.get("uri_s") or (f"https://hal.science/{post['halId_s']}" if post.get("halId_s") else None),
        "oa_lank": post.get("fileMain_s"),
    }


def sok(
    q: str | None = None,
    *,
    limit: int = 20,
    fran_ar: int | None = None,
    till_ar: int | None = None,
) -> dict:
    """Söker HAL.

    q              - fritextfråga (Solr-syntax stöds, t.ex. fältprefix).
    limit          - max antal träffar (standard 20).
    fran_ar/till_ar- läggs till som ett Solr-intervall på producedDateY_i.
    """
    if not q:
        raise HalFel("Ange en fritextfråga (q).")
    fraga = q
    if fran_ar or till_ar:
        lag = int(fran_ar) if fran_ar else "*"
        hog = int(till_ar) if till_ar else "*"
        fraga = f"{q} AND producedDateY_i:[{lag} TO {hog}]"

    data = _hamta({"q": fraga, "rows": str(max(1, min(limit, 1000))), "wt": "json", "fl": _FALT})
    svar = data.get("response", {})
    traffar = [_forma(p) for p in svar.get("docs", [])]
    return {"kalla": KALLA, "totalt": svar.get("numFound"), "antal": len(traffar), "traffar": traffar}


def hamta(hal_id: str) -> dict:
    """Läser en enskild post via dess HAL-id (t.ex. "hal-04113132")."""
    if not hal_id or not hal_id.strip():
        raise HalFel("Tomt HAL-id angavs.")
    data = _hamta({"q": f"halId_s:{hal_id.strip()}", "rows": "1", "wt": "json", "fl": _FALT})
    poster = data.get("response", {}).get("docs", [])
    if not poster:
        raise HalFel(f"Hittar ingen post med id '{hal_id}' hos HAL.")
    return _forma(poster[0])
