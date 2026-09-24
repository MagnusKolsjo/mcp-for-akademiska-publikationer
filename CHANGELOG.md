# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/sv/1.1.0/).
Versioner enligt [SemVer](https://semver.org/lang/sv/).

## [Unreleased]

### Tillagt
- arXiv som ny källa: `arxiv_client.py`, verktygen `arxiv_sok` och
  `arxiv_hamta`, och en rad i `providers.py` så att arXiv deltar i
  `discovery_sok`/`discovery_kallor` tillsammans med Crossref och DataCite.
  Anropen mot arXiv strypas till högst ett var tredje sekund, enligt
  källans användarvillkor.
- OpenAlex som ny, sökbar källa (`openalex_client.py`): brett index över
  samtliga ämnesfält, med filter på ämne (`primary_topic.field.id`,
  `topics.id`), institutionsland, öppen tillgång och utgivningsår. Deltar i
  `discovery_sok`. Kostnadsbaserat sedan februari 2026 — varje svar
  innehåller `kostnad_usd` ur källans egna `meta.cost_usd`.
- Unpaywall (`unpaywall_client.py`) och Semantic Scholar
  (`semanticscholar_client.py`) som berikningskällor: svarar bara på frågor
  om en redan känd DOI/id (open access-länk respektive citeringsgraf),
  deltar inte i `discovery_sok`. Unpaywall kräver
  `DISCOVERY_KONTAKT_EPOST` och stängs av automatiskt utan den.
- Tre nya verktyg: `discovery_hamta(kalla, id)` (läs en post från en
  namngiven källa), `discovery_oa_lank(doi)` (öppen tillgång-länk via
  OpenAlex + Unpaywall) och `discovery_citeringar(id, riktning)`
  (citeringsgraf via OpenAlex + Semantic Scholar).
- `discovery_sok` utökad med `oppen_tillgang`, `land` och `filter`
  (källspecifika råfilter); resultatet dedupliceras nu på DOI. Källorna
  frågas parallellt (en tråd per källa) med en delad tidsgräns på 15 s per
  källa — en långsam eller nedgången källa fördröjer inte de andra.
- Nya källor kan slås av/på oberoende i `.env` via
  `DISCOVERY_<KALLA>_AKTIV`, utan kodändring (gäller inte Libris/Crossref/
  DataCite/arXiv, som behålls oförändrade). En källa som kräver en nyckel
  eller `DISCOVERY_KONTAKT_EPOST` den saknar inaktiveras automatiskt, med
  förklaring i `discovery_kallor`. Ny gemensam infrastruktur för detta:
  `kallkonfig.py` (av/på, nycklar, User-Agent, kontakt-e-post) och
  `kallhjalp.py` (taktbegränsare, gemensam felbasklass).

- Elva ytterligare källor: SwePub (Libris Xsearch), DiVA (export.jsf,
  förenklad från stream-12-diva-portal), Publicera/KB (sökning via en
  OpenAlex-fråga begränsad till 46 av 55 identifierade tidskrifts-ISSN,
  hämtning äkta via OAI-PMH GetRecord med samma Anubis-igenkänning som
  stream-11-publicera-kb), NVA (Norge), OSF Preprints (titelsökning, en
  leverantör per anrop), Europe PMC, zbMATH Open, EconBiz, HAL, DOAJ och
  CORE (avstängd som standard, `DISCOVERY_CORE_AKTIV=true` slår på den).
  Samtliga deltar i `discovery_sok`/`discovery_hamta` via samma register
  i `providers.py`.
- `publicera_tidskrifter.json`: ISSN/eISSN för Publicera-tidskrifterna,
  eftersom `stream-11-publicera-kb/tidskrifter.json` saknar ISSN helt.
  Togs fram genom ett engångsuppslag av varje tidskriftsnamn mot OpenAlex
  `/sources` (namnlikhet ≥ 0,85 krävdes för att acceptera en träff);
  46 av 55 tidskrifter fick en ISSN.
- `kallkonfig.aktiv()` har fått en `standard=`-parameter så att en källa
  (CORE) kan vara av som standard medan övriga är på.

### Kända begränsningar
- SwePub saknar `hamta()` — Xsearch har ingen dokumenterad hämtning av en
  enskild känd post.
- Publicera-sökningen täcker bara 46 av 55 tidskrifter (de utan
  identifierad ISSN); se `publicera_tidskrifter.json`.
- OSF Preprints: författarnamn utelämnas (kräver ett extra nästlat
  embed-anrop per träff), och `filter[provider]` matchar bara en
  preprintserver per anrop, inte en lista.
- DiVA:s klient använder ett annat User-Agent-standardvärde än övriga nya
  källor, eftersom DiVA:s WAF blockerar strängar som börjar med
  "Discovery" (verifierat) — se `diva_client.py`.

### Rättat
- `arxiv_client.py`: arXivs felpost (ogiltiga sökparametrar, t.ex. ett
  ogiltigt `sortBy`-värde) tolkas nu oavsett HTTP-status, inte bara vid
  200 — ett 400-svar gav tidigare rå Atom-XML som felmeddelande.
  `sort_by`/`sort_order` valideras dessutom innan anropet.
- `arxiv_client.py`: strypningen mot arXiv håller låset bara för att
  reservera nästa starttid, inte under hela HTTP-anropet. Flera anrop kan
  nu vara i flykt samtidigt så länge starterna ligger minst tre sekunder
  isär, i stället för att köa bakom varandras hela svarstid.
- `arxiv_client.normalisera_id`: hanterar nu även pdf-URL:er
  (`arxiv.org/pdf/...`), med eller utan `.pdf`-ändelse och version.

### Ändrat
- Migrerad till `mcp` 2.x (`MCPServer` i stället för `FastMCP`, `mcp>=2.0,<3`
  i `requirements.txt`).
- Http-transporten körs nu via den gemensamma `mcp_transport.starta()` i
  stället för handskriven uvicorn/Starlette-uppstart i `mcp_server.py`.
- Verktygens returvärden är typade (`TypedDict`/`dict[str, Any]`) i stället
  för otypad `dict`, så klienten får ett strukturerat `outputSchema`.
- Förväntade fel (okänt Libris-id, okänd DOI, källan svarar inte) kastas nu
  som `ToolError` i stället för att returneras som `{"fel": ...}`.
- Alla tio verktyg har fått `title=` och läsannotationer
  (`LASNING_EXTERN` för de nio som anropar en extern källa,
  `LASNING_DB` för `discovery_kallor` som listar en inbyggd källista).

### Brytande
- Http-läget kräver nu `MCP_API_KEY`. Uppstart utan nyckel avbryts med
  exitkod 2 i stället för att som tidigare starta öppet med en varning.
- Fel som tidigare kom som ett lyckat svar med fältet `"fel"` kommer nu som
  ett verktygsfel (`ToolError`, `isError: true`). Klienter som läste
  `"fel"`-fältet för att avgöra om ett anrop misslyckades måste läsa
  `isError` i stället.
