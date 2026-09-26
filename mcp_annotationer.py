# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Verktygsannotationer och cachningshintar för MCP-servern.

MCP:s fyra hintar (readOnlyHint, destructiveHint, idempotentHint,
openWorldHint) är hintar till klienten, inte garantier. En klient kan
använda dem för att avgöra vad som får köras utan att fråga användaren.
Utan dem går ett verktyg som skriver inte att skilja från ett som läser.

Varje verktyg faller i exakt en av fyra klasser:

    LASNING_EXTERN  Hämtar från källans API över nätet, eller från en lokal
                    cache som fylls på därifrån. Öppen värld: källan kan
                    publicera nytt när som helst, så ett upprepat anrop kan
                    ge ett annat svar. Att en läsning lägger ett dokument i
                    den lokala cachen gör den inte till en skrivning; det
                    ändrar inget som användaren ser.
    LASNING_DB      Läser bara ur serverns egen databas. Sluten värld:
                    innehållet ändras bara av våra egna synkar.
    SYNK            Skriver till databasen. Idempotent upsert på naturlig
                    nyckel, så att samma anrop två gånger ger samma
                    sluttillstånd. Befintliga rader uppdateras, raderas aldrig.
    SKRIVNING_DESTRUKTIV
                    Reserverad. Klassen finns så att den som inför ett
                    raderande verktyg måste välja den medvetet i stället för
                    att återanvända SYNK.

Ingångspunkter:
    LASNING_EXTERN, LASNING_DB, SYNK, SKRIVNING_DESTRUKTIV
    CACHE_HINTAR
"""

from __future__ import annotations

from mcp.server.caching import CacheHint
from mcp.types import ToolAnnotations

LASNING_EXTERN = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

LASNING_DB = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

# Synkarna upsertar mot naturlig nyckel. Att köra om en synk är säkert,
# och det ska synas för klienten; annars behandlas den som destruktiv.
SYNK = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

SKRIVNING_DESTRUKTIV = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)


# Cachningshintar enligt protokollrevision 2026-07-28.
#
# Utan hintar svarar servern ttlMs=0, alltså "omedelbart inaktuell", och
# klienten hämtar om verktygslistan vid varje behov. Verktygsdefinitionerna
# ändras bara när koden driftsätts, och det kräver ändå omstart av klienten,
# så en timme är gott om marginal.
#
# `public` stämmer så länge listorna inte varierar med vem som anropar.
# Börjar ett verktyg filtreras per användare måste hinten bli `private`.
CACHE_HINTAR = {
    "tools/list": CacheHint(ttl_ms=3_600_000, scope="public"),
    "prompts/list": CacheHint(ttl_ms=3_600_000, scope="public"),
    "server/discover": CacheHint(ttl_ms=3_600_000, scope="public"),
}
