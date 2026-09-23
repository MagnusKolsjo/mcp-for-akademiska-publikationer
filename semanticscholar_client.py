"""Klientmodul för Semantic Scholars Graph API.

Semantic Scholar används här som berikningskälla för discovery_citeringar
(citeringsgrafen), inte i den enade sökningen discovery_sok — OpenAlex täcker
redan bred sökning och har egen, säkrare citeringsdata. Källan kräver
attribution och tillåter inte vidaredistribution av data i bulk; discovery
har ingen databas och lagrar inget, så det uppfylls redan av arkitekturen.

Utan nyckel delar alla anrop en global, hårt begränsad pool (dokumenterat
oförutsägbar — 429 är vanligt även vid enstaka anrop). Med
DISCOVERY_SEMANTICSCHOLAR_API_NYCKEL är takten 1 anrop/sekund.

API-referens: https://api.semanticscholar.org/api-docs/graph
"""

from __future__ import annotations

import os

import requests

import kallkonfig
from kallhjalp import DiscoveryKallaFel, ny_taktbegransare

BASE_URL = os.environ.get(
    "SEMANTICSCHOLAR_BASE_URL", "https://api.semanticscholar.org/graph/v1"
).rstrip("/")
USER_AGENT = kallkonfig.user_agent("SEMANTICSCHOLAR_USER_AGENT")
TIMEOUT = float(os.environ.get("SEMANTICSCHOLAR_TIMEOUT", "30"))
API_NYCKEL = kallkonfig.api_nyckel("semanticscholar")

KALLA = "semanticscholar"
ATTRIBUTION = "Data från Semantic Scholar (semanticscholar.org)."

_FALT = "title,year,externalIds,authors,venue,openAccessPdf,citationCount,publicationTypes"


class SemanticScholarFel(DiscoveryKallaFel):
    """Fel vid anrop mot Semantic Scholar-API:et."""


_session = requests.Session()
_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
if API_NYCKEL:
    _headers["x-api-key"] = API_NYCKEL
_session.headers.update(_headers)

# Utan nyckel: mycket försiktig takt — den delade poolen är hårt begränsad
# och 429 förekommer även vid låg trafik. Med nyckel: dokumenterat 1/s.
_vanta = ny_taktbegransare(1.0 if API_NYCKEL else 3.0)


def _hamta(path: str, params: dict | None = None) -> dict:
    _vanta()
    try:
        svar = _session.get(f"{BASE_URL}{path}", params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise SemanticScholarFel(f"Kunde inte nå Semantic Scholar ({BASE_URL}{path}): {exc}") from exc

    if svar.status_code == 429:
        raise SemanticScholarFel(
            "Semantic Scholar avvisade anropet (429, för hög trafik i den delade "
            "poolen). Sätt DISCOVERY_SEMANTICSCHOLAR_API_NYCKEL för en egen, "
            "stabilare kvot, eller försök igen senare."
        )
    if svar.status_code == 404:
        raise SemanticScholarFel("Semantic Scholar hittar inte den angivna posten.")
    if svar.status_code != 200:
        raise SemanticScholarFel(f"Semantic Scholar svarade {svar.status_code}: {svar.text[:300]}")
    try:
        return svar.json()
    except ValueError as exc:
        raise SemanticScholarFel(f"Semantic Scholar gav ett svar som inte är JSON: {exc}") from exc


def _forfattare(authors: list | None) -> list[dict]:
    return [{"namn": a["name"]} for a in (authors or []) if isinstance(a, dict) and a.get("name")]


def _forma(item: dict) -> dict:
    ext = item.get("externalIds") or {}
    doi = ext.get("DOI")
    oa = item.get("openAccessPdf") or {}
    typer = item.get("publicationTypes") or []
    return {
        "kalla": KALLA,
        "kalla_id": item.get("paperId"),
        "doi": doi,
        "titel": item.get("title"),
        "forfattare": _forfattare(item.get("authors")),
        "ar": item.get("year"),
        "typ": typer[0] if typer else None,
        "url": f"https://doi.org/{doi}" if doi else (
            f"https://www.semanticscholar.org/paper/{item.get('paperId')}" if item.get("paperId") else None
        ),
        "oa_lank": oa.get("url"),
        "citeringar": item.get("citationCount"),
    }


def _normalisera_id(id_eller_doi: str) -> str:
    """Semantic Scholar tar emot flera id-format: eget paperId, "DOI:...",
    "ARXIV:..." m.fl. En bar DOI ("10.xxx/yyy") görs om till "DOI:10.xxx/yyy";
    allt annat skickas vidare oförändrat."""
    ident = (id_eller_doi or "").strip()
    if ident.lower().startswith("10.") :
        return f"DOI:{ident}"
    return ident


def hamta(id_eller_doi: str) -> dict:
    """Läser en enskild post via Semantic Scholars id eller DOI."""
    if not (id_eller_doi or "").strip():
        raise SemanticScholarFel("Tomt id/DOI angavs.")
    ident = _normalisera_id(id_eller_doi)
    data = _hamta(f"/paper/{ident}", {"fields": _FALT})
    return _forma(data)


def citeringar(id_eller_doi: str, *, riktning: str = "citerande", limit: int = 20) -> dict:
    """Citeringsgrafen runt en post.

    riktning - "citerande" (/citations, verk som citerar denna post) eller
               "referenser" (/references, verk denna post citerar).
    """
    if riktning not in ("citerande", "referenser"):
        raise SemanticScholarFel("riktning måste vara 'citerande' eller 'referenser'.")

    ident = _normalisera_id(id_eller_doi)
    andpunkt = "citations" if riktning == "citerande" else "references"
    nyckel = "citingPaper" if riktning == "citerande" else "citedPaper"

    data = _hamta(
        f"/paper/{ident}/{andpunkt}",
        {"fields": _FALT, "limit": str(max(1, min(limit, 200)))},
    )
    poster = [d[nyckel] for d in data.get("data", []) if isinstance(d, dict) and d.get(nyckel)]
    traffar = [_forma(p) for p in poster]

    return {
        "kalla": KALLA,
        "id": ident,
        "riktning": riktning,
        "antal": len(traffar),
        "traffar": traffar,
    }
