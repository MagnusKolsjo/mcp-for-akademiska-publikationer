# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Gemensamma byggblock för de nya discovery-källornas HTTP-anrop och takt.

Varje källa har sin egen klientmodul (t.ex. openalex_client.py), men alla
delar samma mönster: ett GET mot ett JSON- eller XML-API, en projektspecifik
User-Agent, en timeout och en strypning per källa så att flera samtidiga
verktygsanrop inte forcerar källans hastighetsgräns. Det här är byggblocken
källorna sätts ihop av — se respektive <namn>_client.py för fält och
frågespråk.

De äldre klientmodulerna (libris_client.py, crossref_client.py,
datacite_client.py, arxiv_client.py) har varsitt eget HTTP-mönster sedan
tidigare men delar textstädningen (ren_text, kapa_text) med övriga.
"""

from __future__ import annotations

import html
import re
import threading
import time
from typing import Callable


class DiscoveryKallaFel(Exception):
    """Basklass för alla nya källors felklasser.

    En gemensam basklass gör att providers.py kan fånga fel från vilken
    källa som helst enhetligt (en källa som fallerar ska inte fälla
    discovery_sok/discovery_hamta som helhet), utan att räkna upp varje
    källas egen felklass för sig.
    """


def ny_taktbegransare(min_intervall_s: float) -> Callable[[], None]:
    """Bygger en strypfunktion som reserverar nästa tillåtna starttid.

    Returnerad funktion blockerar tills det är anroparens tur, men håller
    ett lås bara för bokföringen — inte under själva HTTP-anropet. Flera
    trådar kan därför ha anrop i flykt samtidigt, så länge starterna mot
    just den här källan ligger minst min_intervall_s isär. Nödvändigt
    eftersom MCP 2.x kör synkrona verktyg på arbetstrådar, så
    discovery_sok kan fråga en källa från flera trådar samtidigt.

    En egen instans per källa (anropa en gång per klientmodul, vid
    modulinläsning) — källorna har olika takt och ska inte dela kö.
    """
    las = threading.Lock()
    tillstand = {"nasta": 0.0}

    def vanta() -> None:
        with las:
            start = max(time.monotonic(), tillstand["nasta"])
            tillstand["nasta"] = start + min_intervall_s
        drojsmal = start - time.monotonic()
        if drojsmal > 0:
            time.sleep(drojsmal)

    return vanta


def kapa_lista(lista: list, max_antal: int) -> list:
    """Kapar en lista till max_antal poster. Ren hjälpfunktion för läsbarhet."""
    if max_antal <= 0:
        return []
    return lista[:max_antal]


def ren_text(text) -> str | None:
    """HTML/JATS-taggar och teckenentiteter bort, blanksteg normaliserade.

    Källorna levererar abstract omväxlande som ren text, HTML (<p>, <i>)
    och JATS-XML (<jats:p>). Styckesgränser bevaras som radbrytningar så att
    ett längre abstract går att läsa och citera stycke för stycke.
    """
    if not text:
        return None
    if isinstance(text, (list, tuple)):
        text = next((t for t in text if t), None)
        if not text:
            return None
    text = re.sub(r"</?(?:jats:)?p\b[^>]*>|<br\s*/?>", "\n", str(text))
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    stycken = [" ".join(rad.split()) for rad in text.split("\n")]
    return "\n".join(s for s in stycken if s) or None


def kapa_text(text: str | None, max_tecken: int) -> tuple[str | None, bool]:
    """Kapar vid närmaste ordgräns före max_tecken och markerar med "…".

    Returnerar (text, kapad). max_tecken <= 0 betyder att texten utelämnas.
    """
    if not text:
        return text, False
    if max_tecken <= 0:
        return None, True
    if len(text) <= max_tecken:
        return text, False
    kapad = text[:max_tecken].rsplit(" ", 1)[0].rstrip(" ,;:")
    return kapad + "…", True
