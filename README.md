# Discovery MCP

MCP-server för att söka publikationer och deras metadata. Hubben samlar flera
live sök-API:er bakom ett gemensamt gränssnitt så att en AI-assistent kan hitta
artiklar, böcker, forskningsdata och bibliotekskatalogposter — och slå mot
flera källor i ett anrop.

## Funktion

Servern är en tunn wrapper mot externa sök-API:er. Varje verktygsanrop går
direkt mot källans API — det finns ingen lokal databas och ingen synk, eftersom
källorna redan är färdigindexerade sök-API:er.

Varje källa är en självständig klientmodul. DOI-källorna (Crossref och DataCite)
normaliserar sina träffar till en gemensam form; arXiv har ett eget, rikare
träffschema som normaliseras i providers.py för att också delta i den enade
sökningen. Det gör att `discovery_sok` kan slå mot flera källor samtidigt och
returnera en sammanslagen träfflista. Libris har ett eget, rikare frågespråk
och egna verktyg, utanför den enade sökningen.

Nedladdning och lagring av fulltext ligger utanför den här servern — discovery
hittar och beskriver poster; en separat tjänst kan hämta och lagra dem.

## Datakällor

| Källa | Täckning | Roll | Krav | Attribution/villkor |
|---|---|---|---|---|
| [Libris](https://libris.kb.se/api/docs/reference/find/) | Svenska bibliotekskatalogen (KB) | Sökbar, eget frågespråk | — | — |
| [Crossref](https://www.crossref.org/documentation/retrieve-metadata/rest-api/) | DOI:er: artiklar, böcker, konferensbidrag | Sökbar | — | `CROSSREF_MAILTO` ger polite pool (frivilligt) |
| [DataCite](https://support.datacite.org/docs/api) | DOI:er: forskningsdata, programvara, preprints | Sökbar | — | — |
| [arXiv](https://info.arxiv.org/help/api/user-manual.html) | Preprints: fysik, matematik, data, biologi m.fl. | Sökbar, eget frågespråk | — | Max 1 anrop/3 s ([villkor](https://info.arxiv.org/help/api/tou.html)); *"Thank you to arXiv for use of its open access interoperability."* |
| [OpenAlex](https://docs.openalex.org/) | Brett index, samtliga ämnesfält | Sökbar | — | CC0. Kostnadsbaserat sedan feb 2026 — se `kostnad_usd` i svaret; `DISCOVERY_OPENALEX_API_NYCKEL` höjer det dagliga taket |
| [Unpaywall](https://unpaywall.org/products/api) | Open access-status för en DOI | Berikning (`discovery_oa_lank`) | `DISCOVERY_KONTAKT_EPOST` | Ingen sökning (`/v2/search` är trasig hos källan och används inte) |
| [Semantic Scholar](https://api.semanticscholar.org/api-docs/graph) | Citeringsgraf | Berikning (`discovery_citeringar`) | — (nyckel rekommenderas) | Attribution krävs, ingen vidaredistribution i bulk — uppfylls redan av en databasfri server |
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

Alla källor utom Libris, Crossref, DataCite och arXiv kan slås av eller
på var för sig via `DISCOVERY_<KALLA>_AKTIV` i `.env`, utan kodändring.
De fyra har egna verktyg och är alltid aktiva. En
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

## Installation

Kräver Python med `mcp` 2.x (`mcp>=2.0,<3`).

1. Klona repot.
2. Skapa ett Python-venv och installera beroenden:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
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

Käll-specifik sökning:

- `cr_sok(titel="attention is all you need", typ="proceedings-article")` → Crossref med fältfilter.
- `dc_sok(q="ocean temperature", typ="dataset", utgivare="PANGAEA")` → DataCite med typ- och utgivarfilter.
- `arxiv_sok(q="au:hinton AND cat:cs.LG")` → arXiv med fältprefix och kategori.

## Lägga till en ny källa

Discovery är byggd för att kopplas på fler källor. Mönstret för en ny,
av/på-bar källa (allt utom Libris/Crossref/DataCite/arXiv, som har ett äldre
inline-mönster sedan innan av/på-systemet fanns):

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
