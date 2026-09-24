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

# Standardvärdet är projektets egen identitet (namn, version, projekt-URL)
# — ett medvetet val, inte en installatörsspecifik uppgift. Det som aldrig
# kodas in här är kontakt-e-post, nycklar eller annat som pekar ut en
# enskild installation; kontakt-e-posten läggs till separat i user_agent()
# nedan, bara om DISCOVERY_KONTAKT_EPOST är satt.
_STANDARD_USER_AGENT = os.environ.get(
    "DISCOVERY_USER_AGENT",
    "Discovery-MCP/0.1 (+https://github.com/MagnusKolsjo/discovery-mcp)",
)


_SANT = ("true", "1", "ja", "på")
_FALSKT = ("false", "0", "nej", "av")


def aktiv(kalla: str, *, standard: bool = True) -> bool:
    """DISCOVERY_<KALLA>_AKTIV som bool, annars `standard`.

    De flesta källor är på som standard (fungerar utan nyckel/registrering).
    En källa som bör vara avstängd tills installatören medvetet slår på den
    (t.ex. CORE — se dess klientmodul) anger standard=False.
    """
    varde = os.environ.get(f"DISCOVERY_{kalla.upper()}_AKTIV", "").strip().lower()
    if varde in _SANT:
        return True
    if varde in _FALSKT:
        return False
    return standard


def api_nyckel(kalla: str) -> str:
    """DISCOVERY_<KALLA>_API_NYCKEL, tom sträng om ingen är satt."""
    return os.environ.get(f"DISCOVERY_{kalla.upper()}_API_NYCKEL", "").strip()


def user_agent(befintlig_env: str | None = None, *, med_kontakt: bool = False) -> str:
    """User-Agent för en ny källa.

    befintlig_env - namnet på en källspecifik *_USER_AGENT-variabel som
    fanns innan det här uppdraget (t.ex. "ARXIV_USER_AGENT"). Om den är
    satt används den oförändrad, för bakåtkompatibilitet — kontakt-e-posten
    bakas då inte in (den variabelns eget värde styr helt).

    med_kontakt - om True och DISCOVERY_KONTAKT_EPOST är satt, bakas
    e-posten in i DISCOVERY_USER_AGENT/standardvärdet. Källor som ber om en
    identifierbar kontakt (t.ex. NVA, Unpaywall) sätter detta till True.
    Ingen kontaktuppgift läggs till om DISCOVERY_KONTAKT_EPOST inte är satt
    — installatören avgör själv om den vill identifiera sig.
    """
    if befintlig_env:
        varde = os.environ.get(befintlig_env, "").strip()
        if varde:
            return varde
    if med_kontakt and KONTAKT_EPOST:
        return f"{_STANDARD_USER_AGENT} (mailto:{KONTAKT_EPOST})"
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
