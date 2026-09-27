# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""MCP-server för discovery — sök publikationer och deras metadata.

Hubben samlar flera live sök-API:er för publikationer och forskningsutfall bakom
ett gemensamt MCP-gränssnitt. Varje källa är en självständig klientmodul; varje
verktygsanrop går mot källans API. Ingen synk och ingen lokal kopia av
källorna — de är redan färdigindexerade sök-API:er — men en frivillig
svarscache (svarscache.py) sparar svaren en kort tid.

Källor och verktyg:
  Libris (KB:s nationella bibliotekskatalog)
    libris_sok          - fritext- och filtersökning i katalogen
    libris_hamta        - läs en post (sammanfattning eller fullständig JSON-LD)
    libris_bestand      - vilka bibliotek som har ett verk
    libris_sla_upp_term - översätt fritext till id.kb.se-termer för filter
  Crossref (DOI:er för artiklar, böcker, konferensbidrag)
    cr_sok              - sök verk, med fält- och årsfilter
    cr_hamta            - läs ett verk via DOI
  DataCite (DOI:er för forskningsdata, programvara, preprints)
    dc_sok              - sök poster, med typ-, års- och utgivarfilter
    dc_hamta            - läs en post via DOI
  arXiv (preprints inom fysik, matematik, datavetenskap m.fl.)
    arxiv_sok           - sök preprints, med fältprefix, kategori och årsfilter
    arxiv_hamta         - läs en preprint via dess arXiv-id
  Enad sökning över flera källor (se providers.py för hela källregistret:
  Libris, OpenAlex, SwePub, DiVA, Publicera, NVA, OSF Preprints, Europe PMC,
  zbMATH Open, EconBiz, HAL, DOAJ och CORE (avstängd som standard) utöver
  Crossref/DataCite/arXiv ovan; Unpaywall och Semantic Scholar används bara
  som berikningskällor för discovery_oa_lank/discovery_citeringar)
    discovery_sok       - sök flera källor samtidigt, sammanslaget och deduplicerat
    discovery_hamta     - läs en enskild post från en namngiven källa
    discovery_kallor    - lista alla källor: aktiva, filterstöd, avstängningsskäl
    discovery_oa_lank   - öppen tillgång-länk för en DOI (OpenAlex + Unpaywall)
    discovery_citeringar- citeringsgraf för en post (OpenAlex + Semantic Scholar)
    discovery_citera    - färdig referens (APA, Harvard m.fl.) eller BibTeX/RIS
    discovery_expandera - vilka språk en fråga bör sökas på, och varianterna

Nya källor kopplas in via providers.py — se den modulen för mönstret. Varje
källa kan slås av/på oberoende via DISCOVERY_<KALLA>_AKTIV i .env; för
Libris, Crossref, DataCite och arXiv styr det också om källans egna verktyg
registreras. En källa som kräver en nyckel eller kontakt-e-post den saknar
inaktiveras automatiskt — se discovery_kallor.

