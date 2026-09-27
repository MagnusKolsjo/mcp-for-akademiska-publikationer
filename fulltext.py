# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Fulltext för arbetsbiblioteket: hitta en öppen kopia och extrahera texten.

Kandidaterna prövas i tur och ordning tills en ger text:

  1. Källans egen fulltext — arXiv-PDF, Europe PMC:s JATS-XML för
     PMC-artiklar, DiVA:s fulltextlänk och Publiceras artikel-PDF.
  2. Öppna kopior för DOI:n enligt Unpaywall (om aktiv) och OpenAlex.
  3. Postens egen oa_lank.

En landningssida (HTML) följs bara vidare via metataggen citation_pdf_url,
som förlag, OJS-tidskrifter och arkiv lägger ut för just det här syftet.
Övrig HTML används inte som fulltext — den är oftast en sida med abstract,
menyer och kakbanner, och att spara den som "fulltext" vore missvisande.

Adresserna kommer alltid ur källornas metadata, aldrig från anroparen:
servern hämtar inte godtyckliga URL:er.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests

import diva_client
import kallkonfig
from kallhjalp import DiscoveryKallaFel, ny_taktbegransare

TIMEOUT = float(os.environ.get("DISCOVERY_FULLTEXT_TIMEOUT", "60"))
MAX_BYTE = int(float(os.environ.get("DISCOVERY_FULLTEXT_MAX_MB", "40")) * 1024 * 1024)
USER_AGENT = kallkonfig.user_agent(med_kontakt=True)

# DiVA:s brandvägg avvisar User-Agent som börjar med "Discovery"; dess
# filer hämtas därför med DiVA-klientens egen identitet.
_DIVA_UA = diva_client.USER_AGENT

_vanta = ny_taktbegransare(1.0)


class FulltextFel(DiscoveryKallaFel):
    """Fel vid hämtning eller extraktion av fulltext."""


@dataclass
class Fulltext:
    text: str
    url: str
    format: str                      # "pdf" eller "jats"
    sidor: list[int] = field(default_factory=list)  # teckenposition där varje sida börjar
    licens: str | None = None


@dataclass
class Kandidat:
    url: str
    licens: str | None = None
    user_agent: str | None = None


# ---------------------------------------------------------------------------
# Extraktion
# ---------------------------------------------------------------------------

def _stada(text: str) -> str:
    """Avstavning över radslut ihopfogad, radbrytningar inom stycken bort."""
    text = text.replace("\r", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)          # av-\nstavning
    text = re.sub(r"(?<![.!?:\n])\n(?!\n)", " ", text)     # radbrytning mitt i en mening
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _ur_pdf(innehall: bytes) -> tuple[str, list[int]]:
    import pymupdf  # lat import: behövs bara när biblioteket används

    delar: list[str] = []
    sidor: list[int] = []
    langd = 0
    with pymupdf.open(stream=innehall, filetype="pdf") as dok:
        for sida in dok:
            text = _stada(sida.get_text("text"))
            sidor.append(langd)
            delar.append(text)
            langd += len(text) + 2
    return "\n\n".join(delar), sidor


def _ur_jats(innehall: bytes) -> str:
    """Brödtexten ur en JATS-artikel (Europe PMC): rubriker och stycken.

    XML hämtad från nätet med egna entitetsdeklarationer avvisas: både
    XXE och entitetsexpansion ("billion laughs") kräver dem, och en
    JATS-artikel behöver dem inte (DOCTYPE med extern DTD är ofarligt —
    expat hämtar den inte)."""
    if b"<!ENTITY" in innehall:
        raise FulltextFel("XML:en innehåller entitetsdeklarationer och tolkas inte.")
    rot = ET.fromstring(innehall)
    body = rot.find(".//body")
    if body is None:
        return ""
    delar: list[str] = []
    for elem in body.iter():
        if elem.tag in ("title", "p"):
            text = " ".join("".join(elem.itertext()).split())
            if text:
                delar.append(text)
    return "\n\n".join(delar)


def _licens_ur_html(html: str) -> str | None:
    m = re.search(r"creativecommons\.org/(licenses|publicdomain)/([a-z\-]+)/?([\d.]+)?", html, re.I)
    if not m:
        return None
    return f"cc-{m.group(2).lower()}" + (f"-{m.group(3)}" if m.group(3) else "")


def _pdf_ur_html(html: str) -> str | None:
    for monster in (
        r'<meta[^>]+name="citation_pdf_url"[^>]+content="([^"]+)"',
        r'<meta[^>]+content="([^"]+)"[^>]+name="citation_pdf_url"',
    ):
        m = re.search(monster, html, re.I)
        if m:
            return m.group(1).replace("&amp;", "&")
    return None


# ---------------------------------------------------------------------------
# Hämtning
# ---------------------------------------------------------------------------

_MAX_OMDIRIGERINGAR = 5


