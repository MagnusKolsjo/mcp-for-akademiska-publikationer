# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klientmodul för DiVA (Digitala Vetenskapliga Arkivet) export-API.

DiVA aggregerar publikationer från ~50 svenska lärosäten och myndigheter.
Modulen bygger på samma export.jsf-anrop, frågebygge (_bygg_params) och
CSV-tolkning (_bygg_rubrikindex, DivaKallaFel) som mcp-for-diva:s
mcp_server.py, kopierat och förenklat till bara sökdelen — ingen databas,
ingen PDF-hämtning eller textextraktion, och bara det kolumnurval discovery
faktiskt normaliserar till sitt gemensamma träffschema.

Ett giltigt svar från export.jsf är alltid CSV med en rubrikrad, även vid
noll träffar. Allt annat (en HTML-sida, ett tomt svar, en CSV utan de kända
rubrikerna) betyder att källan har ändrats — DivaKallaFel, aldrig tomma
träffar som ser ut som ett riktigt sökresultat.

API-referens: DiVA har inget publicerat REST-API; export.jsf är portalens
egen CSV-export, verifierad mot faktiska svar (se mcp-for-diva).
"""

from __future__ import annotations

import csv
import io
import json
import os

import requests

import kallkonfig
from kallhjalp import DiscoveryKallaFel, ny_taktbegransare, ren_text

BASE_URL = os.environ.get("DIVA_BASE_URL", "https://www.diva-portal.org/smash/export.jsf")
# DiVA:s WAF blockerar (403) User-Agent-strängar som *börjar* med "Discovery"
# — verifierat: "Discovery-Test/1.0" gav 403, "XDiscoveryX/1.0" och
# "Foo-Discovery-Bar/1.0" gav 302 (ordet är alltså inte förbjudet i sig,
# bara som första token). Modulens standardvärde undviker därför den
# delade kallkonfig-standarden och börjar med källans eget projektnamn
# i stället; DIVA_USER_AGENT överstyr som vanligt.
USER_AGENT = os.environ.get(
    "DIVA_USER_AGENT",
    "MagnusKolsjo-Discovery-MCP/0.1 (+https://github.com/MagnusKolsjo/discovery-mcp)",
)
TIMEOUT = float(os.environ.get("DIVA_TIMEOUT", "30"))

KALLA = "diva"

# Internt fältnamn -> möjliga CSV-rubriker (gement). Bara det urval som
# discoveryhubbens gemensamma schema behöver — se mcp-for-diva för
# det fulla kolumnschemat (epistemisk status, handledare, examinator m.m.),
# som inte tas med här.
_KOLUMN_ALIAS: dict[str, list[str]] = {
    "diva_id": ["pid", "postid", "post id"],
    "forfattare": ["name", "författare", "author", "authors"],
    "titel": ["title", "titel"],
    "publikationstyp": ["publicationtype", "publikationstyp", "publication type", "type", "typ"],
    "ar": ["year", "år"],
    "doi": ["doi"],
    "urn": ["nbn", "urn:nbn", "urn", "uri"],
    "sammanfattning": ["abstract", "sammanfattning"],
    "fulltext_url": ["fulltextlink", "länk till fulltext", "fulltext url", "link to fulltext"],
    "fri_fulltext": ["freefulltext", "fri fulltext", "free fulltext", "open access"],
}

# Utan dessa kolumner går varken id eller titel att läsa ut ur en rad.
_OBLIGATORISKA_KOLUMNER: dict[str, str] = {"diva_id": "PID", "titel": "Title"}


class DivaFel(DiscoveryKallaFel):
    """Fel vid anrop mot DiVA export-API:et."""


class DivaKallaFel(DivaFel):
    """DiVA svarade, men inte med den CSV-export klienten är byggd för."""


_session = requests.Session()
_session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "text/csv,text/plain,*/*",
    "Referer": "https://www.diva-portal.org/",
})

# Ingen publicerad hastighetsgräns; en försiktig, egen takt är gott skick
# mot en portal utan dedikerat API.
_vanta = ny_taktbegransare(1.0)


def _bygg_rubrikindex(rubriker: list[str]) -> dict[str, str]:
    """Bygger ett index: internt fältnamn -> faktisk CSV-rubrik."""
    index: dict[str, str] = {}
    rubriker_gem = {r.lower(): r for r in rubriker}
    for internt, alias_lista in _KOLUMN_ALIAS.items():
        for alias in alias_lista:
            if alias in rubriker_gem:
                index[internt] = rubriker_gem[alias]
                break
    return index


def _val(rad: dict, rubrikindex: dict, falt: str, default: str = "") -> str:
    rubrik = rubrikindex.get(falt)
    if not rubrik:
        return default
    return (rad.get(rubrik) or "").strip()


def _kontrollera_csv_svar(svar: requests.Response, text: str) -> None:
    """Kastar DivaKallaFel om svaret från export.jsf inte ser ut som CSV."""
    innehallstyp = svar.headers.get("content-type", "").lower()
    borjan = text.lstrip()[:200].lower()
    ar_html = "html" in innehallstyp or borjan.startswith("<") or "<html" in borjan
    if ar_html:
        raise DivaKallaFel(
            "DiVA svarade med en webbsida i stället för CSV-export. Källan kan "
            "ha bytt plattform, eller export.jsf kan vara tillfälligt ur drift."
        )
    if not text.strip():
        raise DivaKallaFel(
            "DiVA svarade med ett tomt svar utan kolumnrubriker. export.jsf ger "
            "normalt en rubrikrad även utan träffar, så ett helt tomt svar tyder "
            "på ett tillfälligt fel eller att källan har bytt plattform."
        )


def _forma_rad(rad: dict, rubrikindex: dict) -> dict | None:
    diva_id = _val(rad, rubrikindex, "diva_id")
    if not diva_id:
        return None
    # PID-kolumnen är bara det numeriska id:t (t.ex. "1573847"); DiVA:s
    # egen kanoniska form och det id andra verktyg (t.ex. urn.kb.se) tar
    # emot är "diva2:1573847".
    if diva_id.isdigit():
        diva_id = f"diva2:{diva_id}"

    forfattare_raw = _val(rad, rubrikindex, "forfattare")
    # DiVA:s Name-fält radar upp "Efternamn, Förnamn (Lärosäte); Efternamn2, ..."
    forfattare = [
        {"namn": del_.split("(")[0].strip()}
        for del_ in forfattare_raw.split(";") if del_.strip()
    ]

    ar_str = _val(rad, rubrikindex, "ar")
    ar = int(ar_str) if ar_str.isdigit() else None

    doi = _val(rad, rubrikindex, "doi") or None
    urn = _val(rad, rubrikindex, "urn")
    fulltext_url = _val(rad, rubrikindex, "fulltext_url")
    fri_fulltext = _val(rad, rubrikindex, "fri_fulltext").lower() in ("yes", "ja", "true", "1", "x", "✓")

    url = f"https://urn.kb.se/resolve?urn={urn}" if urn else f"https://www.diva-portal.org/smash/record.jsf?pid={diva_id}"

    return {
        "kalla": KALLA,
        "kalla_id": diva_id,
        "doi": doi,
        "titel": _val(rad, rubrikindex, "titel") or None,
        "forfattare": forfattare,
        "ar": ar,
        "typ": _val(rad, rubrikindex, "publikationstyp") or None,
        "url": url,
        "oa_lank": fulltext_url or (url if fri_fulltext else None),
        "sammanfattning": ren_text(_val(rad, rubrikindex, "sammanfattning")),
    }


def _bygg_params(
    q: str,
    *,
    rows: int,
    fran_ar: int | None,
    till_ar: int | None,
    oppen_tillgang: bool | None,
) -> dict:
    """DiVA-sökparametrar: fritext och årsintervall i en AND-grupp (samma
    aq-struktur som mcp-for-diva:s _bygg_params)."""
    and_villkor: list[dict] = [{"freeText": q}]
    if fran_ar and till_ar:
        and_villkor.append({"dateIssued": {"from": str(int(fran_ar)), "to": str(int(till_ar))}})
    p: dict = {
        "format": "csvall2",
        "searchtype": "all",
        "noOfRows": rows,
        "aq": json.dumps([and_villkor]),
        "aqe": "[]",
        "af": "[]",
        "aq2": "[[]]",
    }
    if oppen_tillgang:
        p["onlyFullText"] = "true"
    return p


def _hamta_export(params: dict) -> list[dict]:
    _vanta()
    try:
        svar = _session.get(BASE_URL, params=params, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException as exc:
        raise DivaFel(f"Kunde inte nå DiVA ({BASE_URL}): {type(exc).__name__}") from exc

    if svar.status_code in (404, 410):
        raise DivaKallaFel(
            f"DiVA:s export.jsf svarade HTTP {svar.status_code} — adressen finns inte "
            "längre. Källan kan ha bytt plattform."
        )
    if svar.status_code >= 400:
        raise DivaFel(f"DiVA svarade {svar.status_code}. Försök igen om en stund.")

    text = svar.content.decode("utf-8-sig", errors="replace")
    _kontrollera_csv_svar(svar, text)

    forsta_rad = text.split("\n")[0] if "\n" in text else text[:500]
    separator = ";" if forsta_rad.count(";") > forsta_rad.count(",") else ","
    lasare = csv.DictReader(io.StringIO(text), delimiter=separator)
    rubriker = list(lasare.fieldnames or [])
    rubrikindex = _bygg_rubrikindex(rubriker)

    saknade = [namn for falt, namn in _OBLIGATORISKA_KOLUMNER.items() if falt not in rubrikindex]
    if saknade:
        raise DivaKallaFel(
            "DiVA:s CSV-export saknar kolumnrubriker klienten bygger på "
            f"(saknas: {', '.join(saknade)}). Källan kan ha bytt exportformat."
        )

    poster = []
    for rad in lasare:
        post = _forma_rad(rad, rubrikindex)
        if post:
            poster.append(post)
    return poster


def sok(
    q: str | None = None,
    *,
    limit: int = 20,
    fran_ar: int | None = None,
    till_ar: int | None = None,
    oppen_tillgang: bool | None = None,
) -> dict:
    """Söker DiVA via export.jsf.

    q              - fritextfråga.
    limit          - max antal träffar (1-200, standard 20).
    fran_ar/till_ar- utgivningsårsintervall (kräver båda för att slå igenom).
    oppen_tillgang - True filtrerar till poster med fri fulltext
                     (onlyFullText). False stöds inte separat av DiVA.
    """
    if not q:
        raise DivaFel("Ange en fritextfråga (q).")
    rows = max(1, min(limit, 200))
    params = _bygg_params(q, rows=rows, fran_ar=fran_ar, till_ar=till_ar, oppen_tillgang=oppen_tillgang)
    traffar = _hamta_export(params)
    return {"kalla": KALLA, "totalt": None, "antal": len(traffar), "traffar": traffar[:rows]}


def hamta(diva_id: str) -> dict:
    """Läser en enskild post via dess DiVA-id (t.ex. "diva2:833794").

    DiVA har inget dedikerat läs-API; export.jsf filtrerat på id återanvänds.
    """
    if not diva_id or not diva_id.strip():
        raise DivaFel("Tomt DiVA-id angavs.")
    naket = diva_id.strip()
    params = {
        "format": "csvall2",
        "searchtype": "all",
        "noOfRows": 1,
        "aq": json.dumps([[{"id": naket}]]),
        "aqe": "[]",
        "af": "[]",
        "aq2": "[[]]",
    }
    traffar = _hamta_export(params)
    if not traffar:
        raise DivaFel(f"Hittar ingen DiVA-post med id '{diva_id}'.")
    return traffar[0]
