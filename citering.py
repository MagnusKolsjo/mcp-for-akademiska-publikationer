# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Referenser för rapporter och presentationer: formaterad text, BibTeX, RIS.

Poster med DOI formateras av doi.org själv (content negotiation): Crossref
och DataCite renderar referensen ur förlagets egna metadata med
citeproc-stilarna från Citation Style Language — samma motor som Zotero och
Mendeley använder, och över 10 000 stilar. Det ger en korrekt referens utan
att den här servern behöver bära stilfiler.

Poster utan DOI (katalogposter i Libris, många DiVA- och HAL-poster)
formateras här, ur postens egna metadata. Det räcker till en korrekt APA-
eller Harvard-referens och till BibTeX/RIS/CSL-JSON för import i ett
referenshanteringsprogram, men inte till godtyckliga CSL-stilar.

API-referens: https://citation.doi.org/docs.html
"""

from __future__ import annotations

import json
import os
import re

import requests

import kallkonfig
from kallhjalp import DiscoveryKallaFel, ny_taktbegransare, ren_text

DOI_BASE_URL = os.environ.get("DOI_BASE_URL", "https://doi.org").rstrip("/")
TIMEOUT = float(os.environ.get("DOI_TIMEOUT", "30"))
USER_AGENT = kallkonfig.user_agent("DOI_USER_AGENT", med_kontakt=True)

# Format som inte är CSL-stilar. Allt annat tolkas som ett CSL-stilnamn
# (t.ex. "apa", "harvard-cite-them-right", "chicago-author-date", "ieee",
# "vancouver", "modern-language-association"); se
# https://github.com/citation-style-language/styles för hela listan.
_MASKINFORMAT = {
    "bibtex": "application/x-bibtex",
    "ris": "application/x-research-info-systems",
    "csl-json": "application/vnd.citationstyles.csl+json",
}

# Stilar som kan formateras här när posten saknar DOI.
_EGNA_STILAR = {"apa", "harvard", "harvard-cite-them-right"}


class CiteringFel(DiscoveryKallaFel):
    """Fel vid formatering av en referens."""


_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})
_vanta = ny_taktbegransare(0.2)


def via_doi(doi: str, format: str = "apa", sprak: str = "sv-SE") -> str:
    """Formaterar en referens via doi.org. Kastar CiteringFel vid problem."""
    format = (format or "apa").strip().lower()
    accept = _MASKINFORMAT.get(format) or f"text/x-bibliography; style={format}; locale={sprak}"
    _vanta()
    try:
        svar = _session.get(f"{DOI_BASE_URL}/{doi}", headers={"Accept": accept}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise CiteringFel(f"Kunde inte nå doi.org: {type(exc).__name__}") from exc

    if svar.status_code == 404:
        raise CiteringFel(f"doi.org känner inte till DOI:n '{doi}'.")
    if svar.status_code == 406:
        raise CiteringFel(
            f"Formatet '{format}' finns inte hos doi.org. Använd ett CSL-stilnamn "
            "(t.ex. apa, harvard-cite-them-right, chicago-author-date, ieee, "
            "vancouver) eller bibtex, ris, csl-json. DOI:er från registrerare "
            "utanför Crossref och DataCite stöder ibland bara vissa format."
        )
    if svar.status_code != 200:
        raise CiteringFel(f"doi.org svarade {svar.status_code}: {svar.text[:200]}")

    svar.encoding = "utf-8"
    text = svar.text.strip()
    if format == "csl-json":
        return text
    if format in _MASKINFORMAT:
        return text
    # DataCite levererar referensen med HTML-kursiv och entiteter.
    return ren_text(text) or text


# ---------------------------------------------------------------------------
# Formatering ur egna metadata (poster utan DOI)
# ---------------------------------------------------------------------------

def _delat_namn(namn: str) -> tuple[str, str]:
    """(efternamn, förnamn) ur "Efternamn, Förnamn" eller "Förnamn Efternamn"."""
    namn = " ".join((namn or "").split())
    if "," in namn:
        efter, _, for_ = namn.partition(",")
        return efter.strip(), for_.strip()
    delar = namn.rsplit(" ", 1)
    return (delar[1], delar[0]) if len(delar) == 2 else (namn, "")


def _initialer(fornamn: str) -> str:
    return " ".join(f"{d[0]}." for d in re.split(r"[\s]+", fornamn) if d and d[0].isalpha())


def _apa_forfattare(forfattare: list[dict]) -> str:
    namn = []
    for f in forfattare[:20]:
        efter, for_ = _delat_namn(f.get("namn", ""))
        namn.append(f"{efter}, {_initialer(for_)}".rstrip(", "))
    if not namn:
        return ""
    if len(namn) == 1:
        return namn[0]
    return ", ".join(namn[:-1]) + ", & " + namn[-1]


def _harvard_forfattare(forfattare: list[dict]) -> str:
    namn = []
    for f in forfattare:
        efter, for_ = _delat_namn(f.get("namn", ""))
        namn.append(f"{efter}, {_initialer(for_)}".rstrip(", "))
    if not namn:
        return ""
    if len(namn) > 3:
        return f"{namn[0]} m.fl."
    if len(namn) == 1:
        return namn[0]
    return ", ".join(namn[:-1]) + " och " + namn[-1]


def _csl_typ(typ: str | None) -> str:
    typ = (typ or "").lower()
    if any(o in typ for o in ("thesis", "avhandling", "uppsats", "examensarbete")):
        return "thesis"
    if "book" in typ or "monograph" in typ or typ in ("text", "print", "bok"):
        return "book"
    if "report" in typ or "rapport" in typ:
        return "report"
    if "preprint" in typ:
        return "article"
    if "article" in typ or "artikel" in typ:
        return "article-journal"
    return "document"


def ur_post(post: dict, format: str = "apa") -> str:
    """Formaterar en referens ur det gemensamma träffschemat."""
    format = (format or "apa").strip().lower()
    titel = (post.get("titel") or "").strip()
    ar = post.get("ar")
    forfattare = post.get("forfattare") or []
    # Libris-URI:er pekar på instansen med fragmentet "#it"; i en referens
    # ska adressen gå till postens webbsida.
    url = (post.get("url") or "").removesuffix("#it")
    typ = _csl_typ(post.get("typ"))

    if format == "csl-json":
        return json.dumps({
            "id": post.get("kalla_id"),
            "type": typ,
            "title": titel,
            "author": [
                {"family": e, "given": g} for e, g in (_delat_namn(f.get("namn", "")) for f in forfattare)
            ],
            "issued": {"date-parts": [[ar]]} if ar else None,
            "URL": url or None,
        }, ensure_ascii=False)

    if format == "bibtex":
        bibtyp = {"thesis": "phdthesis", "book": "book", "report": "techreport",
                  "article-journal": "article"}.get(typ, "misc")
        nyckel = re.sub(r"\W", "", _delat_namn(forfattare[0]["namn"])[0] if forfattare else "okand") + str(ar or "")
        falt = {
            "title": titel,
            "author": " and ".join(f.get("namn", "") for f in forfattare),
            "year": str(ar) if ar else "",
            "url": url,
        }
        rader = ",\n".join(f"  {k} = {{{v}}}" for k, v in falt.items() if v)
        return f"@{bibtyp}{{{nyckel},\n{rader}\n}}"

    if format == "ris":
        ristyp = {"thesis": "THES", "book": "BOOK", "report": "RPRT",
                  "article-journal": "JOUR"}.get(typ, "GEN")
        rader = [f"TY  - {ristyp}", f"TI  - {titel}"]
        rader += [f"AU  - {f.get('namn')}" for f in forfattare if f.get("namn")]
        if ar:
            rader.append(f"PY  - {ar}")
        if url:
            rader.append(f"UR  - {url}")
        rader.append("ER  - ")
        return "\n".join(rader)

    if format == "apa":
        delar = [_apa_forfattare(forfattare) or titel, f"({ar or 'u.å.'})."]
        if forfattare:
            delar.append(f"{titel}.")
        if url:
            delar.append(url)
        return " ".join(d for d in delar if d)

    if format in ("harvard", "harvard-cite-them-right"):
        delar = [_harvard_forfattare(forfattare) or titel, f"({ar or 'u.å.'})"]
        if forfattare:
            delar.append(f"{titel}.")
        if url:
            delar.append(f"Tillgänglig vid: {url}.")
        return " ".join(d for d in delar if d)

    raise CiteringFel(
        f"Posten saknar DOI, och formatet '{format}' kan då inte formateras. "
        f"Utan DOI stöds: {', '.join(sorted(_EGNA_STILAR | set(_MASKINFORMAT)))}."
    )
