# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/sv/1.1.0/).
Versioner enligt [SemVer](https://semver.org/lang/sv/).

## [Unreleased]

## [1.0.0] - 2026-09-28

Första publicerade versionen: en MCP-server för sökning, läsning och
citering av akademiska publikationer i ett tjugotal källor — granskade
artiklar och böcker liksom preprints, avhandlingar, rapporter och
forskningsdata som inte genomgått peer review.

### Källor
- Sökbara: OpenAlex, Crossref, DataCite, arXiv, Libris, SwePub, DiVA,
  Publicera (KB), NVA, OSF Preprints (via SHARE), Europe PMC, zbMATH Open,
  EconBiz, HAL, DOAJ och CORE (avstängd som standard).
- Berikning: Unpaywall (öppna kopior, kräver `DISCOVERY_KONTAKT_EPOST`) och
  Semantic Scholar (citeringsgraf).
- Varje källa slås av och på med `DISCOVERY_<KALLA>_AKTIV`. En källa som
  saknar en nödvändig nyckel eller kontakt-e-post stängs av automatiskt,
  med förklaring i `discovery_kallor`.

### Sökning
- `discovery_sok` väljer källor efter frågans språk, ämne (`amne`),
  dokumenttyp (`typ`) och land, och breddar bara när för få träffar
  innehåller frågans ord; `strategi="bred"` frågar alla aktiva källor.
- Flerspråkig sökning: frågan kan ges per språk (`{"sv": …, "en": …}`),
  med synonymer. Språken väljs efter hur forskningen inom ämnet faktiskt
  publiceras, och svaret redovisar valet, saknade språk och
  täckningsvarningar. Varianterna tas fram av den anropande assistenten
  (skillen `sok-vetenskapligt`) eller av en språkmodell i servern
  (`DISCOVERY_BEGREPPSEXPANSION_*`, avstängd som standard).
  `discovery_expandera` visar språkplanen utan att söka.
- Träffarna slås ihop på DOI och rangordnas med reciprocal rank fusion;
  poster som flera källor hittar rankas högre, och kringmaterial som
  "Copyright" och "Index" sorteras bort. Varje träff har abstract (kapat i
  träfflistan) och originalspråk. `limit` gäller hela svaret, och nästa
  sida hämtas med `fortsattning` utan nya källanrop.
- En källa som är överbelastad pausas en stund i stället för att fördröja
  varje svar. `discovery_kallor` visar källornas profiler och statistik.
- Käll-specifika verktyg med rikare frågespråk: `libris_sok`,
  `libris_hamta`, `libris_bestand`, `libris_sla_upp_term`, `cr_sok`,
  `cr_hamta`, `dc_sok`, `dc_hamta`, `arxiv_sok` och `arxiv_hamta`. De
  registreras bara när källan är aktiv.

### Läsning och citering
- `discovery_hamta` läser en post med hela abstractet.
- `discovery_oa_lank` hittar öppna kopior (OpenAlex och Unpaywall);
  `discovery_citeringar` visar citeringsgrafen (OpenAlex och Semantic
  Scholar).
- `discovery_citera` ger färdiga referenser i alla CSL-stilar (APA,
  Harvard, IEEE, Vancouver, Chicago m.fl.) via doi.org, samt BibTeX, RIS
  och CSL-JSON; poster utan DOI formateras ur källans metadata.

### Arbetsbibliotek
- `discovery_spara` sparar en publikation med metadata, abstract, referens
  och fulltext uppdelad i numrerade stycken med teckenpositioner och
  sidnummer, märkt med projekt. Fulltext hämtas från arXiv, Europe PMC,
  DiVA, Publicera och öppna kopior via OpenAlex och Unpaywall.
- Skannade sidor maskinläses (OCR) med Tesseract via `pdftext_skydd.py`,
  sida för sida så att sidnumren stämmer.
- `discovery_sok_i_bibliotek` kombinerar ordsökning och semantisk sökning
  (flerspråkiga embeddings), `discovery_las` läser ett stycke eller
  fulltexten från en position, `discovery_lista_bibliotek` och
  `discovery_ta_bort_ur_bibliotek` förvaltar biblioteket.
- Nedladdade fulltexter och skannade PDF:er raderas efter 30 dagar
  (`DISCOVERY_FULLTEXT_BEVARA_DAGAR`); metadata och referens behålls.
- Över http visas texter utan öppen licens bara som utdrag. Fulltext
  hämtas bara från publika adresser, och varje omdirigering kontrolleras.
- `DISCOVERY_BIBLIOTEK_AKTIV=false` ger en lättviktig server utan
  biblioteksverktyg och utan PDF- och embeddingpaket.

### Drift
- PostgreSQL eller SQLite, valt med `DATABASE_URL`, för svarscache och
  arbetsbibliotek. Utan databas fungerar all sökning, bara utan cache och
  bibliotek. Schemat ligger i `db/` och är låst som baslinje v1.0.
- Svarscache: sökningar i 6 timmar och poster i 7 dagar; fail-open om
  databasen inte svarar.
- Transport via stdio eller Streamable HTTP (`MCP_TRANSPORT`); http kräver
  `MCP_API_KEY`.
- Servern identifierar sig som `mcp-for-akademiska-publikationer/1.0` mot
  källorna.
- Licens: AGPL-3.0-or-later.

### Kända begränsningar
- SwePub saknar hämtning av en enskild post.
- Publicera-sökningen täcker 46 av 55 tidskrifter (de med identifierad
  ISSN i `publicera_tidskrifter.json`).
- EconBiz saknar abstract; Crossref och Libris har det bara för en del av
  posterna.
- Kinesisk-, rysk-, arabisk- och japanskspråkig litteratur ligger till
  stor del i nationella databaser som källorna bara delvis täcker.
- OpenAlex är kostnadsbaserat; `DISCOVERY_OPENALEX_API_NYCKEL` (gratis)
  behövs för stabil åtkomst.
