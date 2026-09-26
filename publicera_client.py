# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klientmodul för Publicera — KB:s OJS-plattform för svenska
vetenskapliga tidskrifter (publicera.kb.se).

Discovery har ingen egen databas, till skillnad från
MCP-servern mcp-for-kb-publicera (som synkar metadata till Postgres/SQLite). sok()
löser det genom att söka OpenAlex, begränsat till de ISSN som identifierats
för Publicera-tidskrifterna — se publicera_tidskrifter.json, som togs fram
genom att slå upp varje tidskriftsnamn i mcp-for-kb-publicera:s
tidskrifter.json (som saknar ISSN) mot OpenAlex /sources, med ett
likhetstest mot tidskriftsnamnet innan träffen accepterades. 46 av 55
tidskrifter fick en ISSN den vägen (se CHANGELOG); resten täcks inte av
sok() förrän de kompletteras manuellt eller via en annan metod (Publiceras
egen OAI-PMH per tidskrift, som mcp-for-kb-publicera använder).

hamta() går däremot äkta mot Publicera: en DOI följs (redirect) till sin
OJS-landningssida (https://publicera.kb.se/<spec>/article/view/<id>), vars
<id> är samma nummer som Publiceras OAI-identifierare använder
(oai:ojs.publicera.kb.se:article/<id> — verifierat mot en känd artikel),
och hela posten hämtas sedan med OAI-PMH GetRecord.

Publicera har botskyddet Anubis framför sajten. Det släpper igenom en
ärlig, identifierbar klient men utmanar webbläsarlika User-Agents — se
kallkonfig.user_agent(), som aldrig efterliknar en webbläsare.
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

import kallkonfig
import openalex_client
from kallhjalp import DiscoveryKallaFel, ny_taktbegransare

PUBLICERA_BASE = os.environ.get("PUBLICERA_BASE_URL", "https://publicera.kb.se").rstrip("/")
USER_AGENT = kallkonfig.user_agent("PUBLICERA_USER_AGENT")
TIMEOUT = float(os.environ.get("PUBLICERA_TIMEOUT", "30"))

KALLA = "publicera"

_TIDSKRIFTER_FIL = Path(__file__).parent / "publicera_tidskrifter.json"
NS_OAI = "http://www.openarchives.org/OAI/2.0/"
NS_DC = "http://purl.org/dc/elements/1.1/"
NS_OAI_DC = "http://www.openarchives.org/OAI/2.0/oai_dc/"

# Markörer som bara finns på Anubis egna sidor (inbäddade JSON-block och
# sökvägen till dess statiska filer) — samma mönster som mcp-for-kb-publicera.
_ANUBIS_MARKORER = (b"anubis_challenge", b"anubis_version", b"/.within.website/")


class PubliceraFel(DiscoveryKallaFel):
    """Fel vid anrop mot Publicera."""


class BotskyddFel(PubliceraFel):
    """Publicera svarade med botskyddet Anubis i stället för det begärda innehållet."""


def _ar_anubis(innehall: bytes) -> bool:
    borjan = innehall[:512].lstrip().lower()
    if not (borjan.startswith(b"<!doctype html") or borjan.startswith(b"<html")):
        return False
    return any(markor in innehall for markor in _ANUBIS_MARKORER)


_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})

# Ingen publicerad hastighetsgräns; en försiktig, egen takt mot en portal
# som redan har ett botskydd att ta hänsyn till.
_vanta = ny_taktbegransare(1.0)


def _las_tidskrifter() -> list[dict]:
    if not _TIDSKRIFTER_FIL.exists():
        return []
    return json.loads(_TIDSKRIFTER_FIL.read_text(encoding="utf-8"))


_TIDSKRIFTER = _las_tidskrifter()
_ISSN_TILL_SPEC = {t["issn"]: t["spec"] for t in _TIDSKRIFTER if t.get("issn")}
_ISSN_LISTA = list(_ISSN_TILL_SPEC)


