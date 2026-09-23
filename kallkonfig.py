"""Av/på-läge, nycklar och kontaktuppgift för de nya discovery-källorna.

Varje ny källa kan slås av eller på oberoende i .env, utan kodändring:

    DISCOVERY_<KALLA>_AKTIV       true/false (standard: true)
    DISCOVERY_<KALLA>_API_NYCKEL  källans egen API-nyckel, om den har en

Gemensamt för alla nya källor:

    DISCOVERY_KONTAKT_EPOST  e-post för källor som kräver kontaktuppgift
                              (Unpaywall, artighetspooler)
    DISCOVERY_USER_AGENT     standard-User-Agent för källor utan egen
                              *_USER_AGENT sedan tidigare

En källspecifik *_USER_AGENT (LIBRIS_USER_AGENT, CROSSREF_USER_AGENT,
DATACITE_USER_AGENT, ARXIV_USER_AGENT) som redan fanns innan det här
uppdraget har fortsatt företräde för sin källa — bakåtkompatibilitet, inte
ett nytt krav.

En källa som kräver e-post eller nyckel men saknar det inaktiveras
automatiskt (se `krav_saknas`); den registreras då inte som verktyg och
ingår inte i discovery_sok, och discovery_kallor förklarar varför.
"""

from __future__ import annotations

import os

KONTAKT_EPOST = os.environ.get("DISCOVERY_KONTAKT_EPOST", "").strip()

_STANDARD_USER_AGENT = os.environ.get(
    "DISCOVERY_USER_AGENT",
    "Discovery-MCP/0.1 (+https://github.com/MagnusKolsjo/discovery-mcp)",
)


def aktiv(kalla: str) -> bool:
    """True om DISCOVERY_<KALLA>_AKTIV inte uttryckligen är satt till falskt."""
    varde = os.environ.get(f"DISCOVERY_{kalla.upper()}_AKTIV", "true").strip().lower()
    return varde not in ("false", "0", "nej", "av")


def api_nyckel(kalla: str) -> str:
    """DISCOVERY_<KALLA>_API_NYCKEL, tom sträng om ingen är satt."""
    return os.environ.get(f"DISCOVERY_{kalla.upper()}_API_NYCKEL", "").strip()


def user_agent(befintlig_env: str | None = None) -> str:
    """User-Agent för en ny källa.

    befintlig_env - namnet på en källspecifik *_USER_AGENT-variabel som
    fanns innan det här uppdraget (t.ex. "ARXIV_USER_AGENT"). Om den är
    satt används den, för bakåtkompatibilitet. Annars DISCOVERY_USER_AGENT
    eller standardvärdet.
    """
    if befintlig_env:
        varde = os.environ.get(befintlig_env, "").strip()
        if varde:
            return varde
    return _STANDARD_USER_AGENT


def krav_saknas(kalla: str, *, kravs_epost: bool = False, kravs_nyckel: bool = False) -> str | None:
    """Returnerar en förklarande text om ett krav för källan inte är uppfyllt.

    None om alla krav (om några) är uppfyllda — källan kan då aktiveras.
    """
    saknar = []
    if kravs_epost and not KONTAKT_EPOST:
        saknar.append("DISCOVERY_KONTAKT_EPOST")
    if kravs_nyckel and not api_nyckel(kalla):
        saknar.append(f"DISCOVERY_{kalla.upper()}_API_NYCKEL")
    if not saknar:
        return None
    return f"kräver {' och '.join(saknar)}, som inte är satt"
