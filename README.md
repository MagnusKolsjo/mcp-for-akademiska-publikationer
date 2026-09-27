# Discovery MCP

MCP-server för att söka publikationer och deras metadata. Hubben samlar flera
live sök-API:er bakom ett gemensamt gränssnitt så att en AI-assistent kan hitta
artiklar, böcker, forskningsdata och bibliotekskatalogposter — och slå mot
flera källor i ett anrop.

## Funktion

Servern är en tunn wrapper mot externa sök-API:er. Varje verktygsanrop går
mot källans API — det finns ingen synk och ingen lokal kopia av källorna,
eftersom de redan är färdigindexerade sök-API:er. En frivillig svarscache
(se nedan) sparar källornas svar en kort tid, så att upprepade anrop inte
kostar nya API-anrop.

Varje källa är en självständig klientmodul. DOI-källorna (Crossref och DataCite)
normaliserar sina träffar till en gemensam form; arXiv har ett eget, rikare
träffschema som normaliseras i providers.py för att också delta i den enade
sökningen. Det gör att `discovery_sok` kan slå mot flera källor samtidigt och
returnera en sammanslagen träfflista. Libris har ett eget, rikare frågespråk
och egna verktyg, men deltar också i den enade sökningen.

Fulltext hämtas och lagras bara för publikationer som användaren sparar i
arbetsbiblioteket (se nedan); sökverktygen själva lagrar ingenting utöver
den tillfälliga svarscachen.

## Datakällor

