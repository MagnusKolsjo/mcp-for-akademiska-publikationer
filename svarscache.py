# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Svarscache: sparar källornas API-svar en kort tid i databasen.

En assistent som arbetar med en fråga kör ofta samma eller nästan samma
sökning flera gånger och läser samma DOI via flera verktyg. Cachen sparar
då API-anrop — vilket betyder pengar hos OpenAlex, och kvot hos källor med
hård takt (arXiv, OSF) — och ger snabbare svar.

Det här är en tillfällig svarscache, inte ett arkiv: en post som passerat
sin giltighetstid är värdelös och raderas. Innehåll som ska sparas
permanent hör hemma i arbetsbiblioteket, inte här.

Konfiguration i .env:

    DATABASE_URL                     postgresql://… eller sqlite:///… (se db.py)
    DISCOVERY_CACHE_AKTIV            true/false (standard: true)
    DISCOVERY_CACHE_SOK_TIMMAR       livstid för sökningar (standard: 6)
    DISCOVERY_CACHE_POST_DAGAR       livstid för enskilda poster (standard: 7)

Fail-open: är cachen avstängd, saknas DATABASE_URL eller svarar databasen
inte, körs anropet direkt mot källan som om cachen inte fanns. En sökning
ska aldrig falla på att cachen inte fungerar.

Semantic Scholar cachas aldrig: villkoren förbjuder lagring och
vidaredistribution av deras data utöver det enskilda svaret.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import threading
import time
from typing import Any, Callable

import db
import kallkonfig

log = logging.getLogger(__name__)

SOK = "sok"
POST = "post"

_EJ_CACHADE_KALLOR = frozenset({"semanticscholar"})

_LIVSTID_S = {
    SOK: float(os.environ.get("DISCOVERY_CACHE_SOK_TIMMAR", "6")) * 3600,
    POST: float(os.environ.get("DISCOVERY_CACHE_POST_DAGAR", "7")) * 86400,
}

# Utgångna poster rensas i samband med skrivning, i genomsnitt vid vart
# hundrade skriv — tillräckligt för att tabellen inte ska växa, utan att
# varje skrivning betalar för en rensning.
_RENSNING_SANNOLIKHET = 0.01

_AKTIV_ENLIGT_ENV = kallkonfig.aktiv("cache")
_initierad = False
_avstangd_orsak: str | None = None
# discovery_sok når cachen från flera trådar samtidigt; låset ser till att
# schemat är färdigt innan någon tråd läser eller skriver.
_start_las = threading.Lock()


def _starta() -> None:
    """Initierar schemat första gången cachen används (inte vid import, så
    att en nedstängd databas inte hindrar servern från att starta)."""
    global _initierad, _avstangd_orsak
    if _initierad:
        return
    with _start_las:
        if _initierad:
            return
        _initiera()
        _initierad = True


def _initiera() -> None:
    global _avstangd_orsak
    if not _AKTIV_ENLIGT_ENV:
        _avstangd_orsak = "avstängd med DISCOVERY_CACHE_AKTIV=false"
    elif not db.konfigurerad():
        _avstangd_orsak = "kräver DATABASE_URL (postgresql://… eller sqlite:///…), som inte är satt"
    else:
        try:
            db.initiera_schema()
        except Exception as exc:  # databasen nere eller felkonfigurerad
            _avstangd_orsak = f"databasen gick inte att initiera: {type(exc).__name__}: {' '.join(str(exc).split())}"
            log.warning("Svarscachen avstängd: %s", _avstangd_orsak)


def status() -> dict:
    """Aktiv-status för discovery_kallor."""
    _starta()
    rad: dict[str, Any] = {
        "aktiv": _avstangd_orsak is None,
        "sok_timmar": _LIVSTID_S[SOK] / 3600,
        "post_dagar": _LIVSTID_S[POST] / 86400,
        "cachas_aldrig": sorted(_EJ_CACHADE_KALLOR),
    }
    if _avstangd_orsak:
        rad["inaktiverad_orsak"] = _avstangd_orsak
    return rad


def _nyckel(kalla: str, operation: str, parametrar: dict) -> str:
    """Stabil nyckel av källa, operation och parametrar. Blanksteg i
    strängar normaliseras (\"ai  ethics \" = \"ai ethics\"), men skiftläget
    bevaras — flera källors frågespråk skiljer på AND och and."""
    def _stada(v):
        if isinstance(v, str):
            return " ".join(v.split())
        if isinstance(v, dict):
            return {k: _stada(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [_stada(x) for x in v]
        return v

    underlag = json.dumps(
        {"k": kalla, "o": operation, "p": _stada(parametrar)},
        sort_keys=True, ensure_ascii=False, default=str,
    )
    return hashlib.sha256(underlag.encode("utf-8")).hexdigest()


def _las(nyckel: str) -> Any | None:
    with db.hamta_db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT svar FROM {db.prefix()}svarscache WHERE nyckel = {db.ph()} AND giltig_till > {db.ph()}",
            (nyckel, time.time()),
        )
        rad = cur.fetchone()
    return json.loads(rad[0]) if rad else None


def _skriv(nyckel: str, kalla: str, operation: str, svar: Any) -> None:
    nu = time.time()
    varden = (nyckel, kalla, operation, json.dumps(svar, ensure_ascii=False, default=str), nu, nu + _LIVSTID_S[operation])
    p = db.ph()
    with db.hamta_db() as conn:
        cur = conn.cursor()
        # Samma syntax i Postgres och SQLite (≥ 3.24).
        cur.execute(
            f"""INSERT INTO {db.prefix()}svarscache
                (nyckel, kalla, operation, svar, skapad, giltig_till)
                VALUES ({p}, {p}, {p}, {p}, {p}, {p})
                ON CONFLICT (nyckel) DO UPDATE SET
                    svar = EXCLUDED.svar, skapad = EXCLUDED.skapad,
                    giltig_till = EXCLUDED.giltig_till""",
            varden,
        )
        if random.random() < _RENSNING_SANNOLIKHET:
            cur.execute(f"DELETE FROM {db.prefix()}svarscache WHERE giltig_till <= {p}", (nu,))


def hamta_eller_kor(
    kalla: str,
    operation: str,
    parametrar: dict,
    kor: Callable[[], Any],
) -> tuple[Any, bool]:
    """Returnerar (svar, fran_cache).

    kor() anropas bara om ett giltigt svar saknas i cachen. Ett undantag från
    kor() lyfts vidare oförändrat och cachas aldrig — ett fel hos källan ska
    inte leva kvar i timmar. Ett tomt svar (noll träffar) är giltigt och cachas.
    """
    _starta()
    if _avstangd_orsak is not None or kalla in _EJ_CACHADE_KALLOR:
        return kor(), False

    nyckel = _nyckel(kalla, operation, parametrar)
    try:
        sparat = _las(nyckel)
    except Exception as exc:
        log.warning("Svarscachen gick inte att läsa (%s) — frågar källan direkt", exc)
        sparat = None
    if sparat is not None:
        return sparat, True

    svar = kor()
    try:
        _skriv(nyckel, kalla, operation, svar)
    except Exception as exc:
        log.warning("Svarscachen gick inte att skriva (%s) — svaret levereras ändå", exc)
    return svar, False