def _http_get(url: str) -> bytes:
    """GET mot Publicera med projektets User-Agent. Kastar BotskyddFel om
    Anubis svarar, PubliceraFel vid övriga fel."""
    _vanta()
    try:
        svar = _session.get(url, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise PubliceraFel(f"Kunde inte nå Publicera ({url}): {type(exc).__name__}") from exc
    if _ar_anubis(svar.content):
        raise BotskyddFel(
            "Publicera svarade med botskyddet Anubis i stället för OAI-PMH-XML. "
            "Försök igen senare."
        )
    if svar.status_code != 200:
        raise PubliceraFel(f"Publicera svarade {svar.status_code} för {url}.")
    return svar.content


def _text_lista(meta: ET.Element, tag: str) -> list[str]:
    return [e.text.strip() for e in meta.findall(f"{{{NS_DC}}}{tag}") if e.text]


def _tolka_post(record: ET.Element) -> dict:
    header = record.find(f"{{{NS_OAI}}}header")
    oai_id = header.findtext(f"{{{NS_OAI}}}identifier") if header is not None else None

    meta = record.find(f".//{{{NS_OAI_DC}}}dc")
    if meta is None:
        raise PubliceraFel("Publicera-posten saknar metadata (oai_dc:dc).")

    identifierare = _text_lista(meta, "identifier")
    artikel_url = next(
        (u for u in identifierare if u.startswith("http") and "/article/view/" in u), None
    )
    doi = next((u for u in identifierare if u.startswith("10.") or "doi.org" in u), None)
    if doi and "doi.org" in doi:
        doi = doi.split("doi.org/")[-1]

    titlar = _text_lista(meta, "title")
    datum = _text_lista(meta, "date")
    ar = int(datum[0][:4]) if datum and datum[0][:4].isdigit() else None

    return {
        "kalla": KALLA,
        "kalla_id": oai_id,
        "doi": doi,
        "titel": titlar[0] if titlar else None,
        "forfattare": [{"namn": n} for n in _text_lista(meta, "creator")],
        "ar": ar,
        "typ": "article",
        "url": artikel_url,
        "oa_lank": artikel_url,
    }


def sok(
    q: str | None = None,
    *,
    limit: int = 20,
    fran_ar: int | None = None,
    till_ar: int | None = None,
) -> dict:
    """Söker Publicera-artiklar via OpenAlex, begränsat till kända Publicera-ISSN.

    Se modulens docstring för varför — discovery har ingen egen databas att
    söka i. Bara de 46 av 55 tidskrifter som har en identifierad ISSN
    (publicera_tidskrifter.json) täcks.
    """
    if not _ISSN_LISTA:
        raise PubliceraFel(
            "Ingen Publicera-tidskrift har en känd ISSN — publicera_tidskrifter.json "
            "saknas eller är tom."
        )
    try:
        svar = openalex_client.sok(
            q,
            limit=limit,
            fran_ar=fran_ar,
            till_ar=till_ar,
            filter={"primary_location.source.issn": "|".join(_ISSN_LISTA)},
        )
    except openalex_client.OpenAlexFel as exc:
        raise PubliceraFel(f"OpenAlex-sökningen (begränsad till Publicera-ISSN) misslyckades: {exc}") from exc

    traffar = [dict(t, kalla=KALLA) for t in svar.get("traffar", [])]
    return {"kalla": KALLA, "totalt": svar.get("totalt"), "antal": len(traffar), "traffar": traffar}


def hamta(doi: str) -> dict:
    """Läser en enskild Publicera-artikel äkta från källan, via dess DOI.

    DOI:n följs till sin OJS-landningssida för att hitta artikel-id:t, som
    sedan slås upp med OAI-PMH GetRecord. En DOI som inte pekar mot
    Publicera (t.ex. en DOI från en annan källa) ger ett tydligt fel.
    """
    naken = (doi or "").strip().removeprefix("https://doi.org/").removeprefix("doi:")
    if not naken:
        raise PubliceraFel("Tom DOI angavs.")

    try:
        svar = requests.head(
            f"https://doi.org/{naken}",
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        raise PubliceraFel(f"Kunde inte slå upp DOI:n hos doi.org: {type(exc).__name__}") from exc

    matchning = re.search(r"publicera\.kb\.se/([^/]+)/article/view/(\d+)", svar.url)
    if not matchning:
        raise PubliceraFel(f"DOI:n '{doi}' pekar inte mot en Publicera-artikel.")
    spec, artikel_id = matchning.group(1), matchning.group(2)

    identifierare = f"oai:ojs.publicera.kb.se:article/{artikel_id}"
    url = (
        f"{PUBLICERA_BASE}/{spec}/oai?verb=GetRecord&metadataPrefix=oai_dc"
        f"&identifier={identifierare}"
    )
    innehall = _http_get(url)
    try:
        root = ET.fromstring(innehall)
    except ET.ParseError as exc:
        raise PubliceraFel(f"Svaret för tidskriften '{spec}' är inte OAI-PMH-XML.") from exc

    fel_elem = root.find(f".//{{{NS_OAI}}}error")
    if fel_elem is not None:
        raise PubliceraFel(f"OAI-PMH-fel: {(fel_elem.text or '').strip()}")

    records = root.findall(f".//{{{NS_OAI}}}record")
    if not records:
        raise PubliceraFel(f"Hittar ingen post med id '{identifierare}' hos Publicera.")
    return _tolka_post(records[0])