| Källa | Täckning | Roll | Krav | Attribution/villkor |
|---|---|---|---|---|
| [Libris](https://libris.kb.se/api/docs/reference/find/) | Svenska bibliotekskatalogen (KB) | Sökbar, eget frågespråk och egna verktyg | — | — |
| [Crossref](https://www.crossref.org/documentation/retrieve-metadata/rest-api/) | DOI:er: artiklar, böcker, konferensbidrag | Sökbar | — | `CROSSREF_MAILTO` ger polite pool (frivilligt) |
| [DataCite](https://support.datacite.org/docs/api) | DOI:er: forskningsdata, programvara, preprints | Sökbar | — | — |
| [arXiv](https://info.arxiv.org/help/api/user-manual.html) | Preprints: fysik, matematik, data, biologi m.fl. | Sökbar, eget frågespråk | — | Max 1 anrop/3 s ([villkor](https://info.arxiv.org/help/api/tou.html)); *"Thank you to arXiv for use of its open access interoperability."* |
| [OpenAlex](https://docs.openalex.org/) | Brett index, samtliga ämnesfält | Sökbar | — | CC0. Kostnadsbaserat sedan feb 2026 — se `kostnad_usd` i svaret; `DISCOVERY_OPENALEX_API_NYCKEL` höjer det dagliga taket |
| [Unpaywall](https://unpaywall.org/products/api) | Open access-status för en DOI | Berikning (`discovery_oa_lank`) | `DISCOVERY_KONTAKT_EPOST` | Ingen sökning (`/v2/search` är trasig hos källan och används inte) |
| [Semantic Scholar](https://api.semanticscholar.org/api-docs/graph) | Citeringsgraf | Berikning (`discovery_citeringar`) | — (nyckel rekommenderas) | Attribution krävs, ingen vidaredistribution i bulk — svaren cachas därför aldrig |
| [SwePub](https://www.kb.se) (Libris Xsearch) | Svenska lärosäten/myndigheter | Sökbar (ingen `hamta`) | — | — |
| [DiVA](https://www.diva-portal.org) | ~50 svenska lärosäten/myndigheter | Sökbar | — | Egen `DIVA_USER_AGENT` (källans WAF blockerar UA-strängar som börjar med "Discovery") |
| [Publicera](https://publicera.kb.se) (KB) | Svenska OJS-tidskrifter | Sökbar (via OpenAlex, 46/55 tidskrifter) | — | Botskyddet Anubis — identifierbar UA krävs, aldrig webbläsarlik |
| [NVA](https://nva.sikt.no) | Norska lärosäten | Sökbar | — | Identifierbar User-Agent efterfrågas — `DISCOVERY_KONTAKT_EPOST` bakas in |
| [OSF Preprints](https://osf.io/preprints) | SocArXiv/LawArXiv/EdArXiv/PsyArXiv m.fl. | Sökbar (fritext via SHARE, alla preprintservrar) | — (token rekommenderas för `hamta`) | REST-API:et: 100 anrop/timme delat utan token |
| [Europe PMC](https://europepmc.org) | Biomedicin, life science | Sökbar | — | — |
| [zbMATH Open](https://zbmath.org) | Matematik, MSC-klassificerat | Sökbar | — | Bibliografi CC0, recensioner CC BY-SA 4.0 |
| [EconBiz](https://www.econbiz.de) (ZBW) | Nationalekonomi, working papers | Sökbar | — | Ingen massnedladdning |
| [HAL](https://hal.science) | Franskt öppet arkiv, humaniora/samhällsvetenskap | Sökbar | — | — |
| [DOAJ](https://doaj.org) | Granskade open access-tidskrifter | Sökbar | — | CC0 |
| [CORE](https://core.ac.uk) | Aggregerad fulltext/metadata | Sökbar, **avstängd som standard** | — (nyckel rekommenderas) | Icke-kommersiell fri nivå |

Alla källor kan slås av eller på var för sig via `DISCOVERY_<KALLA>_AKTIV`
i `.env`, utan kodändring. Libris, Crossref, DataCite och arXiv har
dessutom egna verktyg; när en av dem stängs av försvinner även dess
verktyg ur verktygslistan. En
källa som kräver en nyckel eller kontakt-e-post som saknas inaktiveras
automatiskt — kör `discovery_kallor()` för att se aktiv-status och skälet.
CORE är av som standard; sätt `DISCOVERY_CORE_AKTIV=true` för att slå på.

**Publicera-tidskrifternas ISSN:** tidskriftslistan i [mcp-for-kb-publicera](https://github.com/MagnusKolsjo/mcp-for-kb-publicera)
(`tidskrifter.json`) har bara `spec`/`namn`, ingen ISSN. `publicera_tidskrifter.json` i det här
repot togs fram genom att slå upp varje tidskriftsnamn mot OpenAlex
`/sources` och kräva en namnlikhet på minst 0,85 (`difflib.SequenceMatcher`)
innan träffen accepterades — ett engångsuppslag, inte något som körs vid
serverstart. 46 av 55 tidskrifter fick en ISSN; resten (bl.a. "Publicera
Support", som inte är en riktig tidskrift) täcks inte av `publicera_sok`
förrän de kompletteras manuellt.

## Källval i discovery_sok

`discovery_sok` frågar inte alla källor. Den väljer en kärna utifrån
frågan och breddar bara när kärnan ger för få träffar:

- **Alltid:** de två breda indexen OpenAlex och Crossref.
- **Språk:** en svensk fråga tar med Libris, SwePub, DiVA och Publicera, en
  norsk NVA. Språket gissas ur frågan, men säkrast är att skicka frågan per
  språk: `q={"sv": "ensamhet äldre", "en": "loneliness older adults"}` —
  då får varje källa sitt språk.
- **Ämne** (`amne`): arXiv, Europe PMC, zbMATH, EconBiz, OSF och HAL frågas
  bara när ämnet passar dem. **Typ** (`typ`): DataCite vid `dataset` och
  `programvara`. **Land** (`land="SE"`): landets källor.
- **Breddning:** har färre än hälften av `limit` träffar frågans ord i
  titel eller abstract frågas även DOAJ, CORE och de nordiska källorna.
  Ämneskällor som inte passar frågan frågas aldrig automatiskt.
- `strategi="bred"` frågar alla aktiva källor; en uttrycklig `kallor`-lista
  går alltid före.

Svaret redovisar vilka källor som frågades och varför (`fragade_kallor`),
och vilka som inte frågades (`ej_fragade`). `limit` gäller hela svaret;
resten av det sammanslagna resultatet hämtas med `fortsattning` utan nya
anrop till källorna. Träffar med frågans ord i titel eller abstract
rangordnas först, och kringmaterial som förlagen registrerar med egen DOI
("Copyright", "Index", "Front Matter" …) sorteras bort.

En källa som svarar 429 (för många anrop), eller missar tidsgränsen två
gånger i rad, pausas i fem minuter (`DISCOVERY_PAUS_VID_OVERBELASTNING_S`).
`discovery_kallor` visar varje källas profil och statistik sedan start.

## Språk och begreppsexpansion

Forskning publiceras på olika språk i olika fält. I OpenAlex var 2023–2025
omkring 90 % av medicinen, 89 % av datavetenskapen och 83 % av tekniken på
engelska, men bara 63 % av samhällsvetenskapen och 57 % av humanioran — där
är tyska, franska, spanska och portugisiska stora. Kinesisk forskning inom
AI och datavetenskap publiceras nästan helt på engelska (av drygt 380 000
datavetenskapliga verk med kinesisk institution var 546 på kinesiska), och
indisk medicinsk forskning likaså. Kinesisk-, rysk-, arabisk- och
japanskspråkig litteratur finns däremot till stor del i nationella
databaser (CNKI, eLibrary.ru, Al Manhal, J-STAGE) som Discoverys källor
bara delvis täcker.

Därför söker `discovery_sok` alltid på frågans språk och engelska, lägger
till tyska, franska och spanska inom humaniora, samhällsvetenskap, juridik
och utbildning, och landets språk när `land` anges. Svaret redovisar valet
under `begreppsexpansion`, med `saknade_sprak` och `tackningsvarningar`.
`discovery_expandera` visar samma plan utan att söka.

Varianterna på de andra språken tas fram på ett av två sätt:

- **Av den anropande assistenten** (skillen `sok-vetenskapligt`): `q`
  skickas som `{"sv": …, "en": …, "de": …}` och eventuella `synonymer`.
- **Av servern**, om `DISCOVERY_BEGREPPSEXPANSION_AKTIV=true` och en
  språkmodell är konfigurerad. Fail-open: fungerar den inte söks
  originalfrågan.

Varje källa får frågan på sitt språk; källor som stöder ELLER (OpenAlex,
Libris, SwePub, DataCite, HAL, DOAJ, Europe PMC, EconBiz, arXiv, zbMATH,
CORE) får varianter och synonymer som en ELLER-lista. Ett språk som ingen
vald källa har som huvudspråk får ett eget, språkfiltrerat anrop mot
OpenAlex, och varje begärt språk garanteras en plats på första sidan om
det finns träffar. Varje träff har `sprak` (originalspråk) och
`matchade_termer`.

Svaren till användaren ges på användarens språk; titlar, abstract och
citat återges på originalspråket med översättning intill, och en
översättning presenteras aldrig som citat (se serverns instruktioner).

## Arbetsbibliotek

Sökverktygen hittar och beskriver; biblioteket behåller. `discovery_spara`
sparar en publikation med metadata, hela abstractet, en färdig
APA-referens och — när en öppen kopia finns — fulltexten, uppdelad i
numrerade stycken med teckenpositioner och sidnummer. Varje citat får
därmed en adress (post, stycke, sida) som går att kontrollera i efterhand.

- **Fulltext** hämtas från källan (arXiv-PDF, Europe PMC:s JATS-XML, DiVA:s
  fulltextfil, Publiceras artikel-PDF) eller från öppna kopior för DOI:n
  (OpenAlex, Unpaywall). Landningssidor följs bara via metataggen
  `citation_pdf_url`. Skannade PDF:er utan textlager sparas inte som fulltext.
- **Projekt:** poster kan märkas med ett eller flera projekt, t.ex. en
  rapport.
- **Sökning** (`discovery_sok_i_bibliotek`) kombinerar ordsökning med
  semantisk sökning — en fråga på ett språk hittar stycken om samma sak på
  ett annat. `discovery_las` läser ett stycke med omgivning eller
  fulltexten från en position, med samma kapningsregler som övriga servrar.
- **Konfiguration:** `DISCOVERY_BIBLIOTEK_AKTIV=false` ger en lättviktig
  Discovery utan biblioteksverktyg och utan PDF- och embeddingpaket.
  `DISCOVERY_BIBLIOTEK_SEMANTISK=false` behåller ordsökningen men slår av
  embeddings. Kräver `DATABASE_URL` och paketen i `requirements-bibliotek.txt`.
- **Delad drift:** körs servern över http visas texter utan öppen licens
  (Creative Commons eller public domain) bara som utdrag. Lokalt är
  biblioteket användarens eget.

## Svarscache

Med `DATABASE_URL` satt sparas källornas svar i en databas: sökningar i 6
timmar och enskilda poster i 7 dagar (`DISCOVERY_CACHE_SOK_TIMMAR`,
`DISCOVERY_CACHE_POST_DAGAR`). Det sparar pengar hos OpenAlex och kvot hos
källor med hård takt (arXiv, OSF), och ger snabbare svar när samma fråga
körs igen. PostgreSQL och SQLite är likvärdiga val:

- **PostgreSQL** — `DATABASE_URL=postgresql://…`; tabellen hamnar i schemat
  `discovery`, så databasen kan delas med andra MCP-servrar. Kräver
  `psycopg2-binary`.
- **SQLite** — `DATABASE_URL=sqlite:///discovery-cache.db`; en lokal fil i
  servermappen, ingen serverprocess.

Utan `DATABASE_URL`, eller med `DISCOVERY_CACHE_AKTIV=false`, frågas källorna
direkt varje gång. Cachen är fail-open: svarar databasen inte fortsätter
sökningen utan den. `discovery_kallor` visar cachens status, och
`discovery_sok` redovisar `fran_cache` per källa. Fel från en källa cachas
aldrig, och Semantic Scholar cachas aldrig (villkoren förbjuder lagring).

## Installation

Kräver Python med `mcp` 2.x (`mcp>=2.0,<3`).

1. Klona repot.
2. Skapa ett Python-venv och installera beroenden:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   # Arbetsbiblioteket (valfritt):
   pip install -r requirements-bibliotek.txt
   ```
3. Kopiera `config.example.env` till `.env` och fyll i värdena. Sätt särskilt en
   egen `LIBRIS_USER_AGENT`, `DATACITE_USER_AGENT` och `ARXIV_USER_AGENT` med
   kontaktuppgift, och gärna `CROSSREF_MAILTO` för Crossrefs polite pool.
   Sätt `DISCOVERY_KONTAKT_EPOST` till din egen adress (inte ett exempel —
   flera källor avvisar uttryckligen testadresser) för att aktivera
   Unpaywall, höja OpenAlex artighetspool och identifiera dig mot NVA.
   `DISCOVERY_OSF_API_NYCKEL`/`DISCOVERY_CORE_API_NYCKEL` är valfria men
   höjer annars hårt begränsade kvoter (se källtabellen ovan).
4. Lägg till servern i MCP-klientens konfiguration, t.ex.:
   ```json
   {
     "mcpServers": {
       "discovery": {
         "command": "/sökväg/till/.venv/bin/python",
         "args": ["/sökväg/till/mcp_server.py"]
       }
     }
   }
   ```

## MCP-verktyg

| Verktyg | Beskrivning |
|---|---|
| `discovery_sok` | Sök alla aktiva källor samtidigt; sammanslagna, deduplicerade träffar. |
| `discovery_hamta` | Läs en enskild post från en namngiven källa. |
| `discovery_kallor` | Lista källor: aktiv-status, filterstöd, avstängningsskäl. |
| `discovery_oa_lank` | Öppen tillgång-länk för en DOI (OpenAlex + Unpaywall). |
| `discovery_citeringar` | Citeringsgraf för en post (OpenAlex + Semantic Scholar). |
| `discovery_spara` | Spara en post i arbetsbiblioteket: abstract, referens och fulltext i stycken. |
| `discovery_sok_i_bibliotek` | Ord- och semantisk sökning i sparad fulltext. |
| `discovery_las` | Läs ett stycke med omgivning, eller fulltexten från en position. |
| `discovery_lista_bibliotek` | Sparade poster och projekt. |
| `discovery_ta_bort_ur_bibliotek` | Ta bort en post ur ett projekt eller helt. |
| `discovery_expandera` | Vilka språk en fråga bör sökas på, med skäl och täckningsvarningar; färdiga varianter om serverexpansionen är på. |
| `discovery_citera` | Färdig referens (APA, Harvard, IEEE … — alla CSL-stilar) eller BibTeX/RIS/CSL-JSON. |
| `cr_sok` | Sök Crossref (artiklar m.m.), med fält- och årsfilter. |
| `cr_hamta` | Läs ett verk i Crossref via DOI (kort eller full). |
| `dc_sok` | Sök DataCite (forskningsdata m.m.), med typ-, års- och utgivarfilter. |
| `dc_hamta` | Läs en post i DataCite via DOI (kort eller full). |
| `arxiv_sok` | Sök preprints på arXiv, med fältprefix, kategori och årsfilter. |
| `arxiv_hamta` | Läs en preprint på arXiv via dess id (kort eller full). |
| `libris_sok` | Fritext- och filtersökning i Libris-katalogen. |
| `libris_hamta` | Läs en Libris-post — sammanfattning eller fullständig JSON-LD. |
| `libris_bestand` | Visa vilka bibliotek som har ett verk (sigel och namn). |
| `libris_sla_upp_term` | Översätt fritext till id.kb.se-termer för precisa filter. |

### Typiska flöden

Bred sökning över alla aktiva källor:

1. `discovery_kallor()` → se vilka källor som är aktiva.
2. `discovery_sok(q="machine learning fairness", fran_ar=2020)` → sammanslagna, deduplicerade träffar med `doi` och `kalla`.
3. `discovery_hamta(kalla="openalex", id=doi)` → läs hela posten från den källa som gav bäst träff.
4. `discovery_oa_lank(doi)` → hitta en öppet tillgänglig kopia.
5. `discovery_citeringar(id=doi, riktning="citerande")` → vad citerar verket, och vad citeras av det.
6. `discovery_citera(doi=doi, format="apa")` → färdig referens till rapporten; `format="bibtex"` eller `"ris"` för import i Zotero, EndNote m.fl.

Träffarna i `discovery_sok` har abstractet kapat till 300 tecken
(`sammanfattning_max`, 0 utelämnar det); `discovery_hamta` ger hela texten.
Abstract finns hos de flesta källor, men inte hos EconBiz, och bara för en
mindre del av Crossrefs och Libris poster — förlagen och katalogisatörerna
lämnar det inte alltid. Samma artikel hämtad via OpenAlex har det ofta.

Referenser för poster med DOI formateras av doi.org ur förlagets egna
metadata, och alla [CSL-stilar](https://github.com/citation-style-language/styles)
fungerar. Poster utan DOI (t.ex. i Libris) formateras ur källans metadata:
APA, Harvard, BibTeX, RIS och CSL-JSON. Kontrollera alltid en referens mot
originalet innan den publiceras.

Käll-specifik sökning:

- `cr_sok(titel="attention is all you need", typ="proceedings-article")` → Crossref med fältfilter.
- `dc_sok(q="ocean temperature", typ="dataset", utgivare="PANGAEA")` → DataCite med typ- och utgivarfilter.
- `arxiv_sok(q="au:hinton AND cat:cs.LG")` → arXiv med fältprefix och kategori.

## Lägga till en ny källa

Discovery är byggd för att kopplas på fler källor. Mönstret för en ny,
av/på-bar källa (en källa som också ska ha egna verktyg registrerar dem i
`mcp_server.py` med `@kallverktyg("<namn>", ...)`, så att av/på-läget gäller
dem också):

1. Skriv `<namn>_client.py` med `sok()`/`hamta()` som returnerar dictar med
   minst `kalla`/`doi`/`titel` (se `openalex_client.py`), en egen felklass
   som ärver `kallhjalp.DiscoveryKallaFel`, User-Agent via
   `kallkonfig.user_agent(...)` och takt via `kallhjalp.ny_taktbegransare(...)`.
2. Lägg till en rad i `providers.PROVIDERS` (sökbar källa) eller
   `providers.BERIKNING` (svarar bara på frågor om en redan känd post), med
   `aktiv` kopplat till `kallkonfig.aktiv(namn)` och `krav_saknas` om källan
   kräver en nyckel eller e-post.
3. Dokumentera källan i README:s källtabell och i `config.example.env`
   (`DISCOVERY_<NAMN>_AKTIV`, ev. `DISCOVERY_<NAMN>_API_NYCKEL`).

## Transport

Servern stödjer två transporter, valda via `MCP_TRANSPORT`:

- **stdio** — MCP-klienten startar processen lokalt.
- **http** — långkörande process bakom en URL, delad av flera klienter/maskiner.
  Kräver `MCP_API_KEY`: uppstarten avbryts (exitkod 2) om nyckeln saknas, så en
  öppen endpoint inte kan uppstå av misstag. Klienten skickar nyckeln i
  `Authorization: Bearer <nyckel>`-headern.

## Licens

AGPL-3.0-or-later.

Servern lagrar ingenting från källorna. Posterna omfattas av respektive källas villkor; se kolumnen Attribution/villkor under Datakällor.