arXivs användarvillkor (https://info.arxiv.org/help/api/tou.html) ber om
erkännandet "Thank you to arXiv for use of its open access
interoperability." — se README:s avsnitt om datakällor.
"""

from pathlib import Path
from typing import Any, NotRequired, TypedDict

from dotenv import load_dotenv

# .env läses från skriptets egen mapp (inte processens cwd) eftersom
# MCP-klienten kan starta servern med en annan arbetskatalog. Måste ske innan
# klientmodulerna importeras, då de läser sina konfigurationsvärden (bas-URL,
# User-Agent, timeout) vid import.
load_dotenv(Path(__file__).parent / ".env")

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

import arxiv_client
import crossref_client
import datacite_client
import libris_client
import providers
import svarscache
from arxiv_client import ArxivFel
from crossref_client import CrossrefFel
from kallhjalp import DiscoveryKallaFel
from datacite_client import DataCiteFel
from libris_client import LibrisFel
from mcp_annotationer import CACHE_HINTAR, LASNING_DB, LASNING_EXTERN
from mcp_transport import starta

mcp = MCPServer(
    "discovery",
    instructions=(
        "Sök vetenskapligt material brett över många källor: internationella "
        "index (OpenAlex, Crossref, DataCite), preprints (arXiv, OSF), svenska "
        "och nordiska källor (Libris, SwePub, DiVA, Publicera, NVA) och "
        "ämneskällor (Europe PMC, zbMATH Open, EconBiz, HAL, DOAJ). "
        "discovery_sok väljer källor efter frågans språk, ämne och typ "
        "(breddar vid för få träffar), slår ihop träffarna på DOI och "
        "rangordnar efter relevans. Skicka gärna q per språk, "
        "{\"sv\": …, \"en\": …}, och amne/typ. discovery_kallor "
        "visar vilka källor som är aktiva och varför en källa kan vara "
        "avstängd. discovery_oa_lank hittar öppna kopior och "
        "discovery_citeringar citeringsgrafen. libris_sok/cr_sok/dc_sok/"
        "arxiv_sok har egna, rikare frågespråk för käll-specifika filter."
        " SPRÅK: svara alltid på det språk användaren skriver på. Titlar, "
        "abstract och citat återges på originalspråket (fältet sprak) med "
        "översättning intill, och det anges vilket originalspråket är; en "
        "översättning presenteras aldrig som citat. Erbjud originaltexten "
        "när den inte visats. discovery_expandera visar vilka språk en fråga "
        "bör sökas på; skicka då q per språk till discovery_sok."
    ),
    version="0.1.0",
    cache_hints=CACHE_HINTAR,
)


def _cachad(kalla: str, operation: str, parametrar: dict, kor):
    """Kör ett källanrop via svarscachen (se svarscache.py)."""
    svar, _ = svarscache.hamta_eller_kor(kalla, operation, parametrar, kor)
    return svar


def kallverktyg(kalla: str, **kwargs):
    """Registrerar ett käll-specifikt verktyg bara om källan är aktiv.

    DISCOVERY_<KALLA>_AKTIV=false tar bort källans egna verktyg ur
    verktygslistan helt, i stället för att de finns kvar och svarar med ett
    fel — en avstängd källa ska inte kosta klienten något. Samma läge styr
    källans deltagande i discovery_sok (providers.PROVIDERS)."""
    if providers.PROVIDERS[kalla]["aktiv"]:
        return mcp.tool(**kwargs)
    return lambda funktion: funktion


# ===========================================================================
# Typade returvärden
#
# Träffarnas inre fält varieras mycket mellan poster (historiska poster
# saknar ofta fält som moderna poster har), så varje träff typas som
# dict[str, Any] — bara skalet runt listan (totalt, offset/sida, antal)
# är stabilt nog för en strikt TypedDict. libris_hamta och *_hamta med
# format="full" returnerar dessutom olika fältmängder beroende på format,
# så de typas som öppna dict[str, Any].
# ===========================================================================

class LibrisSokResultat(TypedDict):
    totalt: int | None
    offset: int
    antal: int
    traffar: list[dict[str, Any]]


class LibrisBestandResultat(TypedDict):
    libris_id: str
    antal_bibliotek: int | None
    bibliotek: list[dict[str, Any]]


class LibrisTermerResultat(TypedDict):
    totalt: int | None
    antal: int
    termer: list[dict[str, Any]]


class KallSokResultat(TypedDict):
    kalla: str
    totalt: int | None
    offset: NotRequired[int]
    sida: NotRequired[int]
    antal: int
    traffar: list[dict[str, Any]]


class DiscoverySokResultat(TypedDict):
    fraga: str | dict[str, str]
    strategi: str
    breddad: bool
    fragade_kallor: dict[str, str]
    ej_fragade: dict[str, str]
    per_kalla: dict[str, dict[str, Any]]
    antal: int
    antal_sammanslagna: int
    fran_plats: int
    traffar: list[dict[str, Any]]
    begreppsexpansion: dict[str, Any]
    fortsattning: NotRequired[str]
    fel: NotRequired[dict[str, str]]


class DiscoveryKallorResultat(TypedDict):
    kallor: list[dict[str, Any]]
    berikningskallor: list[dict[str, Any]]
    svarscache: dict[str, Any]
    begreppsexpansion: dict[str, Any]
    amnen: dict[str, str]
    typer: list[str]


class ArxivTraff(TypedDict):
    id: str | None
    version: int | None
    titel: str | None
    forfattare: list[dict[str, Any]]
    sammanfattning: str | None
    publicerad: str | None
    uppdaterad: str | None
    kategorier: list[str]
    primarkategori: str | None
    doi: str | None
    journal_ref: str | None
    url_abs: str | None
    url_pdf: str | None


class ArxivSokResultat(TypedDict):
    kalla: str
    totalt: int | None
    start: int
    antal: int
    traffar: list[ArxivTraff]


class DiscoveryOaLankResultat(TypedDict):
    doi: str
    kallor: dict[str, dict[str, Any]]
    oa_lank: str | None


class DiscoveryCiteraResultat(TypedDict):
    doi: str | None
    format: str
    referens: str
    kalla: str


class DiscoveryCiteringarResultat(TypedDict):
    id: str
    riktning: str
    kallor: dict[str, dict[str, Any]]


# ===========================================================================
# Libris — Sveriges nationella bibliotekskatalog (KB)
# ===========================================================================

@kallverktyg("libris", title="Sök i Libris", annotations=LASNING_EXTERN)
def libris_sok(
    q: str = "",
    limit: int = 20,
    offset: int = 0,
    sort: str = "",
    typ: str = "",
    fran_ar: int = 0,
    till_ar: int = 0,
    amne_id: str = "",
    filter: dict | None = None,
) -> LibrisSokResultat:
    """Sök i Libris, Sveriges nationella bibliotekskatalog.

    Fritextsökningen stödjer Libris operatorer: mellanslag betyder OCH,
    `-` exkluderar, `|` betyder ELLER, `*` är prefixsökning, `""` matchar
    en hel fras och `()` styr operatorprioritet. Exempel: `tove (jansson | lindgren)`.

    Parametrar:
      q       - fritextfråga.
      limit   - max antal träffar (1-200, standard 20).
      offset  - antal träffar att hoppa över (paginering).
      sort    - sorteringsegenskap, t.ex. "publication.year" (äldst först)
                eller "-publication.year" (nyast först). Tom = relevans.
      typ     - begränsa till en verkstyp, t.ex. "Text", "NotatedMusic",
                "MovingImage", "Map", "SoundRecording".
      fran_ar - tidigaste utgivningsår (inklusive).
      till_ar - senaste utgivningsår (inklusive).
      amne_id - id.kb.se-URI för ett ämne, t.ex.
                "https://id.kb.se/term/sao/Rymdfart". Slå upp med
                libris_sla_upp_term först.
      filter  - dict med råa Libris-filter för avancerade behov, t.ex.
                {"instanceOf.language.@id": "https://id.kb.se/language/swe"}.
                Operator-prefix (not-, or-, and-, exists-, min-, max-,
                matches-) anges i nyckeln. Listvärde = ELLER på samma egenskap.

    Returnerar antal träffar totalt och en lista med formade träffar.
    Varje träff har libris_id som kan matas vidare till libris_hamta
    och libris_bestand.
    """
    sammanslaget: dict = dict(filter or {})
    if typ:
        sammanslaget.setdefault("instanceOf.@type", typ)
    if fran_ar:
        sammanslaget.setdefault("min-publication.year", fran_ar)
    if till_ar:
        sammanslaget.setdefault("max-publication.year", till_ar)
    if amne_id:
        sammanslaget.setdefault("instanceOf.subject.@id", amne_id)

    try:
        return _cachad(
            "libris", svarscache.SOK, {"q": q, "limit": limit, "offset": offset, "sort": sort, "filter": sammanslaget},
            lambda: libris_client.sok(
                q=q or None,
                limit=limit,
                offset=offset,
                sort=sort or None,
                filter=sammanslaget or None,
            ),
        )
    except LibrisFel as exc:
        raise ToolError(str(exc)) from exc


@kallverktyg("libris", title="Läs en Libris-post", annotations=LASNING_EXTERN)
def libris_hamta(libris_id: str, format: str = "kort") -> dict[str, Any]:
    """Läs en post i Libris via dess id.

    libris_id - postens id, antingen den korta nyckeln ("l4x7v34x34zz5lq")
                eller en full URL. Hämtas från libris_sok-träffar.
    format    - "kort" ger en kompakt sammanfattning (titel, upphov, år,
                ämnen, identifierare). "full" ger den fullständiga, inbäddade
                JSON-LD-posten (stor, men komplett).

    Returnerar postdata enligt valt format. Ett okänt libris_id ger ett fel
    som säger att posten inte hittades.
    """
    try:
        if format == "full":
            return _cachad(
                "libris", svarscache.POST, {"id": libris_id, "format": "full"},
                lambda: libris_client.hamta_post(libris_id),
            )
        return _cachad(
            "libris", svarscache.POST, {"id": libris_id, "format": "kort"},
            lambda: libris_client.sammanfatta_post(libris_id),
        )
    except LibrisFel as exc:
        raise ToolError(str(exc)) from exc


@kallverktyg("libris", title="Visa Libris-bestånd", annotations=LASNING_EXTERN)
def libris_bestand(libris_id: str, limit: int = 50) -> LibrisBestandResultat:
    """Visa vilka bibliotek som har ett verk (bestånd).

    libris_id - postens id eller URL för verket/instansen.
    limit     - max antal bibliotek att lista (1-200, standard 50).

    Returnerar antalet bibliotek totalt och en lista med sigel (kortkod)
    och namn för varje innehavande bibliotek.
    """
    try:
        return _cachad(
            "libris", svarscache.POST, {"bestand": libris_id, "limit": limit},
            lambda: libris_client.bestand(libris_id, limit=limit),
        )
    except LibrisFel as exc:
        raise ToolError(str(exc)) from exc


@kallverktyg("libris", title="Slå upp Libris-term", annotations=LASNING_EXTERN)
def libris_sla_upp_term(q: str, typ: str = "", limit: int = 10) -> LibrisTermerResultat:
    """Slå upp länkade termer på id.kb.se för att bygga precisa filter.

    Översätter fritext till id.kb.se-URI:er som kan matas in i libris_sok
    (parametern amne_id eller filter). Användbart för ämnen, genre/form,
    personer, språk och länder.

    q     - fritext att slå upp, t.ex. "rymdfart".
    typ   - valfri begränsning, t.ex. "Topic" (ämne), "GenreForm",
            "Person", "Language", "Country".
    limit - max antal termer (1-100, standard 10).

    Returnerar en lista med termer där varje term har ett id (URI) att
    använda som filtervärde.
    """
    try:
        return _cachad(
            "libris", svarscache.POST, {"term": q, "typ": typ, "limit": limit},
            lambda: libris_client.sla_upp_term(q, typ=typ or None, limit=limit),
        )
    except LibrisFel as exc:
        raise ToolError(str(exc)) from exc


# ===========================================================================
# Crossref — DOI:er för artiklar, böcker, konferensbidrag
# ===========================================================================

@kallverktyg("crossref", title="Sök i Crossref", annotations=LASNING_EXTERN)
def cr_sok(
    q: str = "",
    limit: int = 20,
    offset: int = 0,
    sort: str = "",
    typ: str = "",
    fran_ar: int = 0,
    till_ar: int = 0,
    forfattare: str = "",
    titel: str = "",
    tidskrift: str = "",
    filter: dict | None = None,
) -> KallSokResultat:
    """Sök publikationer i Crossref (DOI:er för vetenskaplig litteratur).

    Parametrar:
      q          - allmän fritextfråga över all metadata.
      limit      - max antal träffar (1-1000, standard 20).
      offset     - antal träffar att hoppa över (paginering, max 10000).
      sort       - sorteringsfält, t.ex. "published" (utgivningsdatum),
                   "is-referenced-by-count" (citeringar) eller "relevance".
      typ        - publikationstyp, t.ex. "journal-article", "book-chapter",
                   "proceedings-article", "dataset", "posted-content".
      fran_ar    - tidigaste utgivningsår (inklusive).
      till_ar    - senaste utgivningsår (inklusive).
      forfattare - fritext mot författarfältet.
      titel      - fritext mot titelfältet.
      tidskrift  - fritext mot tidskrift/källtitel.
      filter     - dict med råa Crossref-filter, t.ex.
                   {"has-abstract": "true", "issn": "2167-8359"}.

    Returnerar antal träffar totalt och normaliserade träffar; varje träff har
    en doi som kan matas vidare till cr_hamta.
    """
    try:
        return _cachad(
            "crossref", svarscache.SOK,
            dict(q=q, limit=limit, offset=offset, sort=sort, typ=typ, fran_ar=fran_ar, till_ar=till_ar,
                 forfattare=forfattare, titel=titel, tidskrift=tidskrift, filter=filter),
            lambda: crossref_client.sok(
                q=q or None,
                limit=limit,
                offset=offset,
                sort=sort or None,
                typ=typ or None,
                fran_ar=fran_ar or None,
                till_ar=till_ar or None,
                forfattare=forfattare or None,
                titel=titel or None,
                tidskrift=tidskrift or None,
                filter=filter or None,
            ),
        )
    except CrossrefFel as exc:
        raise ToolError(str(exc)) from exc


@kallverktyg("crossref", title="Läs ett verk i Crossref", annotations=LASNING_EXTERN)
def cr_hamta(doi: str, format: str = "kort") -> dict[str, Any]:
    """Läs ett verk i Crossref via dess DOI.

    doi    - DOI som naken identifierare, doi.org-URL eller doi:-sträng.
    format - "kort" ger en kompakt sammanfattning, "full" lägger till abstract,
             volym/nummer/sidor, ISSN/ISBN, ämnen och licens.

    Returnerar postdata enligt valt format. En okänd DOI ger ett fel som
    säger att verket inte hittades.
    """
    try:
        return _cachad(
            "crossref", svarscache.POST, {"id": doi, "format": format},
            lambda: crossref_client.hamta(doi, format=format),
        )
    except CrossrefFel as exc:
        raise ToolError(str(exc)) from exc


# ===========================================================================
# DataCite — DOI:er för forskningsdata, programvara, preprints
# ===========================================================================

@kallverktyg("datacite", title="Sök i DataCite", annotations=LASNING_EXTERN)
def dc_sok(
    q: str = "",
    limit: int = 20,
    sida: int = 1,
    sort: str = "",
    typ: str = "",
    fran_ar: int = 0,
    till_ar: int = 0,
    utgivare: str = "",
    klient_id: str = "",
    filter: dict | None = None,
) -> KallSokResultat:
    """Sök forskningsutfall i DataCite (DOI:er för data, programvara, preprints).

    Parametrar:
      q         - fritextfråga (Lucene-syntax, t.ex. `climate AND ocean`).
      limit     - antal träffar per sida (1-1000, standard 20).
      sida      - sidnummer (1-baserat). DataCite paginerar i sidor, inte offset.
      sort      - sorteringsfält, t.ex. "relevance", "created", "-created"
                  (nyast först), "published", "-published".
      typ       - generell resurstyp (gement), t.ex. "dataset", "text",
                  "software", "image", "collection".
      fran_ar   - tidigaste publikationsår (inklusive).
      till_ar   - senaste publikationsår (inklusive).
      utgivare  - exakt utgivarnamn att filtrera på.
      klient_id - DataCite-repositorium (klient-id), t.ex. "snd.figshare".
      filter    - dict med råa DataCite-frågeparametrar, t.ex.
                  {"provider-id": "snsd"}.

    Returnerar antal träffar totalt och normaliserade träffar; varje träff har
    en doi som kan matas vidare till dc_hamta.
    """
    try:
        return _cachad(
            "datacite", svarscache.SOK,
            dict(q=q, limit=limit, sida=sida, sort=sort, typ=typ, fran_ar=fran_ar, till_ar=till_ar,
                 utgivare=utgivare, klient_id=klient_id, filter=filter),
            lambda: datacite_client.sok(
                q=q or None,
                limit=limit,
                sida=sida,
                sort=sort or None,
                typ=typ or None,
                fran_ar=fran_ar or None,
                till_ar=till_ar or None,
                utgivare=utgivare or None,
                klient_id=klient_id or None,
                filter=filter or None,
            ),
        )
    except DataCiteFel as exc:
        raise ToolError(str(exc)) from exc


@kallverktyg("datacite", title="Läs en post i DataCite", annotations=LASNING_EXTERN)
def dc_hamta(doi: str, format: str = "kort") -> dict[str, Any]:
    """Läs en post i DataCite via dess DOI.

    doi    - DOI som naken identifierare, doi.org-URL eller doi:-sträng.
    format - "kort" ger en kompakt sammanfattning, "full" lägger till abstract,
             version, ämnen, format, rättigheter och statistik.

    Returnerar postdata enligt valt format. En okänd DOI ger ett fel som
    säger att posten inte hittades.
    """
    try:
        return _cachad(
            "datacite", svarscache.POST, {"id": doi, "format": format},
            lambda: datacite_client.hamta(doi, format=format),
        )
    except DataCiteFel as exc:
        raise ToolError(str(exc)) from exc


# ===========================================================================
# arXiv — preprints inom fysik, matematik, datavetenskap m.fl.
# ===========================================================================

@kallverktyg("arxiv", title="Sök på arXiv", annotations=LASNING_EXTERN)
def arxiv_sok(
    q: str = "",
    kategori: str = "",
    limit: int = 20,
    start: int = 0,
    sort_by: str = "",
    sort_order: str = "",
    fran_ar: int = 0,
    till_ar: int = 0,
) -> ArxivSokResultat:
    """Sök preprints på arXiv.

    Fritextfrågan stödjer arXivs fältprefix (ti:, au:, abs:, cat:, all:) och
    operatorerna AND, OR, ANDNOT, t.ex. `au:hinton AND cat:cs.LG`. Utan
    prefix söks alla fält (all:).

    Parametrar:
      q          - fritextfråga, gärna med fältprefix.
      kategori   - arXiv-kategori att begränsa till, t.ex. "cs.LG",
                   "astro-ph.CO". ANDas ihop med q om båda anges.
      limit      - max antal träffar (1-100, standard 20).
      start      - antal träffar att hoppa över (paginering).
      sort_by    - "relevance" (standard), "lastUpdatedDate" eller
                   "submittedDate".
      sort_order - "ascending" eller "descending".
      fran_ar    - tidigaste inskickningsår (inklusive).
      till_ar    - senaste inskickningsår (inklusive).

    Kräver minst en av q, kategori, fran_ar eller till_ar.

    Returnerar antal träffar totalt och en lista med träffar. Sammanfattningen
    är kapad till 500 tecken (markerad med "…"); hämta hela texten med
    arxiv_hamta(format="full"). Varje träff har ett id som kan matas vidare
    till arxiv_hamta.

    Thank you to arXiv for use of its open access interoperability.
    """
    try:
        return _cachad(
            "arxiv", svarscache.SOK,
            dict(q=q, kategori=kategori, limit=limit, start=start, sort_by=sort_by,
                 sort_order=sort_order, fran_ar=fran_ar, till_ar=till_ar),
            lambda: arxiv_client.sok(
                q=q or None,
                limit=limit,
                start=start,
                sort_by=sort_by or None,
                sort_order=sort_order or None,
                kategori=kategori or None,
                fran_ar=fran_ar or None,
                till_ar=till_ar or None,
            ),
        )
    except ArxivFel as exc:
        raise ToolError(str(exc)) from exc


@kallverktyg("arxiv", title="Läs en post på arXiv", annotations=LASNING_EXTERN)
def arxiv_hamta(arxiv_id: str, format: str = "kort") -> ArxivTraff:
    """Läs en preprint på arXiv via dess id.

    arxiv_id - arXiv-id med eller utan version, t.ex. "2101.00001",
               "2101.00001v2" eller "arXiv:2101.00001". Utan version ges
               den senaste.
    format   - "kort" kapar sammanfattningen till 500 tecken, "full" ger
               den oavkortad.

    Returnerar postdata enligt valt format. Ett okänt id ger ett fel som
    säger att posten inte hittades.
    """
    try:
        return _cachad(
            "arxiv", svarscache.POST, {"id": arxiv_id, "format": format},
            lambda: arxiv_client.hamta(arxiv_id, format=format),
        )
    except ArxivFel as exc:
        raise ToolError(str(exc)) from exc


# ===========================================================================
# Enad sökning över flera källor
# ===========================================================================

@mcp.tool(title="Sök flera källor samtidigt", annotations=LASNING_EXTERN)
def discovery_sok(
    q: str | dict[str, str] = "",
    amne: str = "",
    typ: str = "",
    strategi: str = "auto",
    kallor: list[str] | None = None,
    limit: int = 20,
    fran_ar: int = 0,
    till_ar: int = 0,
    oppen_tillgang: bool | None = None,
    land: str = "",
    sammanfattning_max: int = 300,
    fortsattning: str = "",
    synonymer: dict[str, list[str]] | None = None,
    sprak: list[str] | None = None,
    expandera: str = "auto",
    limit_per_kalla: int = 10,
    filter: dict[str, dict] | None = None,
) -> DiscoverySokResultat:
    """Sök vetenskapligt material i flera källor; sammanslaget och rangordnat.

    Servern väljer källor efter frågan i stället för att fråga alla: två
    breda index (OpenAlex, Crossref) plus de källor som passar frågans
    språk, ämne, dokumenttyp och land. Ger det för få träffar breddas
    sökningen till allmänna och nordiska källor; ämneskällor (arXiv,
    Europe PMC, zbMATH, EconBiz, OSF, HAL) frågas bara när amne passar,
    och DataCite när typ är dataset/programvara. Svaret visar
    vilka källor som frågades och varför (fragade_kallor), och vilka som
    inte frågades (ej_fragade).

    Parametrar:
      q        - fritextfråga, eller samma fråga per språk:
                 {"sv": "ensamhet äldre", "en": "loneliness older adults"}.
                 Svenska källor (Libris, SwePub, DiVA, Publicera) får då
                 den svenska, NVA den norska och övriga den engelska.
                 Ange gärna båda: mycket svensk forskning är på engelska.
      amne     - styr källvalet: medicin, biologi, psykologi, matematik,
                 statistik, fysik, datavetenskap, teknik, ekonomi,
                 samhallsvetenskap, juridik, utbildning, humaniora, miljo.
      typ      - styr källvalet: artikel, bok, avhandling, rapport,
                 konferens, preprint, dataset, programvara.
                 (amne och typ filtrerar inte träffarna, de väljer källor.)
      strategi - "auto" (standard) eller "bred" (alla aktiva källor direkt).
      kallor   - uttrycklig källista, t.ex. ["diva", "openalex"]; går före
                 strategi. Se discovery_kallor.
      limit    - antal träffar i svaret (standard 20). Fler finns ofta:
                 hämta nästa sida med fortsattning.
      fran_ar/till_ar - utgivningsår (inklusive; arXiv: inskickningsår).
      oppen_tillgang  - True/False; stöds av vissa källor (discovery_kallor).
      land     - ISO-landskod, t.ex. "SE"; filtrerar hos källor som kan
                 och tar med landets källor i källvalet.
      sammanfattning_max - tecken abstract per träff (standard 300); 0 ger
                 kortast svar. Hela abstractet: discovery_hamta.
      fortsattning - token ur ett tidigare svar: nästa sida av samma
                 resultat, utan nya anrop till källorna. Övriga parametrar
                 ignoreras då.
      synonymer - etablerade alternativa facktermer per språk,
                 {"en": ["social isolation"]}; läggs som ELLER-termer hos
                 källor som stöder det. Högst tre per språk; undvik allmänna
                 enstaka ord, de ger brus.
      sprak    - extra språk (ISO 639-1) utöver dem servern väljer.
      expandera - "auto" (standard): är begreppsexpansionen på i servern
                 expanderas en fråga given som sträng automatiskt. "av":
                 ingen expansion. q som dict expanderas aldrig av servern.
      limit_per_kalla - träffar per källa före sammanslagning (standard 10).
      filter   - källspecifika råfilter: {"openalex": {"topics.id": "..."}}.

    Språkval: svaret redovisar under begreppsexpansion vilka språk frågan
    bör sökas på och varför — frågans språk och engelska alltid; tyska,
    franska och spanska inom humaniora, samhällsvetenskap, juridik och
    utbildning; landets språk med land (t.ex. "CN" ger kinesiska). Språk
    som inte söktes står i saknade_sprak, och tackningsvarningar anger när
    litteraturen på ett språk till stor del ligger utanför källorna.

    Varje träff har: kalla, kalla_id, doi, titel (på originalspråket),
    sprak (originalspråk, ISO 639-1, null om källan inte anger det),
    forfattare (högst tre; forfattare_antal anger totalen), ar, typ, url,
    oa_lank, citeringar, sammanfattning (kapat abstract på originalspråket,
    sammanfattning_kapad), hittad_i och — vid flera språk eller synonymer —
    matchade_termer.
    Träffarna rangordnas efter relevans; en post som flera oberoende källor
    hittar rankas högre. En källa som fallerar redovisas under fel, och en
    som nyss varit överbelastad pausas en stund och står under ej_fragade.
    """
    try:
        if fortsattning:
            return providers.sok_fortsattning(fortsattning)
        return providers.sok_alla(
            q,
            kallor=kallor,
            strategi=strategi,
            amne=amne or None,
            typ=typ or None,
            limit=limit,
            limit_per_kalla=limit_per_kalla,
            fran_ar=fran_ar or None,
            till_ar=till_ar or None,
            oppen_tillgang=oppen_tillgang,
            land=land or None,
            filter=filter,
            sammanfattning_max=sammanfattning_max,
            expandera=expandera,
            synonymer=synonymer,
            sprak=sprak,
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(title="Planera en flerspråkig sökning", annotations=LASNING_EXTERN)
def discovery_expandera(
    q: str | dict[str, str],
    amne: str = "",
    land: str = "",
    sprak: list[str] | None = None,
) -> dict[str, Any]:
    """Visar vilka språk en fråga bör sökas på, utan att söka.

    Returnerar språken med skäl (frågans språk och engelska alltid; fler
    när ämnet har stor litteratur på andra språk eller frågan gäller ett
    land), täckningsvarningar för språk vars litteratur till stor del ligger
    utanför källorna, och — om begreppsexpansionen är påslagen i servern —
    färdiga varianter per språk, synonymer och nyckelord.

    Är servern utan expansion (server_expansion.aktiv = false) tar den
    anropande assistenten själv fram varianterna på de listade språken och
    skickar dem som q={"sv": …, "en": …, …} och synonymer till
    discovery_sok. Använd etablerade fackbegrepp, inte ordagranna
    översättningar.
    """
    try:
        return providers.expandera_fraga(q, amne=amne or None, land=land or None, sprak=sprak)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(title="Läs en post från en discovery-källa", annotations=LASNING_EXTERN)
def discovery_hamta(kalla: str, id: str) -> dict[str, Any]:
    """Läs en enskild post från en namngiven källa (se discovery_kallor).

    kalla - källnamn, t.ex. "openalex", "crossref", "libris", "diva".
    id    - källans egna id eller DOI, beroende på källa (kalla_id/doi ur
            en tidigare discovery_sok-träff fungerar alltid).

    Returnerar samma gemensamma träffschema som discovery_sok, med hela
    abstractet i sammanfattning (okapat). En okänd
    källa, en avstängd källa eller ett okänt id ger ett begripligt fel.
    """
    try:
        return providers.hamta_fran_kalla(kalla, id)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    except tuple(p["fel"] for p in providers.PROVIDERS.values()) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(title="Lista discovery-källor", annotations=LASNING_DB)
def discovery_kallor() -> DiscoveryKallorResultat:
    """Lista alla discovery-källor: sökbara källor och berikningskällor.

    Sökbara källor (fältet "kallor") deltar i discovery_sok och kan hämtas
    via discovery_hamta; varje post visar aktiv-status och filterstöd.
    Berikningskällor (fältet "berikningskallor") används bara av
    discovery_oa_lank/discovery_citeringar, inte av discovery_sok.

    En källa som kräver en nyckel eller kontakt-e-post som saknas i .env
    är "aktiv": false med en förklaring i "inaktiverad_orsak". Fältet
    "svarscache" visar om svarscachen är aktiv, dess livstider och, om den
    är avstängd, varför. Varje källa har en "profil" (roll, språk, ämnen,
    typer, land) som styr källvalet i discovery_sok, och — när den har
    anropats — "statistik_sedan_start" (anrop, cacheträffar, fel,
    medeltid, eventuell paus). "amnen" och "typer" listar giltiga värden
    för discovery_soks parametrar amne och typ.

    Listan är inbyggd i servern (providers.py), inte hämtad över nätet.
    """
    return providers.lista_kallor()


# ===========================================================================
# Berikning: öppen tillgång och citeringar
# ===========================================================================

@mcp.tool(title="Hitta öppen tillgång-länk för en DOI", annotations=LASNING_EXTERN)
def discovery_oa_lank(doi: str) -> DiscoveryOaLankResultat:
    """Slår upp en öppet tillgänglig fulltextlänk för en DOI.

    Frågar OpenAlex och, om DISCOVERY_KONTAKT_EPOST är satt så att källan är
    aktiv, Unpaywall — se discovery_kallor för vilka som faktiskt frågades.

    Returnerar varje tillfrågad källas egna svar under "kallor", plus ett
    sammanfattande "oa_lank" (första källan i tur och ordning som hade en
    länk). Ingen aktiv källa hade en länk ger "oa_lank": null, inte ett fel
    — det är ett giltigt svar (verket kan helt enkelt sakna öppen kopia).
    """
    try:
        return providers.oa_lank(doi)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(title="Visa citeringsgrafen för en post", annotations=LASNING_EXTERN)
def discovery_citeringar(id: str, riktning: str = "citerande", limit: int = 20) -> DiscoveryCiteringarResultat:
    """Visar vilka verk som citerar en post, eller vilka den citerar.

    id       - OpenAlex-id, Semantic Scholar-id eller DOI för posten.
    riktning - "citerande" (verk som citerar posten) eller "referenser"
               (verk posten citerar).
    limit    - max antal träffar per källa (1-200, standard 20).

    Frågar OpenAlex och, om aktiv, Semantic Scholar — se discovery_kallor.
    Returnerar varje källas egna träffar under "kallor"; källorna har olika
    täckning och dedupliceras inte mot varandra här.
    """
    try:
        return providers.citeringar(id, riktning=riktning, limit=limit)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(title="Formatera en referens", annotations=LASNING_EXTERN)
def discovery_citera(
    doi: str = "",
    kalla: str = "",
    id: str = "",
    format: str = "apa",
    sprak: str = "sv-SE",
) -> DiscoveryCiteraResultat:
    """Ger en färdig referens för en post, för rapporter och presentationer.

    Ange doi, eller kalla + id ur en discovery_sok-träff (kalla/kalla_id).

    format - en citeringsstil: "apa" (standard), "harvard-cite-them-right",
             "chicago-author-date", "ieee", "vancouver",
             "modern-language-association" eller något annat CSL-stilnamn;
             eller ett maskinläsbart format för referenshanterare:
             "bibtex", "ris", "csl-json".
    sprak  - språk för stilens fasta ord (t.ex. "m.fl.", "Tillgänglig vid"),
             "sv-SE" som standard, "en-GB"/"en-US" för engelska.

    Har posten en DOI formaterar doi.org referensen ur förlagets egna
    metadata, och alla CSL-stilar fungerar. Saknar posten DOI (t.ex. en
    Libris-post) formateras den ur källans metadata; då stöds apa, harvard,
    bibtex, ris och csl-json. Fältet "kalla" visar vilken väg som användes.
    Kontrollera alltid en referens mot originalet innan den publiceras —
    metadata hos källan kan vara ofullständig.
    """
    try:
        return providers.citera(
            doi=doi or None, kalla=kalla or None, id_=id or None, format=format, sprak=sprak,
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    except DiscoveryKallaFel as exc:
        raise ToolError(str(exc)) from exc
    except tuple(p["fel"] for p in providers.PROVIDERS.values()) as exc:
        raise ToolError(str(exc)) from exc


if __name__ == "__main__":
    starta(mcp, standardport=8000)
