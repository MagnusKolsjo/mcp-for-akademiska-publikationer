"""MCP-server för discovery — sök publikationer och deras metadata.

Hubben samlar flera live sök-API:er för publikationer och forskningsutfall bakom
ett gemensamt MCP-gränssnitt. Varje källa är en självständig klientmodul; varje
verktygsanrop går direkt mot källans API. Ingen lokal databas och ingen synk —
källorna är redan färdigindexerade sök-API:er.

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
  Enad sökning över DOI-källorna
    discovery_sok       - sök Crossref och DataCite samtidigt, sammanslaget
    discovery_kallor    - lista de källor som ingår i den enade sökningen

Nya källor kopplas in via providers.py — se den modulen för mönstret.
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

import crossref_client
import datacite_client
import libris_client
import providers
from crossref_client import CrossrefFel
from datacite_client import DataCiteFel
from libris_client import LibrisFel
from mcp_annotationer import CACHE_HINTAR, LASNING_DB, LASNING_EXTERN
from mcp_transport import starta

mcp = MCPServer(
    "discovery",
    instructions=(
        "Sök publikationer och deras metadata över flera källor: Libris "
        "(svenska bibliotekskatalogen), Crossref och DataCite (DOI-metadata). "
        "discovery_sok slår mot Crossref och DataCite samtidigt; libris_sok "
        "har ett eget, rikare frågespråk och egna verktyg."
    ),
    version="0.1.0",
    cache_hints=CACHE_HINTAR,
)


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
    fraga: str
    kallor: list[str]
    per_kalla: dict[str, dict[str, Any]]
    antal: int
    traffar: list[dict[str, Any]]
    fel: NotRequired[dict[str, str]]


class DiscoveryKallorResultat(TypedDict):
    kallor: list[dict[str, Any]]


# ===========================================================================
# Libris — Sveriges nationella bibliotekskatalog (KB)
# ===========================================================================

@mcp.tool(title="Sök i Libris", annotations=LASNING_EXTERN)
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
        return libris_client.sok(
            q=q or None,
            limit=limit,
            offset=offset,
            sort=sort or None,
            filter=sammanslaget or None,
        )
    except LibrisFel as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(title="Läs en Libris-post", annotations=LASNING_EXTERN)
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
            return libris_client.hamta_post(libris_id)
        return libris_client.sammanfatta_post(libris_id)
    except LibrisFel as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(title="Visa Libris-bestånd", annotations=LASNING_EXTERN)
def libris_bestand(libris_id: str, limit: int = 50) -> LibrisBestandResultat:
    """Visa vilka bibliotek som har ett verk (bestånd).

    libris_id - postens id eller URL för verket/instansen.
    limit     - max antal bibliotek att lista (1-200, standard 50).

    Returnerar antalet bibliotek totalt och en lista med sigel (kortkod)
    och namn för varje innehavande bibliotek.
    """
    try:
        return libris_client.bestand(libris_id, limit=limit)
    except LibrisFel as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(title="Slå upp Libris-term", annotations=LASNING_EXTERN)
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
        return libris_client.sla_upp_term(q, typ=typ or None, limit=limit)
    except LibrisFel as exc:
        raise ToolError(str(exc)) from exc


# ===========================================================================
# Crossref — DOI:er för artiklar, böcker, konferensbidrag
# ===========================================================================

@mcp.tool(title="Sök i Crossref", annotations=LASNING_EXTERN)
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
        return crossref_client.sok(
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
        )
    except CrossrefFel as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(title="Läs ett verk i Crossref", annotations=LASNING_EXTERN)
def cr_hamta(doi: str, format: str = "kort") -> dict[str, Any]:
    """Läs ett verk i Crossref via dess DOI.

    doi    - DOI som naken identifierare, doi.org-URL eller doi:-sträng.
    format - "kort" ger en kompakt sammanfattning, "full" lägger till abstract,
             volym/nummer/sidor, ISSN/ISBN, ämnen och licens.

    Returnerar postdata enligt valt format. En okänd DOI ger ett fel som
    säger att verket inte hittades.
    """
    try:
        return crossref_client.hamta(doi, format=format)
    except CrossrefFel as exc:
        raise ToolError(str(exc)) from exc


# ===========================================================================
# DataCite — DOI:er för forskningsdata, programvara, preprints
# ===========================================================================

@mcp.tool(title="Sök i DataCite", annotations=LASNING_EXTERN)
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
        return datacite_client.sok(
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
        )
    except DataCiteFel as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(title="Läs en post i DataCite", annotations=LASNING_EXTERN)
def dc_hamta(doi: str, format: str = "kort") -> dict[str, Any]:
    """Läs en post i DataCite via dess DOI.

    doi    - DOI som naken identifierare, doi.org-URL eller doi:-sträng.
    format - "kort" ger en kompakt sammanfattning, "full" lägger till abstract,
             version, ämnen, format, rättigheter och statistik.

    Returnerar postdata enligt valt format. En okänd DOI ger ett fel som
    säger att posten inte hittades.
    """
    try:
        return datacite_client.hamta(doi, format=format)
    except DataCiteFel as exc:
        raise ToolError(str(exc)) from exc


# ===========================================================================
# Enad sökning över DOI-källorna
# ===========================================================================

@mcp.tool(title="Sök flera DOI-källor samtidigt", annotations=LASNING_EXTERN)
def discovery_sok(
    q: str,
    kallor: list[str] | None = None,
    limit_per_kalla: int = 10,
    fran_ar: int = 0,
    till_ar: int = 0,
) -> DiscoverySokResultat:
    """Sök flera DOI-källor samtidigt och få sammanslagna, normaliserade träffar.

    Slår mot Crossref och DataCite (eller en delmängd) i ett anrop och returnerar
    en gemensam träfflista där varje träff är taggad med sin källa. Bra som första
    bred sökning; använd käll-verktygen (cr_sok, dc_sok) för käll-specifika filter.

    Parametrar:
      q               - fritextfråga som skickas till varje vald källa.
      kallor          - lista med källnamn ("crossref", "datacite").
                        Utelämnad = alla. Se discovery_kallor.
      limit_per_kalla - max antal träffar per källa innan sammanslagning
                        (standard 10).
      fran_ar         - tidigaste utgivningsår (inklusive).
      till_ar         - senaste utgivningsår (inklusive).

    Varje träff har fälten: kalla, doi, titel, forfattare, ar, typ, utgivare,
    container, url, citeringar. Resultatet sorteras med nyast först. En källa som
    fallerar stoppar inte de andra — dess fel rapporteras under "fel".
    """
    try:
        return providers.sok_alla(
            q,
            kallor=kallor,
            limit_per_kalla=limit_per_kalla,
            fran_ar=fran_ar or None,
            till_ar=till_ar or None,
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(title="Lista discovery-källor", annotations=LASNING_DB)
def discovery_kallor() -> DiscoveryKallorResultat:
    """Lista de DOI-källor som ingår i den enade sökningen (discovery_sok).

    Returnerar varje källas namn (att använda i discovery_sok), en etikett och
    en kort beskrivning av vad källan täcker.

    Listan är inbyggd i servern (providers.py), inte hämtad över nätet.
    """
    return providers.lista_kallor()


if __name__ == "__main__":
    starta(mcp, standardport=8000)
