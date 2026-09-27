# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/sv/1.1.0/).
Versioner enligt [SemVer](https://semver.org/lang/sv/).

## [Unreleased]

### Tillagt
- Flerspråkig sökning och begreppsexpansion: `discovery_sok` väljer språk
  efter frågans språk, ämne och land (`orkestrering.expansionssprak`) och
  redovisar valet, saknade språk och täckningsvarningar under
  `begreppsexpansion`. Varianter per språk kommer från anroparen
  (`q` som dict, `synonymer`) eller från servern (`begreppsexpansion.py`,
  OpenAI-kompatibel språkmodell, `DISCOVERY_BEGREPPSEXPANSION_*`,
  redigerbar prompt i `prompts/`, fail-open, cachad). Källor med
  ELLER-stöd får varianter och synonymer som ELLER-lista; språk utan egen
  källa får ett språkfiltrerat OpenAlex-anrop; varje begärt språk
  garanteras en plats på första sidan.
- `discovery_expandera`: språkplanen för en fråga utan att söka.
- `sprak` (originalspråk, ISO 639-1) i det gemensamma träffschemat, från
  alla källor som anger det, och `matchade_termer` vid flerspråkig sökning.
- Serverns instruktioner: svara på användarens språk, återge titlar,
  abstract och citat på originalspråket med översättning, erbjud originalet.
- Sökorkestrering i `discovery_sok` (`orkestrering.py`): i stället för att
  fråga alla källor väljs en kärna — OpenAlex och Crossref plus källor som
  passar frågans språk, `amne`, `typ` och `land` — som breddas till
  allmänna och nordiska källor bara när för få träffar innehåller frågans
  ord. `strategi="bred"` frågar alla. `q` kan ges per språk
  (`{"sv": …, "en": …}`). Svaret redovisar `fragade_kallor` med skäl och
  `ej_fragade`.
- `discovery_sok`: `limit` gäller hela svaret; nästa sida hämtas med
  `fortsattning` utan nya källanrop. Högst tre författare per träff
  (`forfattare_antal` anger totalen).
- Kretsbrytare: en källa som svarar 429 eller missar tidsgränsen två gånger
  i rad pausas (`DISCOVERY_PAUS_VID_OVERBELASTNING_S`, standard 300 s).
  `discovery_kallor` visar varje källas profil och statistik sedan start.

- Abstract (`sammanfattning`) i det gemensamma träffschemat, hämtat från
  alla källor som har det: OpenAlex (återskapat ur `abstract_inverted_index`),
  Crossref, DataCite, arXiv, Libris, SwePub, DiVA, Publicera, NVA, OSF,
  Europe PMC (nu `resultType=core`), zbMATH Open (recensionstexten), HAL,
  DOAJ och CORE. `discovery_sok` kapar det till `sammanfattning_max` tecken
  (standard 300, markerat med "…" och `sammanfattning_kapad`);
  `discovery_hamta` ger hela texten. `cr_sok`/`dc_sok` och kort form av
  `cr_hamta`/`dc_hamta` visar det kapat till 500 tecken, som `arxiv_sok`.
- `discovery_citera`: färdig referens för rapporter och presentationer —
  alla CSL-stilar (APA, Harvard, IEEE, Vancouver, Chicago m.fl.) på valfritt
  språk, eller BibTeX/RIS/CSL-JSON. Poster med DOI formateras av doi.org
  ur förlagets metadata; poster utan DOI formateras ur källans metadata
  (APA, Harvard, BibTeX, RIS, CSL-JSON). Ny modul `citering.py`.
- `kallhjalp.ren_text`/`kapa_text`: gemensam städning av HTML/JATS i
  abstract och synlig kapning vid ordgräns.
- Svarscache (`svarscache.py`, `db.py`): källornas svar sparas i
  PostgreSQL eller SQLite, valt med `DATABASE_URL` — sökningar i 6 timmar,
  enskilda poster i 7 dagar (`DISCOVERY_CACHE_SOK_TIMMAR`,
  `DISCOVERY_CACHE_POST_DAGAR`). Gäller `discovery_sok`, `discovery_hamta`,
  `discovery_oa_lank`, `discovery_citeringar` och käll-verktygen. Fail-open:
  utan databas, med `DISCOVERY_CACHE_AKTIV=false` eller om databasen inte
  svarar frågas källorna direkt. Fel cachas aldrig, Semantic Scholar
  cachas aldrig. `discovery_kallor` visar cachens status och
  `discovery_sok` redovisar `fran_cache` per källa.
- Libris, Crossref, DataCite och arXiv kan slås av/på med
  `DISCOVERY_<KALLA>_AKTIV` precis som övriga källor. Avstängd källa
  ingår inte i `discovery_sok`, och dess egna verktyg (`libris_*`, `cr_*`,
  `dc_*`, `arxiv_*`) registreras inte.
- Libris deltar i `discovery_sok` och kan läsas via `discovery_hamta`;
  DOI tas ur katalogpostens identifierare när den finns.
- Licens: AGPL-3.0-or-later, med SPDX-huvud i varje Python-fil.
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
  `DISCOVERY_<KALLA>_AKTIV`, utan kodändring. En källa som kräver en nyckel
  eller `DISCOVERY_KONTAKT_EPOST` den saknar inaktiveras automatiskt, med
  förklaring i `discovery_kallor`. Ny gemensam infrastruktur för detta:
  `kallkonfig.py` (av/på, nycklar, User-Agent, kontakt-e-post) och
  `kallhjalp.py` (taktbegränsare, gemensam felbasklass).

- Elva ytterligare källor: SwePub (Libris Xsearch), DiVA (export.jsf,
  förenklad från mcp-for-diva), Publicera/KB (sökning via en
  OpenAlex-fråga begränsad till 46 av 55 identifierade tidskrifts-ISSN,
  hämtning äkta via OAI-PMH GetRecord med samma Anubis-igenkänning som
  mcp-for-kb-publicera), NVA (Norge), OSF Preprints (titelsökning, en
  leverantör per anrop), Europe PMC, zbMATH Open, EconBiz, HAL, DOAJ och
  CORE (avstängd som standard, `DISCOVERY_CORE_AKTIV=true` slår på den).
  Samtliga deltar i `discovery_sok`/`discovery_hamta` via samma register
  i `providers.py`.
- `publicera_tidskrifter.json`: ISSN/eISSN för Publicera-tidskrifterna,
  eftersom `tidskrifter.json` i mcp-for-kb-publicera saknar ISSN helt.
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
- DiVA:s klient använder ett annat User-Agent-standardvärde än övriga nya
  källor, eftersom DiVA:s WAF blockerar strängar som börjar med
  "Discovery" (verifierat) — se `diva_client.py`.

### Rättat
- `discovery_sok`: träffarna sorterades nyast först, vilket begravde de
  mest relevanta. Nu rangordnas de med reciprocal rank fusion över
  källornas egna relevansordningar, och en post som flera källor hittar
  rankas högre. Nytt fält `hittad_i` visar alla källor som hittade posten.
- `discovery_sok`: tidsgränsen per källa fungerade inte — en källa som tog
  längre tid än 20 s fällde hela anropet med ett ofångat `TimeoutError`,
  och poolen väntade ändå in alla trådar. Nu returneras svaret efter 15 s
  med den långsamma källan redovisad under `fel`.
- `discovery_sok`: DOI:er normaliseras (gemener, utan `https://doi.org/`
  och `doi:`) innan deduplicering, och dubbletter slås ihop i stället för
  att den första vinner — tomma fält som `oa_lank` fylls från andra källor.
- arXiv i `discovery_sok`: flerordsfrågor skickades som OR (hundratusentals
  träffar, den sökta artikeln saknades bland de första); nu blir de
  `all:a AND all:b` utan stoppord. arXiv-träffar utan förlags-DOI får
  arXivs DataCite-DOI (`10.48550/arxiv.<id>`) så att de slås ihop med
  samma preprint från DataCite och OpenAlex. `arxiv_sok` är oförändrat.
- OSF Preprints söker nu via SHARE (share.osf.io), OSF:s egen sökmotor:
  fritext i titel och abstract över alla OSF-preprintservrar, med
  årsfilter och författarnamn. Tidigare söktes bara en exakt delsträng i
  titeln hos en enda preprintserver, vilket i praktiken gav noll träffar.
- zbMATH Open svarar 404 på en sökning utan träffar; det redovisades som
  källfel och tolkas nu som ett tomt resultat.
- OpenAlex: vid 429 görs ett omförsök om källan ber om högst 5 s väntan
  (`OPENALEX_MAX_VANTA_VID_429`); annars ett felmeddelande som pekar på
  `DISCOVERY_OPENALEX_API_NYCKEL`.
- Serverns `instructions` beskriver nu alla källor, inte bara fem.
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
- `discovery_sok` frågar inte längre alla aktiva källor som standard; se
  ovan. `strategi="bred"` ger det tidigare beteendet.
- Träffar med frågans ord i titel eller abstract rangordnas före övriga,
  och kringmaterial ("Copyright", "Index", "Front Matter" …) och poster
  utan titel sorteras bort.
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
