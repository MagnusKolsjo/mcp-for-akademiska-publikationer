# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klientmodul för DOAJ (Directory of Open Access Journals) REST-API:et.

DOAJ indexerar artiklar ur granskade open access-tidskrifter över alla
ämnesfält. All fulltext bakom DOAJ:s artiklar är per definition fritt
tillgänglig. Metadata är CC0.

API-referens: https://doaj.org/api/docs
"""

from __future__ import annotations

import os
import urllib.parse

import requests

import kallkonfig
from kallhjalp import DiscoveryKallaFel, ny_taktbegransare, ren_text

BASE_URL = os.environ.get("DOAJ_BASE_URL", "https://doaj.org/api").rstrip("/")
USER_AGENT = kallkonfig.user_agent("DOAJ_USER_AGENT")
TIMEOUT = float(os.environ.get("DOAJ_TIMEOUT", "30"))

KALLA = "doaj"


class DoajFel(DiscoveryKallaFel):
    """Fel vid anrop mot DOAJ-API:et."""


_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

# DOAJ rekommenderar ~2 anrop/sekund; en försiktig, egen takt.
_vanta = ny_taktbegransare(0.5)


def _hamta(path: str, params: dict | None = None) -> dict:
    _vanta()
    try:
        svar = _session.get(f"{BASE_URL}{path}", params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise DoajFel(f"Kunde inte nå DOAJ ({BASE_URL}{path}): {type(exc).__name__}") from exc
    if svar.status_code == 404:
        raise DoajFel("DOAJ hittar inte den angivna posten.")
    if svar.status_code != 200:
        raise DoajFel(f"DOAJ svarade {svar.status_code}: {svar.text[:300]}")
    try:
        return svar.json()
    except ValueError as exc:
        raise DoajFel(f"DOAJ gav ett svar som inte är JSON: {exc}") from exc


def _identifierare(bibjson: dict, typ: str) -> str | None:
    for ident in bibjson.get("identifier", []) or []:
        if isinstance(ident, dict) and ident.get("type") == typ:
            return ident.get("id")
    return None


def _fulltext_url(bibjson: dict) -> str | None:
    for lank in bibjson.get("link", []) or []:
        if isinstance(lank, dict) and lank.get("type") == "fulltext":
            return lank.get("url")
    return None


def _forma(post: dict) -> dict:
    bibjson = post.get("bibjson", {})
    doi = _identifierare(bibjson, "doi")
    ar_str = bibjson.get("year")
    ar = int(ar_str) if ar_str and str(ar_str).isdigit() else None
    url = _fulltext_url(bibjson) or (f"https://doi.org/{doi}" if doi else None)

    return {
        "kalla": KALLA,
        "kalla_id": post.get("id"),
        "doi": doi,
        "titel": bibjson.get("title"),
        "forfattare": [{"namn": a["name"]} for a in bibjson.get("author", []) or [] if a.get("name")],
        "ar": ar,
        "typ": "article",
        "url": url,
        # DOAJ indexerar bara open access-tidskrifter — fulltextlänken är
        # alltså alltid en öppen kopia, inte bara en landningssida.
        "oa_lank": url,
        "sammanfattning": ren_text(bibjson.get("abstract")),
    }


def sok(
    q: str | None = None,
    *,
    limit: int = 20,
    fran_ar: int | None = None,
    till_ar: int | None = None,
) -> dict:
    """Söker DOAJ-artiklar.

    q              - fritextfråga (Elasticsearch query string-syntax stöds).
    limit          - max antal träffar (1-100, standard 20).
    fran_ar/till_ar- läggs till i frågan som ett intervall på bibjson.year.
    """
    if not q:
        raise DoajFel("Ange en fritextfråga (q).")
    fraga = q
    if fran_ar or till_ar:
        lag = int(fran_ar) if fran_ar else "*"
        hog = int(till_ar) if till_ar else "*"
        fraga = f"{q} AND bibjson.year:[{lag} TO {hog}]"

    kodad = urllib.parse.quote(fraga, safe="")
    data = _hamta(f"/search/articles/{kodad}", {"pageSize": str(max(1, min(limit, 100)))})
    traffar = [_forma(p) for p in data.get("results", [])]
    return {"kalla": KALLA, "totalt": data.get("total"), "antal": len(traffar), "traffar": traffar}


def hamta(doaj_id: str) -> dict:
    """Läser en enskild artikel via dess DOAJ-id."""
    if not doaj_id or not doaj_id.strip():
        raise DoajFel("Tomt DOAJ-id angavs.")
    post = _hamta(f"/articles/{doaj_id.strip()}")
    if not post:
        raise DoajFel(f"Hittar ingen artikel med id '{doaj_id}' hos DOAJ.")
    return _forma(post)
