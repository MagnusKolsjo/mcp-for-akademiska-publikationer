# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klientmodul för SwePub via Libris Xsearch.

SwePub aggregerar publikationer från svenska lärosäten och myndigheter.
Xsearch (`http://libris.kb.se/xsearch?database=swepub`) är den enklaste av
SwePubs tre gränssnitt (Xsearch, SRU/MARCXML, bibliometri-API:et med
BIBFRAME-JSON) och räcker för fritextsökning; modulen använder den.

Xsearch saknar dokumenterad hämtning av en enskild känd post och ger ingen
DOI i sina fält — bara identifier (en swepub-URI), titel, upphov, typ,
språk och datum. hamta() stöds därför inte för den här källan.

API-referens: https://www.kb.se/samverkan-och-utveckling/nytt-fran-kb/
nyhetsarkiv/utveckling/2021-05-26-xsearch-apiet-for-libris-data.html
"""

from __future__ import annotations

import os

import requests

import kallkonfig
from kallhjalp import DiscoveryKallaFel, ny_taktbegransare, ren_text

BASE_URL = os.environ.get("SWEPUB_BASE_URL", "https://libris.kb.se/xsearch").rstrip("/")
USER_AGENT = kallkonfig.user_agent("SWEPUB_USER_AGENT")
TIMEOUT = float(os.environ.get("SWEPUB_TIMEOUT", "30"))

KALLA = "swepub"


class SwePubFel(DiscoveryKallaFel):
    """Fel vid anrop mot SwePub/Xsearch."""


_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

# Ingen publicerad hastighetsgräns för Xsearch, men den delas med alla
# libris.kb.se-tjänster (se libris_client.py) — en försiktig, egen takt här.
_vanta = ny_taktbegransare(0.5)


def _som_lista(varde) -> list[str]:
    if varde is None:
        return []
    if isinstance(varde, list):
        return [v for v in varde if v]
    return [varde]


def _ar_ur_datum(datum: str | None) -> int | None:
    if not datum:
        return None
    forsta_fyra = datum[:4]
    return int(forsta_fyra) if forsta_fyra.isdigit() else None


def _forma(post: dict) -> dict:
    datum = _som_lista(post.get("date"))
    return {
        "kalla": KALLA,
        "kalla_id": post.get("identifier"),
        "doi": None,
        "titel": post.get("title"),
        "forfattare": [{"namn": n} for n in _som_lista(post.get("creator"))],
        "ar": _ar_ur_datum(datum[0] if datum else None),
        "typ": post.get("type"),
        "url": post.get("identifier"),
        "oa_lank": None,
        "sammanfattning": ren_text(post.get("description")),
    }


def sok(
    q: str | None = None,
    *,
    limit: int = 20,
    fran_ar: int | None = None,
    till_ar: int | None = None,
) -> dict:
    """Söker SwePub via Xsearch.

    q              - fritextfråga.
    limit          - max antal träffar (Xsearch tillåter höga värden; kapas
                     här till 100 för rimlig svarsstorlek).
    fran_ar/till_ar- stöds inte av Xsearch och ignoreras (inget
                     dokumenterat datumfilter i gränssnittet); tas emot för
                     att matcha den gemensamma sok-signaturen.
    """
    if not q:
        raise SwePubFel("Ange en fritextfråga (q).")

    _vanta()
    params = {
        "database": "swepub",
        "query": q,
        "format": "json",
        "n": str(max(1, min(limit, 100))),
    }
    try:
        svar = _session.get(BASE_URL, params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise SwePubFel(f"Kunde inte nå SwePub ({BASE_URL}): {type(exc).__name__}") from exc

    if svar.status_code != 200:
        raise SwePubFel(f"SwePub svarade {svar.status_code}: {svar.text[:300]}")
    try:
        data = svar.json()
    except ValueError as exc:
        raise SwePubFel(f"SwePub gav ett svar som inte är JSON: {exc}") from exc

    xsearch = data.get("xsearch", {})
    poster = xsearch.get("list", []) or []
    traffar = [_forma(p) for p in poster]

    return {
        "kalla": KALLA,
        "totalt": xsearch.get("records"),
        "antal": len(traffar),
        "traffar": traffar,
    }
