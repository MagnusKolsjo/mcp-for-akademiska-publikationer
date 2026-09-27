# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Databasåtkomst för discovery: PostgreSQL eller SQLite, valt via DATABASE_URL.

Databasen är frivillig. Utan DATABASE_URL fungerar alla sökverktyg precis
som förut, bara utan svarscache; discovery_kallor visar varför cachen är
avstängd. `postgresql://` och `sqlite:///` är likvärdiga val — Postgres
passar när databasen ändå delas med andra MCP-servrar, SQLite när en lokal
fil räcker.

Varje anrop öppnar och stänger en egen anslutning (per-anrops-mönstret):
discovery_sok frågar källorna från flera trådar samtidigt, och egna
anslutningar per anrop gör det trådsäkert utan delat tillstånd.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

# psycopg2 importeras bara om Postgres används — SQLite-installationer
# behöver inte paketet.
try:
    import psycopg2
except ImportError:
    psycopg2 = None

log = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()


def konfigurerad() -> bool:
    """True om DATABASE_URL pekar mot en backend som går att använda."""
    return _ar_postgres() or DATABASE_URL.startswith("sqlite:")


def _ar_postgres() -> bool:
    return DATABASE_URL.startswith(("postgresql://", "postgres://"))


def _sqlite_sokvag() -> Path:
    """sqlite:///fil.db är relativ till servermappen (inte processens cwd,
    som MCP-klienten styr); sqlite:////abs/fil.db är absolut."""
    sokvag = urlparse(DATABASE_URL).path
    if sokvag.startswith("//"):
        return Path(sokvag[1:])
    sokvag = sokvag.lstrip("/")
    if not sokvag:
        raise RuntimeError("DATABASE_URL för SQLite saknar filsökväg, t.ex. sqlite:///discovery-cache.db")
    return Path(__file__).parent / sokvag


@contextlib.contextmanager
def hamta_db():
    """Öppnar en anslutning, committar vid lyckat block och stänger alltid.

    (sqlite3:s och psycopg2:s egna `with conn` committar men stänger inte —
    därför en egen kontexthanterare.)
    """
    if _ar_postgres():
        if psycopg2 is None:
            raise RuntimeError(
                "DATABASE_URL pekar mot Postgres men psycopg2 är inte installerat. "
                "Kör 'pip install psycopg2-binary' eller välj sqlite:/// i DATABASE_URL."
            )
        # client_encoding: svaren innehåller åäö och annan icke-ASCII-text,
        # oavsett vilken standardkodning databasen skapades med.
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5, client_encoding="UTF8")
    else:
        # timeout: väntan på skrivlås när flera trådar skriver samtidigt.
        conn = sqlite3.connect(_sqlite_sokvag(), timeout=10)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ph() -> str:
    """Platshållare för parameterbindning: %s i Postgres, ? i SQLite."""
    return "%s" if _ar_postgres() else "?"


def prefix() -> str:
    """Schemaprefix för tabellnamn: 'discovery.' i Postgres, tomt i SQLite."""
    return "discovery." if _ar_postgres() else ""


def initiera_schema() -> None:
    """Skapar schema, tabeller och index om de inte redan finns (idempotent)."""
    with hamta_db() as conn:
        cur = conn.cursor()
        if _ar_postgres():
            cur.execute("CREATE SCHEMA IF NOT EXISTS discovery")
        else:
            # WAL låter läsare och en skrivare arbeta samtidigt — discovery_sok
            # skriver cacheposter från flera trådar på en gång.
            cur.execute("PRAGMA journal_mode=WAL")
        # skapad/giltig_till är Unix-tid i sekunder: samma typ och jämförelse
        # i båda backenderna, utan datumadaptrar.
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {prefix()}svarscache (
                nyckel TEXT PRIMARY KEY,
                kalla TEXT NOT NULL,
                operation TEXT NOT NULL,
                svar TEXT NOT NULL,
                skapad DOUBLE PRECISION NOT NULL,
                giltig_till DOUBLE PRECISION NOT NULL
            )
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS svarscache_giltig_till_idx
            ON {prefix()}svarscache (giltig_till)
        """)
