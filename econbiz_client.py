# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klientmodul för EconBiz REST-API:et.

EconBiz (ZBW — Leibniz-informationscentralen för nationalekonomi) indexerar
ekonomisk litteratur, med tonvikt på working papers och grå litteratur som
inte alltid har DOI. Ingen massnedladdning — modulen håller sig till
enstaka sökningar och hämtningar, ingen paginerad totalskörd.

API-referens: https://api.econbiz.de/ (odokumenterat utöver exempel på
econbiz.de; verifierat mot faktiska svar).
"""

from __future__ import annotations

import os

import requests

import kallkonfig
from kallhjalp import DiscoveryKallaFel, ny_taktbegransare

BASE_URL = os.environ.get("ECONBIZ_BASE_URL", "https://api.econbiz.de/v1").rstrip("/")
USER_AGENT = kallkonfig.user_agent("ECONBIZ_USER_AGENT")
TIMEOUT = float(os.environ.get("ECONBIZ_TIMEOUT", "30"))

KALLA = "econbiz"


class EconBizFel(DiscoveryKallaFel):
    """Fel vid anrop mot EconBiz-API:et."""


_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

# Ingen publicerad hastighetsgräns, men "ingen massnedladdning" enligt
# källans egna villkor — en försiktig, egen takt.
_vanta = ny_taktbegransare(1.0)


def _hamta(path: str, params: dict | None = None) -> dict:
    _vanta()
    try:
        svar = _session.get(f"{BASE_URL}{path}", params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise EconBizFel(f"Kunde inte nå EconBiz ({BASE_URL}{path}): {type(exc).__name__}") from exc
    if svar.status_code == 404:
        raise EconBizFel("EconBiz hittar inte den angivna posten.")
    if svar.status_code != 200:
        raise EconBizFel(f"EconBiz svarade {svar.status_code}: {svar.text[:300]}")
    try:
        return svar.json()
    except ValueError as exc:
        raise EconBizFel(f"EconBiz gav ett svar som inte är JSON: {exc}") from exc


def _forma(post: dict) -> dict:
    datum = post.get("date") or []
    ar = None
    if datum:
        siffror = "".join(c for c in datum[0] if c.isdigit())
        # Sista fyra siffrorna i en datumsträng som "1 April 2026" eller
        # "May2026" är oftast året.
        if len(siffror) >= 4:
            ar = int(siffror[-4:])

    lankar = post.get("identifier_url") or []
    url = lankar[0] if lankar else post.get("source_url")

    return {
        "kalla": KALLA,
        "kalla_id": post.get("id"),
        "doi": None,
        "titel": post.get("title"),
        "forfattare": [{"namn": n} for n in (post.get("creator") or [])],
        "ar": ar,
        "typ": post.get("type"),
        "url": url,
        "oa_lank": next((u for u in lankar if u.lower().endswith(".pdf")), None),
    }


def sok(
    q: str | None = None,
    *,
    limit: int = 20,
    fran_ar: int | None = None,
    till_ar: int | None = None,
) -> dict:
    """Söker EconBiz.

    q              - fritextfråga.
    limit          - max antal träffar (standard 20).
    fran_ar/till_ar- stöds inte av det odokumenterade API:et och ignoreras.
    """
    if not q:
        raise EconBizFel("Ange en fritextfråga (q).")
    data = _hamta("/search", {"q": q, "size": str(max(1, min(limit, 100)))})
    hits = (data.get("hits") or {})
    traffar = [_forma(p) for p in hits.get("hits", [])]
    return {"kalla": KALLA, "totalt": hits.get("total"), "antal": len(traffar), "traffar": traffar}


def hamta(id_: str) -> dict:
    """Läser en enskild post via dess EconBiz-id."""
    if not id_ or not id_.strip():
        raise EconBizFel("Tomt EconBiz-id angavs.")
    data = _hamta(f"/record/{id_.strip()}")
    post = data.get("record")
    if not post:
        raise EconBizFel(f"Hittar ingen post med id '{id_}' hos EconBiz.")
    return _forma(post)
