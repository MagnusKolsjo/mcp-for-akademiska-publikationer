# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Databasåtkomst för discovery: PostgreSQL eller SQLite, valt via DATABASE_URL.

Databasen är frivillig. Utan DATABASE_URL fungerar alla sökverktyg precis
som förut, bara utan svarscache och arbetsbibliotek; discovery_kallor visar
varför. Schemat ligger i db/schema_*.sql och vektortabellen för
bibliotekets semantiska sökning i db/vektor_*.sql. `postgresql://` och `sqlite:///` är likvärdiga val — Postgres
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
import threading
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
def hamta_db(*, vektor: bool = False):
    """Öppnar en anslutning, committar vid lyckat block och stänger alltid.

    vektor=True laddar sqlite-vec i SQLite-anslutningen (bibliotekets
    semantiska sökning); Postgres har pgvector som tillägg i databasen.

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
        # SQLite följer främmande nycklar (ON DELETE CASCADE) bara om det
        # slås på, och det gäller per anslutning.
        conn.execute("PRAGMA foreign_keys=ON")
        if vektor:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ar_postgres() -> bool:
    return _ar_postgres()


def ph() -> str:
    """Platshållare för parameterbindning: %s i Postgres, ? i SQLite."""
    return "%s" if _ar_postgres() else "?"


def prefix() -> str:
    """Schemaprefix för tabellnamn: 'discovery.' i Postgres, tomt i SQLite."""
    return "discovery." if _ar_postgres() else ""


_SQL_KATALOG = Path(__file__).parent / "db"
_schema_las = threading.Lock()
_schema_klart = False


def _kor_sqlfil(namn: str, *, vektor: bool = False) -> None:
    sql = (_SQL_KATALOG / namn).read_text(encoding="utf-8")
    with hamta_db(vektor=vektor) as conn:
        if _ar_postgres():
            conn.cursor().execute(sql)
        else:
            conn.executescript(sql)


def initiera_schema() -> None:
    """Skapar schema, tabeller och index om de inte redan finns (idempotent).

    Körs en gång per process, första gången svarscachen eller biblioteket
    behöver databasen — inte vid import, så att en nedstängd databas inte
    hindrar servern från att starta."""
    global _schema_klart
    if _schema_klart:
        return
    with _schema_las:
        if _schema_klart:
            return
        if not _ar_postgres():
            # WAL låter läsare och en skrivare arbeta samtidigt — discovery_sok
            # skriver cacheposter från flera trådar på en gång.
            with hamta_db() as conn:
                conn.execute("PRAGMA journal_mode=WAL")
        _kor_sqlfil("schema_postgres.sql" if _ar_postgres() else "schema_sqlite.sql")
        _schema_klart = True


def initiera_vektor() -> None:
    """Skapar vektortabellen för semantisk sökning (idempotent). Kastar om
    pgvector eller sqlite-vec saknas — anroparen avgör vad det betyder."""
    initiera_schema()
    _kor_sqlfil("vektor_postgres.sql" if _ar_postgres() else "vektor_sqlite.sql", vektor=True)
