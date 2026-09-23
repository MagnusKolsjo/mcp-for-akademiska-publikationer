"""Klientmodul för arXivs Atom-API.

arXiv är preprint-arkivet för fysik, matematik, datavetenskap, biologi m.fl.
Modulen kapslar in HTTP-anropen mot export.arxiv.org och tolkar de
Atom/OpenSearch-formade svaren till kompakta dictar.

Modulen är självständig från MCP — går att importera och använda i ett vanligt
skript för dataladdning eller felsökning.

API-referens och användarvillkor:
  https://info.arxiv.org/help/api/user-manual.html
  https://info.arxiv.org/help/api/tou.html

arXivs användarvillkor kräver högst ett anrop var tredje sekund från en
enskild klient. sok() och hamta() reserverar därför sin starttid under ett
kort lås innan varje anrop — nödvändigt eftersom MCP 2.x kör synkrona
verktyg på arbetstrådar, så flera anrop kan annars starta samtidigt. Låset
hålls bara för bokföringen, inte under själva HTTP-anropet, så flera anrop
kan vara i flykt samtidigt så länge starterna ligger minst tre sekunder isär.
Villkoren ber också om ett erkännande i gränssnittet: "Thank you to arXiv
for use of its open access interoperability." — se README:s avsnitt om
datakällor.
"""

import os
import re
import threading
import time
import xml.etree.ElementTree as ET

import requests

BASE_URL = os.environ.get("ARXIV_BASE_URL", "https://export.arxiv.org/api/query").rstrip("/")
USER_AGENT = os.environ.get(
    "ARXIV_USER_AGENT",
    "Discovery-MCP/0.1 (MCP-server mot arXiv)",
)
TIMEOUT = float(os.environ.get("ARXIV_TIMEOUT", "30"))

# Källetiketten som följer med varje träff.
KALLA = "arxiv"

# arXivs användarvillkor sätter ett tak på ett anrop var tredje sekund per
# klient. Inte konfigurerbart via miljövariabel — det är en gräns från
# källan, inte ett prestandaval för driften.
_MIN_INTERVALL_S = 3.0

ATTRIBUTION = "Thank you to arXiv for use of its open access interoperability."

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
    "arxiv": "http://arxiv.org/schemas/atom",
}

KORT_SAMMANFATTNING_MAX = 500

# arXiv avvisar ett okänt sortBy/sortOrder-värde med HTTP 400. Validerat här
# innan anropet, så felet blir begripligt direkt i stället för att gå via
# källans felpost.
_GILTIGA_SORT_BY = {"relevance", "lastUpdatedDate", "submittedDate"}
_GILTIGA_SORT_ORDER = {"ascending", "descending"}


class ArxivFel(Exception):
    """Fel vid anrop mot arXiv-API:et."""


_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/atom+xml"})

# Reserverar starttider för arXiv-anrop utan att hålla låset under själva
# HTTP-anropet. Synkrona MCP-verktyg körs på arbetstrådar i mcp 2.x, så
# flera anrop mot arxiv_sok/arxiv_hamta kan annars starta samtidigt — men
# ett anrop som väntar på sin tur ska inte blockera andra trådars anrop som
# redan är i flykt (30 s timeout gånger flera köade trådar blir annars en
# lång kö). Bokföringen under låset är i praktiken omedelbar; väntan och
# själva anropet sker utanför.
_anropslas = threading.Lock()
_nasta_tillatna_start = 0.0


def _reservera_starttid() -> float:
    """Reserverar och returnerar nästa lediga starttid, minst _MIN_INTERVALL_S
    efter föregående reserverade start. Håller låset bara för bokföringen."""
    global _nasta_tillatna_start
    with _anropslas:
        start = max(time.monotonic(), _nasta_tillatna_start)
        _nasta_tillatna_start = start + _MIN_INTERVALL_S
    return start


# ---------------------------------------------------------------------------
# Lågnivå-HTTP
# ---------------------------------------------------------------------------

