# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klientmodul för zbMATH Open REST-API:et.

zbMATH Open är den matematiska bibliografidatabasen (efterföljaren till
Zentralblatt MATH) — täcker matematik och angränsande tillämpade fält långt
tillbaka i tiden, med MSC-klassificering. Bibliografiska data är CC0;
redaktionella bidrag (recensioner) är CC BY-SA 4.0 — se README:s attribution.

API-referens: https://api.zbmath.org/v1/
"""

from __future__ import annotations

import os

import requests

import kallkonfig
from kallhjalp import DiscoveryKallaFel, ny_taktbegransare, ren_text

BASE_URL = os.environ.get("ZBMATH_BASE_URL", "https://api.zbmath.org/v1").rstrip("/")
USER_AGENT = kallkonfig.user_agent("ZBMATH_USER_AGENT")
TIMEOUT = float(os.environ.get("ZBMATH_TIMEOUT", "30"))

KALLA = "zbmath"


class ZbmathFel(DiscoveryKallaFel):
    """Fel vid anrop mot zbMATH Open-API:et."""


_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

# Ingen publicerad hastighetsgräns; en försiktig, egen takt.
_vanta = ny_taktbegransare(1.0)


def _hamta(path: str, params: dict) -> dict | None:
    """GET mot zbMATH Open. None betyder 404 — källan svarar så både för en
    okänd post och för en sökning utan träffar; anroparen avgör vilket."""
    _vanta()
    try:
        svar = _session.get(f"{BASE_URL}{path}", params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise ZbmathFel(f"Kunde inte nå zbMATH Open ({BASE_URL}{path}): {type(exc).__name__}") from exc
    if svar.status_code == 404:
        return None
    if svar.status_code != 200:
        raise ZbmathFel(f"zbMATH Open svarade {svar.status_code}: {svar.text[:300]}")
    try:
        return svar.json()
    except ValueError as exc:
        raise ZbmathFel(f"zbMATH Open gav ett svar som inte är JSON: {exc}") from exc


def _doi(lankar: list | None) -> str | None:
    for lank in lankar or []:
        if isinstance(lank, dict) and lank.get("type") == "doi":
            return lank.get("identifier")
    return None


# zbMATH Open visar den här platshållaren i stället för texten när
# recensionens licens inte tillåter vidarespridning.
_EJ_TILLGANGLIG = "contents unavailable due to conflicting licenses"


def _sammanfattning(post: dict) -> str | None:
    for bidrag in post.get("editorial_contributions") or []:
        text = (bidrag or {}).get("text") if isinstance(bidrag, dict) else None
        if text and _EJ_TILLGANGLIG not in text:
            return ren_text(text)
    return None


def _forma(post: dict) -> dict:
    titel_obj = post.get("title") or {}
    forfattare = [
        {"namn": a["name"]} for a in (post.get("contributors") or {}).get("authors", [])
        if isinstance(a, dict) and a.get("name")
    ]
    typ = (post.get("document_type") or {}).get("description")
    # "year" kommer omväxlande som int och som sträng beroende på post.
    ar_raw = post.get("year")
    ar = int(ar_raw) if ar_raw is not None and str(ar_raw).strip().isdigit() else None

    return {
        "kalla": KALLA,
        "kalla_id": post.get("identifier") or str(post.get("id")),
        "doi": _doi(post.get("links")),
        "titel": titel_obj.get("title"),
        "forfattare": forfattare,
        "ar": ar,
        "typ": typ,
        "url": post.get("zbmath_url"),
        "oa_lank": None,
        "sammanfattning": _sammanfattning(post),
    }


def sok(
    q: str | None = None,
    *,
    limit: int = 20,
    fran_ar: int | None = None,
    till_ar: int | None = None,
) -> dict:
    """Söker zbMATH Open.

    q              - fritextfråga.
    limit          - max antal träffar (1-1000, standard 20).
    fran_ar/till_ar- läggs till i search_string som py:fran..till (zbMATH:s
                     egen fältsyntax för publiceringsår).
    """
    if not q:
        raise ZbmathFel("Ange en fritextfråga (q).")
    fraga = q
    if fran_ar and till_ar:
        fraga = f"{q} py: {int(fran_ar)}-{int(till_ar)}"
    elif fran_ar:
        fraga = f"{q} py: {int(fran_ar)}-"
    elif till_ar:
        fraga = f"{q} py: -{int(till_ar)}"

    data = _hamta("/document/_search", {
        "search_string": fraga,
        "results_per_page": str(max(1, min(limit, 1000))),
    })
    if data is None:
        return {"kalla": KALLA, "totalt": 0, "antal": 0, "traffar": []}
    traffar = [_forma(p) for p in data.get("result", [])]
    totalt = (data.get("status") or {}).get("nr_total_results")
    return {"kalla": KALLA, "totalt": totalt, "antal": len(traffar), "traffar": traffar}


def hamta(identifierare: str) -> dict:
    """Läser en enskild post via dess zbMATH-identifierare (t.ex. "0890.05001")."""
    if not identifierare or not identifierare.strip():
        raise ZbmathFel("Tomt zbMATH-id angavs.")
    data = _hamta(f"/document/{identifierare.strip()}", {})
    post = (data or {}).get("result")
    if not post:
        raise ZbmathFel(f"Hittar ingen post med id '{identifierare}' hos zbMATH Open.")
    return _forma(post if isinstance(post, dict) else post[0])
