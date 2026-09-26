# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klientmodul för Unpaywall REST-API:et.

Unpaywall svarar bara på en fråga: var finns en öppet tillgänglig kopia av
den här DOI:n? Ingen fritextsökning (deras /v2/search-endpoint är trasig
och används inte här — se modulens docstring i providers.py). Modulen
används bara som berikningskälla för discovery_oa_lank, inte i den enade
sökningen discovery_sok.

Unpaywall kräver en kontakt-e-post i varje anrop (?email=...). Källan
inaktiveras automatiskt om DISCOVERY_KONTAKT_EPOST saknas — se
kallkonfig.krav_saknas() och providers.py.

API-referens: https://unpaywall.org/products/api
"""

from __future__ import annotations

import os

import requests

import kallkonfig
from kallhjalp import DiscoveryKallaFel, ny_taktbegransare

BASE_URL = os.environ.get("UNPAYWALL_BASE_URL", "https://api.unpaywall.org/v2").rstrip("/")
USER_AGENT = kallkonfig.user_agent("UNPAYWALL_USER_AGENT")
TIMEOUT = float(os.environ.get("UNPAYWALL_TIMEOUT", "30"))

KALLA = "unpaywall"


class UnpaywallFel(DiscoveryKallaFel):
    """Fel vid anrop mot Unpaywall-API:et."""


_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

# Ingen publicerad hastighetsgräns, men en försiktig, jämn takt är gott skick
# mot en gratis, e-postidentifierad tjänst.
_vanta = ny_taktbegransare(0.5)


def _forfattare(z_authors: list | None) -> list[dict]:
    ut = []
    for f in z_authors or []:
        if not isinstance(f, dict):
            continue
        fornamn = f.get("given")
        efternamn = f.get("family")
        namn = " ".join(p for p in (fornamn, efternamn) if p)
        if namn:
            ut.append({"namn": namn})
    return ut


def hamta(doi: str) -> dict:
    """Läser open access-status för en DOI.

    Kräver DISCOVERY_KONTAKT_EPOST (kontrolleras redan av providers.py
    innan källan aktiveras, men även här som skydd mot direktanrop).
    """
    if not kallkonfig.KONTAKT_EPOST:
        raise UnpaywallFel(
            "Unpaywall kräver en kontakt-e-post. Sätt DISCOVERY_KONTAKT_EPOST i .env."
        )
    naken = (doi or "").strip().removeprefix("https://doi.org/").removeprefix("doi:")
    if not naken:
        raise UnpaywallFel("Tom DOI angavs.")

    _vanta()
    try:
        svar = _session.get(
            f"{BASE_URL}/{naken}",
            params={"email": kallkonfig.KONTAKT_EPOST},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        # Bara feltypen, inte hela undantagstexten — den kan innehålla hela
        # anropsadressen med e-posten i querysträngen (requests bakar ofta
        # in URL:en i sina egna undantagsmeddelanden).
        raise UnpaywallFel(
            f"Kunde inte nå Unpaywall ({BASE_URL}/{naken}): {type(exc).__name__}"
        ) from exc

    if svar.status_code == 404:
        raise UnpaywallFel(f"Unpaywall känner inte igen DOI:n '{doi}'.")
    if svar.status_code != 200:
        detalj = svar.text[:300].replace(kallkonfig.KONTAKT_EPOST, "<e-post>")
        raise UnpaywallFel(f"Unpaywall svarade {svar.status_code}: {detalj}")

    try:
        data = svar.json()
    except ValueError as exc:
        raise UnpaywallFel(f"Unpaywall gav ett svar som inte är JSON: {exc}") from exc

    bast = data.get("best_oa_location") or {}
    return {
        "kalla": KALLA,
        "kalla_id": data.get("doi") or naken,
        "doi": data.get("doi") or naken,
        "titel": data.get("title"),
        "forfattare": _forfattare(data.get("z_authors")),
        "ar": data.get("year"),
        "typ": data.get("genre"),
        "url": f"https://doi.org/{data.get('doi') or naken}",
        "oa_lank": bast.get("url_for_pdf") or bast.get("url"),
        "is_oa": data.get("is_oa"),
        "oa_status": data.get("oa_status"),
    }
