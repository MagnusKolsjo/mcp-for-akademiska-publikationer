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