def _kontrollera_adress(url: str) -> None:
    """Avvisar adresser som inte är publika http(s)-adresser.

    Adresserna kommer ur källornas metadata och ur hämtade sidor
    (citation_pdf_url) — innehåll som tredje part kan påverka. Utan kontroll
    kunde servern lockas att anropa interna tjänster (SSRF), t.ex.
    molnleverantörers metadatatjänst på 169.254.169.254, när den körs delat
    över http. Kontrollen görs för varje omdirigering."""
    delar = urlparse(url)
    if delar.scheme not in ("http", "https") or not delar.hostname:
        raise FulltextFel(f"Otillåten adress (bara http/https): {url[:120]}")
    try:
        adresser = {info[4][0] for info in socket.getaddrinfo(delar.hostname, delar.port or 443, proto=socket.IPPROTO_TCP)}
    except socket.gaierror as exc:
        raise FulltextFel(f"Kunde inte slå upp {delar.hostname}") from exc
    for adress in adresser:
        ip = ipaddress.ip_address(adress.split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise FulltextFel(f"Otillåten adress: {delar.hostname} pekar på en icke-publik adress ({ip}).")


def _get(url: str, user_agent: str | None) -> requests.Response:
    """GET med storleksgräns, där varje omdirigering kontrolleras för sig."""
    for _ in range(_MAX_OMDIRIGERINGAR + 1):
        _kontrollera_adress(url)
        _vanta()
        try:
            svar = requests.get(
                url,
                headers={"User-Agent": user_agent or USER_AGENT, "Accept": "application/pdf, application/xml, text/html;q=0.8"},
                timeout=TIMEOUT, stream=True, allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise FulltextFel(f"Kunde inte nå {url}: {type(exc).__name__}") from exc
        if svar.is_redirect and svar.headers.get("Location"):
            url = urljoin(url, svar.headers["Location"])
            svar.close()
            continue
        break
    else:
        raise FulltextFel(f"För många omdirigeringar från {url}")

    if svar.status_code != 200:
        raise FulltextFel(f"{url} svarade {svar.status_code}")
    langd = int(svar.headers.get("Content-Length") or 0)
    if langd > MAX_BYTE:
        raise FulltextFel(f"{url} är större än {MAX_BYTE // (1024 * 1024)} MB")
    innehall = b""
    for bit in svar.iter_content(64 * 1024):
        innehall += bit
        if len(innehall) > MAX_BYTE:
            raise FulltextFel(f"{url} är större än {MAX_BYTE // (1024 * 1024)} MB")
    svar._content = innehall
    return svar


def hamta(kandidat: Kandidat, *, djup: int = 0) -> Fulltext:
    """Hämtar och extraherar en kandidat. Kastar FulltextFel om den inte ger text."""
    svar = _get(kandidat.url, kandidat.user_agent)
    innehall = svar.content
    typ = (svar.headers.get("Content-Type") or "").lower()

    if innehall[:5] == b"%PDF-" or "pdf" in typ:
        try:
            text, sidor = _ur_pdf(innehall)
        except Exception as exc:  # trasig eller krypterad PDF
            raise FulltextFel(f"PDF:en från {svar.url} gick inte att läsa: {type(exc).__name__}") from exc
        if len(text) < 500:
            # Skannade PDF:er utan textlager ger nästan ingen text.
            raise FulltextFel(f"PDF:en från {svar.url} saknar textlager (skannad?)")
        return Fulltext(text=text, url=svar.url, format="pdf", sidor=sidor, licens=kandidat.licens)

    if "xml" in typ and b"<article" in innehall[:5000]:
        text = _ur_jats(innehall)
        if len(text) < 500:
            raise FulltextFel(f"XML:en från {svar.url} saknar brödtext")
        return Fulltext(text=text, url=svar.url, format="jats", licens=kandidat.licens)

    if "html" in typ and djup == 0:
        html = innehall.decode(svar.encoding or "utf-8", errors="replace")
        pdf = _pdf_ur_html(html)
        if pdf:
            licens = kandidat.licens or _licens_ur_html(html)
            return hamta(Kandidat(pdf, licens, kandidat.user_agent), djup=1)
        raise FulltextFel(f"{svar.url} är en webbsida utan länk till fulltext (citation_pdf_url)")

    raise FulltextFel(f"{svar.url} gav varken PDF eller artikel-XML ({typ or 'okänd typ'})")


def kandidater(post: dict, *, oa_uppslag) -> list[Kandidat]:
    """Kandidatadresser för en post i det gemensamma schemat, bästa först.

    oa_uppslag(doi) -> list[(url, licens)] slår upp öppna kopior för en DOI
    (providers.py kopplar in OpenAlex och Unpaywall)."""
    ut: list[Kandidat] = []
    kalla = post.get("kalla")
    kalla_id = str(post.get("kalla_id") or "")

    if kalla == "arxiv" and kalla_id:
        ut.append(Kandidat(f"https://arxiv.org/pdf/{kalla_id}", "arxiv"))
    if kalla == "europepmc" and kalla_id.upper().startswith("PMC:"):
        pmcid = kalla_id.split(":", 1)[1]
        ut.append(Kandidat(f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"))
    if kalla == "diva" and post.get("oa_lank"):
        ut.append(Kandidat(post["oa_lank"], None, _DIVA_UA))
    if kalla == "publicera" and post.get("url"):
        ut.append(Kandidat(post["url"]))

    if post.get("doi"):
        try:
            for url, licens in oa_uppslag(post["doi"]):
                ut.append(Kandidat(url, licens))
        except Exception:  # noqa: BLE001 — en misslyckad OA-uppslagning ska inte stoppa övriga kandidater
            pass
    if post.get("oa_lank"):
        ut.append(Kandidat(post["oa_lank"], None, _DIVA_UA if "diva-portal.org" in post["oa_lank"] else None))

    unika: dict[str, Kandidat] = {}
    for k in ut:
        unika.setdefault(k.url, k)
    return list(unika.values())


def hitta(post: dict, *, oa_uppslag) -> tuple[Fulltext | None, list[str]]:
    """Första kandidat som ger text: (fulltext eller None, försök med skäl)."""
    forsok: list[str] = []
    for kandidat in kandidater(post, oa_uppslag=oa_uppslag):
        try:
            return hamta(kandidat), forsok
        except FulltextFel as exc:
            forsok.append(str(exc))
    return None, forsok