def _felpost_meddelande(root: ET.Element) -> str | None:
    """Känner igen arXivs felpost och returnerar dess meddelande, annars None.

    arXiv rapporterar ogiltiga frågor (t.ex. ett ogiltigt sortBy-värde) som
    en enda <entry> vars id pekar mot arxiv.org/api/errors — ibland med
    HTTP 200, ibland med en 4xx-status. Tolkningen görs därför oavsett
    statuskod, inte bara vid 200.
    """
    entries = root.findall("atom:entry", _NS)
    if len(entries) == 1:
        id_text = entries[0].findtext("atom:id", default="", namespaces=_NS) or ""
        if "arxiv.org/api/errors" in id_text:
            sammanfattning = entries[0].findtext("atom:summary", default="", namespaces=_NS) or ""
            return sammanfattning.strip() or id_text
    return None


def _hamta_feed(params: list[tuple[str, str]]) -> ET.Element:
    """Hämtar och tolkar ett Atom-svar. Kastar ArxivFel vid problem."""
    start = _reservera_starttid()
    vantetid = start - time.monotonic()
    if vantetid > 0:
        time.sleep(vantetid)

    try:
        svar = _session.get(BASE_URL, params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise ArxivFel(f"Kunde inte nå arXiv ({BASE_URL}): {exc}") from exc

    # Felposten tolkas oavsett statuskod — ett ogiltigt sortBy-värde ger t.ex.
    # HTTP 400 med felposten i kroppen, inte en tom eller icke-XML-kropp.
    root: ET.Element | None
    try:
        root = ET.fromstring(svar.content)
    except ET.ParseError:
        root = None

    if root is not None:
        felmeddelande = _felpost_meddelande(root)
        if felmeddelande:
            raise ArxivFel(f"arXiv avvisade frågan: {felmeddelande}")

    if svar.status_code != 200:
        raise ArxivFel(f"arXiv svarade {svar.status_code}: {svar.text[:300]}")

    if root is None:
        raise ArxivFel("arXiv gav ett svar som inte gick att tolka som XML.")

    return root


def _hamta_entries(root: ET.Element) -> list[ET.Element]:
    """Plockar ut <entry>-elementen. Felposten är redan hanterad i _hamta_feed."""
    return root.findall("atom:entry", _NS)


# ---------------------------------------------------------------------------
# Id-hantering
# ---------------------------------------------------------------------------

# Nya id:n är "2101.00001", äldre är "hep-th/9901001". Båda kan bära en
# versionssuffix "vN" och komma med "arXiv:"-prefix eller som abs-URL.
_VERSION_MONSTER = re.compile(r"v(\d+)$")


def normalisera_id(arxiv_id: str) -> tuple[str, int | None]:
    """Plockar ut det nakna arXiv-id:t och en eventuell version.

    Tar emot bara id:t ("2101.00001"), med version ("2101.00001v2"), med
    "arXiv:"-prefix, en abs-URL eller en pdf-URL (med eller utan ".pdf" och
    med eller utan version, t.ex. "https://arxiv.org/pdf/2101.00001v2.pdf").
    Returnerar (naket_id, version) där version är None om ingen angavs
    (= senaste versionen).
    """
    if not arxiv_id or not arxiv_id.strip():
        raise ArxivFel("Tomt arXiv-id angavs.")

    rensad = arxiv_id.strip()
    rensad = re.sub(r"^https?://arxiv\.org/(abs|pdf)/", "", rensad)
    rensad = re.sub(r"^arxiv:", "", rensad, flags=re.IGNORECASE)
    rensad = re.sub(r"\.pdf$", "", rensad, flags=re.IGNORECASE)

    traff = _VERSION_MONSTER.search(rensad)
    version = int(traff.group(1)) if traff else None
    naket = _VERSION_MONSTER.sub("", rensad)

    if not naket:
        raise ArxivFel(f"Hittar inget giltigt arXiv-id i '{arxiv_id}'.")
    return naket, version


# ---------------------------------------------------------------------------
# Resultatformning
# ---------------------------------------------------------------------------

def _stadad_text(entry: ET.Element, path: str) -> str | None:
    """Läser textinnehållet i ett underelement och normaliserar whitespace.

    Titlar och sammanfattningar kommer radbrutna i källans XML; det är
    källans formatering, inte innehåll, så den slås ihop till ett stycke.
    """
    text = entry.findtext(path, namespaces=_NS)
    if not text:
        return None
    return re.sub(r"\s+", " ", text).strip() or None


def _kapa_sammanfattning(text: str | None) -> str | None:
    """Kapar sammanfattningen till KORT_SAMMANFATTNING_MAX tecken med en markör.

    Kapningen är synlig ("…") och format="full" i hamta() ger hela texten —
    till skillnad från en tyst avskärning finns alltså både en markör och en
    dokumenterad väg till resten.
    """
    if text is None or len(text) <= KORT_SAMMANFATTNING_MAX:
        return text
    return text[:KORT_SAMMANFATTNING_MAX].rstrip() + "…"


def _forma_entry(entry: ET.Element) -> dict:
    """Formar en <entry> till en kompakt dict. Sammanfattningen är okapad här."""
    id_text = entry.findtext("atom:id", namespaces=_NS) or ""
    naket, version = normalisera_id(id_text) if id_text else (None, None)

    forfattare = []
    for author in entry.findall("atom:author", _NS):
        namn = author.findtext("atom:name", namespaces=_NS)
        if namn:
            forfattare.append({"namn": namn.strip()})

    kategorier = [
        c.get("term") for c in entry.findall("atom:category", _NS) if c.get("term")
    ]
    primar_el = entry.find("arxiv:primary_category", _NS)
    primarkategori = primar_el.get("term") if primar_el is not None else None

    url_abs = None
    url_pdf = None
    for link in entry.findall("atom:link", _NS):
        if link.get("type") == "application/pdf" or link.get("title") == "pdf":
            url_pdf = link.get("href")
        elif link.get("rel") == "alternate":
            url_abs = link.get("href")

    return {
        "id": naket,
        "version": version,
        "titel": _stadad_text(entry, "atom:title"),
        "forfattare": forfattare,
        "sammanfattning": _stadad_text(entry, "atom:summary"),
        "publicerad": entry.findtext("atom:published", namespaces=_NS),
        "uppdaterad": entry.findtext("atom:updated", namespaces=_NS),
        "kategorier": kategorier,
        "primarkategori": primarkategori,
        "doi": entry.findtext("arxiv:doi", namespaces=_NS),
        "journal_ref": entry.findtext("arxiv:journal_ref", namespaces=_NS),
        "url_abs": url_abs or (f"https://arxiv.org/abs/{naket}" if naket else None),
        "url_pdf": url_pdf or (f"https://arxiv.org/pdf/{naket}" if naket else None),
    }


# ---------------------------------------------------------------------------
# Frågebyggare
# ---------------------------------------------------------------------------

def _bygg_search_query(
    q: str | None,
    kategori: str | None,
    fran_ar: int | None,
    till_ar: int | None,
) -> str | None:
    """Bygger search_query av fritext, kategori och inskickningsårsintervall.

    q kan redan innehålla arXivs fältprefix (ti:, au:, abs:, cat:, all:) och
    operatorerna AND/OR/ANDNOT; den skickas vidare oförändrad. Delarna ANDas
    ihop, i linje med hur cr_sok/dc_sok kombinerar sina filter.
    """
    delar: list[str] = []
    if q:
        delar.append(f"({q})")
    if kategori:
        delar.append(f"cat:{kategori}")
    if fran_ar or till_ar:
        lag = f"{int(fran_ar):04d}01010000" if fran_ar else "000001010000"
        hog = f"{int(till_ar):04d}12312359" if till_ar else "999912312359"
        delar.append(f"submittedDate:[{lag} TO {hog}]")
    return " AND ".join(delar) if delar else None


# ---------------------------------------------------------------------------
# Publika operationer
# ---------------------------------------------------------------------------

def sok(
    q: str | None = None,
    *,
    limit: int = 20,
    start: int = 0,
    sort_by: str | None = None,
    sort_order: str | None = None,
    kategori: str | None = None,
    fran_ar: int | None = None,
    till_ar: int | None = None,
) -> dict:
    """Söker på arXiv och returnerar formade träffar med kapad sammanfattning.

    q          - fritextfråga, gärna med fältprefix (ti:, au:, abs:, cat:,
                 all:) och operatorerna AND, OR, ANDNOT.
    limit      - max antal träffar (1-100, standard 20).
    start      - antal träffar att hoppa över (paginering).
    sort_by    - "relevance", "lastUpdatedDate" eller "submittedDate".
    sort_order - "ascending" eller "descending".
    kategori   - arXiv-kategori (t.ex. "cs.LG"), ANDas ihop med q.
    fran_ar    - tidigaste inskickningsår (inklusive).
    till_ar    - senaste inskickningsår (inklusive).

    Kräver minst en av q, kategori, fran_ar eller till_ar — arXiv avvisar en
    tom search_query.
    """
    search_query = _bygg_search_query(q, kategori, fran_ar, till_ar)
    if not search_query:
        raise ArxivFel("Ange minst en av q, kategori, fran_ar eller till_ar.")
    if sort_by and sort_by not in _GILTIGA_SORT_BY:
        raise ArxivFel(
            f"Ogiltigt sort_by '{sort_by}'. Giltiga värden: {', '.join(sorted(_GILTIGA_SORT_BY))}."
        )
    if sort_order and sort_order not in _GILTIGA_SORT_ORDER:
        raise ArxivFel(
            f"Ogiltigt sort_order '{sort_order}'. Giltiga värden: {', '.join(sorted(_GILTIGA_SORT_ORDER))}."
        )

    params: list[tuple[str, str]] = [("search_query", search_query)]
    params.append(("start", str(max(0, start))))
    params.append(("max_results", str(max(1, min(limit, 100)))))
    if sort_by:
        params.append(("sortBy", sort_by))
    if sort_order:
        params.append(("sortOrder", sort_order))

    root = _hamta_feed(params)
    entries = _hamta_entries(root)

    totalt_text = root.findtext("opensearch:totalResults", namespaces=_NS)
    totalt = int(totalt_text) if totalt_text and totalt_text.isdigit() else None

    traffar = []
    for entry in entries:
        traff = _forma_entry(entry)
        traff["sammanfattning"] = _kapa_sammanfattning(traff["sammanfattning"])
        traffar.append(traff)

    return {
        "kalla": KALLA,
        "totalt": totalt,
        "start": start,
        "antal": len(traffar),
        "traffar": traffar,
    }


def hamta(arxiv_id: str, *, format: str = "kort") -> dict:
    """Läser en post på arXiv via dess id.

    arxiv_id - arXiv-id med eller utan version, "arXiv:"-prefix eller som
               abs-URL. Utan version ges den senaste.
    format   - "kort" kapar sammanfattningen till KORT_SAMMANFATTNING_MAX
               tecken, "full" ger den oavkortad.

    id_list frågar arXiv efter exakt denna post; en post som inte finns ger
    ett tomt svar (inte ett HTTP-fel), vilket tolkas som ArxivFel här.
    """
    naket, version = normalisera_id(arxiv_id)
    id_param = f"{naket}v{version}" if version else naket

    params = [("id_list", id_param), ("max_results", "1")]
    root = _hamta_feed(params)
    entries = _hamta_entries(root)
    if not entries:
        raise ArxivFel(f"Hittar ingen arXiv-post med id '{arxiv_id}'.")

    traff = _forma_entry(entries[0])
    if format != "full":
        traff["sammanfattning"] = _kapa_sammanfattning(traff["sammanfattning"])
    return traff
