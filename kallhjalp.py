"""Gemensamma byggblock för de nya discovery-källornas HTTP-anrop och takt.

Varje källa har sin egen klientmodul (t.ex. openalex_client.py), men alla
delar samma mönster: ett GET mot ett JSON- eller XML-API, en projektspecifik
User-Agent, en timeout och en strypning per källa så att flera samtidiga
verktygsanrop inte forcerar källans hastighetsgräns. Det här är byggblocken
källorna sätts ihop av — se respektive <namn>_client.py för fält och
frågespråk.

De äldre klientmodulerna (libris_client.py, crossref_client.py,
datacite_client.py, arxiv_client.py) använder inte den här modulen; de
behålls oförändrade och har varsitt eget, inline mönster sedan tidigare.
"""

from __future__ import annotations

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
