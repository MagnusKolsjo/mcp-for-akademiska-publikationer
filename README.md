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
normaliserar sina träffar till en gemensam form, vilket gör att den enade
sökningen `discovery_sok` kan slå mot flera källor samtidigt och returnera en
sammanslagen träfflista. Libris har ett eget, rikare frågespråk och egna verktyg.

Nedladdning och lagring av fulltext ligger utanför den här servern — discovery
hittar och beskriver poster; en separat tjänst kan hämta och lagra dem.

## Datakällor

- **Libris** — Sveriges nationella bibliotekskatalog (KB).
  `https://libris.kb.se/find.jsonld` ([dokumentation](https://libris.kb.se/api/docs/reference/find/)),
  samt `https://id.kb.se` för länkade ämnestermer.
- **Crossref** — DOI-metadata för vetenskapliga artiklar, böcker och
  konferensbidrag. `https://api.crossref.org/works`
  ([dokumentation](https://www.crossref.org/documentation/retrieve-metadata/rest-api/)).
- **DataCite** — DOI-metadata för forskningsdata, programvara och preprints.
  `https://api.datacite.org/dois` ([dokumentation](https://support.datacite.org/docs/api)).

## Installation

1. Klona repot.
2. Skapa ett Python-venv och installera beroenden:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Kopiera `config.example.env` till `.env` och fyll i värdena. Sätt särskilt en
   egen `LIBRIS_USER_AGENT` och `DATACITE_USER_AGENT` med kontaktuppgift, och
   gärna `CROSSREF_MAILTO` för Crossrefs polite pool.
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
| `discovery_sok` | Sök Crossref och DataCite samtidigt; sammanslagna, normaliserade träffar. |
| `discovery_kallor` | Lista de DOI-källor som ingår i den enade sökningen. |
| `cr_sok` | Sök Crossref (artiklar m.m.), med fält- och årsfilter. |
| `cr_hamta` | Läs ett verk i Crossref via DOI (kort eller full). |
| `dc_sok` | Sök DataCite (forskningsdata m.m.), med typ-, års- och utgivarfilter. |
| `dc_hamta` | Läs en post i DataCite via DOI (kort eller full). |
| `libris_sok` | Fritext- och filtersökning i Libris-katalogen. |
| `libris_hamta` | Läs en Libris-post — sammanfattning eller fullständig JSON-LD. |
| `libris_bestand` | Visa vilka bibliotek som har ett verk (sigel och namn). |
| `libris_sla_upp_term` | Översätt fritext till id.kb.se-termer för precisa filter. |

### Typiska flöden

Bred sökning över DOI-källorna:

1. `discovery_kallor()` → se vilka källor som finns.
2. `discovery_sok(q="machine learning fairness", fran_ar=2020)` → sammanslagna träffar med `doi` och `kalla`.
3. `cr_hamta(doi)` eller `dc_hamta(doi, format="full")` → läs hela posten.

Käll-specifik sökning:

- `cr_sok(titel="attention is all you need", typ="proceedings-article")` → Crossref med fältfilter.
- `dc_sok(q="ocean temperature", typ="dataset", utgivare="PANGAEA")` → DataCite med typ- och utgivarfilter.

## Lägga till en ny källa

Discovery är byggd för att kopplas på fler källor. Mönstret:

1. Skriv `<namn>_client.py` med en `sok()` som returnerar normaliserade träffar
   och en `hamta()`, plus en egen felklass — som `crossref_client.py`.
2. Registrera källan i `providers.py` om den ska ingå i `discovery_sok`.
3. Exponera käll-specifika verktyg i `mcp_server.py` om källan har egna filter.

## Transport

Servern stödjer två transporter, valda via `MCP_TRANSPORT`:

- **stdio** — MCP-klienten startar processen lokalt.
- **http** — långkörande process bakom en URL, med valfri Bearer-token-autentisering (`MCP_API_KEY`).

## Licens

AGPL-3.0.
