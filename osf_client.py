"""Klientmodul för OSF Preprints (Open Science Framework).

OSF Preprints samlar ett stort antal ämnesspecifika preprint-servrar under
samma API — bl.a. SocArXiv (samhällsvetenskap), LawArXiv (juridik),
EdArXiv (utbildningsvetenskap) och många fler. Sökningen är begränsad:
API:et har ingen fritextsökning, bara filter[title][icontains] (titelsökning).

Utan token delar alla anrop en gräns på 100/timme (≈ ett anrop var 36:e
sekund); med DISCOVERY_OSF_API_NYCKEL (ett Personal Access Token) är
gränsen betydligt högre. Takten här är därför mycket försiktig som standard.

API-referens: https://developer.osf.io/
"""

from __future__ import annotations

import os

import requests

import kallkonfig
from kallhjalp import DiscoveryKallaFel, ny_taktbegransare

BASE_URL = os.environ.get("OSF_BASE_URL", "https://api.osf.io/v2").rstrip("/")
USER_AGENT = kallkonfig.user_agent("OSF_USER_AGENT")
TIMEOUT = float(os.environ.get("OSF_TIMEOUT", "30"))
API_NYCKEL = kallkonfig.api_nyckel("osf")

KALLA = "osf"

# Ämnesinriktade preprint-servrar på OSF som ligger närmast projektets
# prioriterade ämnen (samhällsvetenskap, juridik, utbildningsvetenskap).
# Överstyrs med parametern leverantorer i sok().
_STANDARD_LEVERANTORER = ["socarxiv", "lawarxiv", "edarxiv"]


class OsfFel(DiscoveryKallaFel):
    """Fel vid anrop mot OSF-API:et."""


_session = requests.Session()
_headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.api+json"}
if API_NYCKEL:
    _headers["Authorization"] = f"Bearer {API_NYCKEL}"
_session.headers.update(_headers)

# Utan nyckel: 100/timme delat av alla anropare, dvs ~36 s mellan anrop för
# att hålla god marginal. Med nyckel: klart högre kvot, 1 s räcker gott.
_vanta = ny_taktbegransare(1.0 if API_NYCKEL else 36.0)


def _hamta(path: str, params: dict) -> dict:
    _vanta()
    try:
        svar = _session.get(f"{BASE_URL}{path}", params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise OsfFel(f"Kunde inte nå OSF ({BASE_URL}{path}): {type(exc).__name__}") from exc
    if svar.status_code == 429:
        raise OsfFel(
            "OSF avvisade anropet (429, för hög trafik). Utan "
            "DISCOVERY_OSF_API_NYCKEL delas en gräns på 100 anrop/timme av "
            "alla anropare — sätt en nyckel (Personal Access Token) för en "
            "egen, högre kvot."
        )
    if svar.status_code == 404:
        raise OsfFel("OSF hittar inte den angivna posten.")
    if svar.status_code != 200:
        raise OsfFel(f"OSF svarade {svar.status_code}: {svar.text[:300]}")
    try:
        return svar.json()
    except ValueError as exc:
        raise OsfFel(f"OSF gav ett svar som inte är JSON: {exc}") from exc


def _bar_doi(item: dict) -> str | None:
    attr = item.get("attributes", {})
    if attr.get("doi"):
        return attr["doi"]
    lank = (item.get("links") or {}).get("preprint_doi")
    if lank and "doi.org/" in lank:
        return lank.split("doi.org/")[-1]
    return None


def _forma(item: dict) -> dict:
    attr = item.get("attributes", {})
    doi = _bar_doi(item)
    lankar = item.get("links") or {}
    datum = attr.get("date_published") or attr.get("date_created") or ""
    ar = int(datum[:4]) if datum[:4].isdigit() else None

    return {
        "kalla": KALLA,
        "kalla_id": item.get("id"),
        "doi": doi,
        "titel": attr.get("title"),
        # OSF:s bidragsgivarnamn kräver ett separat anrop per träff
        # (embed=bibliographic_contributors ger bara relationer, inga namn
        # utan ytterligare ett nästlat embed) — utelämnas för att hålla
        # anropen få, i linje med källans egen, hårda hastighetsgräns.
        "forfattare": [],
        "ar": ar,
        "typ": "preprint",
        "url": lankar.get("html") or (f"https://doi.org/{doi}" if doi else None),
        "oa_lank": lankar.get("html"),
    }


def sok(
    q: str | None = None,
    *,
    limit: int = 20,
    leverantor: str | None = None,
) -> dict:
    """Söker OSF Preprints på titel (ingen fritextsökning i API:et).

    q          - text att matcha mot titeln (delsträng, skiftlägesokänsligt).
    limit      - max antal träffar (1-100, standard 20).
    leverantor - en enskild OSF-preprintserver, t.ex. "lawarxiv". Standard:
                 "socarxiv" (samhällsvetenskap). filter[provider] hos OSF
                 matchar bara exakt en leverantör per anrop — ingen
                 OR-lista (verifierat: en kommaseparerad lista gav noll
                 träffar utan felmeddelande, i stället för flera källor).
    """
    if not q:
        raise OsfFel("Ange en titeltext att söka på (q) — OSF har ingen fritextsökning.")
    params = {
        "filter[title][icontains]": q,
        "filter[provider]": leverantor or _STANDARD_LEVERANTORER[0],
        "page[size]": str(max(1, min(limit, 100))),
    }
    data = _hamta("/preprints/", params)
    traffar = [_forma(it) for it in data.get("data", [])]
    totalt = (data.get("links") or {}).get("meta", {}).get("total") if isinstance(data.get("links"), dict) else None
    if totalt is None:
        totalt = (data.get("meta") or {}).get("total")

    return {"kalla": KALLA, "totalt": totalt, "antal": len(traffar), "traffar": traffar}


def hamta(preprint_id: str) -> dict:
    """Läser en enskild preprint via dess OSF-id (t.ex. "8jmu2_v3")."""
    if not preprint_id or not preprint_id.strip():
        raise OsfFel("Tomt OSF-id angavs.")
    data = _hamta(f"/preprints/{preprint_id.strip()}/", {})
    item = data.get("data")
    if not item:
        raise OsfFel(f"Hittar ingen OSF-preprint med id '{preprint_id}'.")
    return _forma(item)
