# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Uppstart och transportval för MCP-servern: stdio eller http.

stdio och http är symmetriska val. stdio passar en lokal MCP-klient, där
klienten startar processen direkt. http passar delad drift bakom en
reverse proxy, där en serverprocess betjänar flera klienter.

http-läget är fail-closed: utan MCP_API_KEY avbryts uppstarten med
exitkod 2. En öppen endpoint mot en databas med cachade dokument ska inte
kunna uppstå av misstag. stdio-läget berörs aldrig av autentiseringen.

Modulen läser inte .env. Servern gör det själv innan den importerar sin
konfiguration, eftersom servern inte ärver shell-miljön från klienten.

Miljövariabler:
    MCP_TRANSPORT  stdio | http            (standard: stdio)
    MCP_HOST       adress i http-läget     (standard: 127.0.0.1)
    MCP_PORT       port i http-läget       (standard: serverns egen)
    MCP_API_KEY    Bearer-nyckel, krävs i http-läget

Ingångspunkt:
    starta(mcp, standardport, initiera=None, forvarm_http=None) -> None
"""

from __future__ import annotations

import hmac
import logging
import os
import sys
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

logger = logging.getLogger(__name__)


def starta(
    mcp: "MCPServer",
    standardport: int,
    initiera: Callable[[], None] | None = None,
    forvarm_http: Callable[[], None] | None = None,
) -> None:
    """Kör valfri initiering och startar den transport MCP_TRANSPORT anger.

    `initiera` körs i båda lägena och fångas i try/except. Servern ska gå
    upp i MCP-klienten även när databasen är nere; verktygsanropen felar
    då med ett begripligt meddelande, vilket är ett tydligare besked än en
    server som saknas i listan.

    `forvarm_http` körs bara i http-läget, före första anropet. Den är till
    för tunga resurser som embeddingmodeller: i stdio-läget laddas de hellre
    lat, så att klienten inte väntar på dem vid start.
    """
    if initiera is not None:
        try:
            initiera()
        except Exception as fel:
            logger.warning("Initiering misslyckades: %s. Fortsätter ändå.", fel)

    transport = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()

    if transport == "stdio":
        mcp.run(transport="stdio")
        return

    if transport == "http":
        api_nyckel = os.getenv("MCP_API_KEY", "").strip()
        if not api_nyckel:
            logger.error(
                "MCP_API_KEY saknas. Uppstart i http-läge avbryts. "
                "Sätt nyckeln i .env eller använd MCP_TRANSPORT=stdio."
            )
            sys.exit(2)
        if forvarm_http is not None:
            forvarm_http()
        host = os.getenv("MCP_HOST", "127.0.0.1")
        port = int(os.getenv("MCP_PORT", str(standardport)))
        _starta_http(mcp, host=host, port=port, api_nyckel=api_nyckel)
        return

    logger.error(
        "Okänt värde för MCP_TRANSPORT: %r. Tillåtna värden: stdio, http.",
        transport,
    )
    sys.exit(2)


def _starta_http(mcp: "MCPServer", host: str, port: int, api_nyckel: str) -> None:
    """Startar Streamable HTTP via uvicorn, bakom Bearer-autentisering.

    uvicorn och starlette importeras här, så att stdio-läget fungerar även
    där de inte är installerade.
    """
    import uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    nyckel_bytes = api_nyckel.encode()

    class BearerAuth(BaseHTTPMiddleware):
        """Kräver `Authorization: Bearer <nyckel>` på alla anrop."""

        async def dispatch(self, request: Request, call_next):
            auth = request.headers.get("authorization", "")
            if not auth.lower().startswith("bearer "):
                return JSONResponse(
                    {"fel": "Authorization-header saknas."}, status_code=401
                )
            given = auth.split(" ", 1)[1].strip().encode()
            # Jämförelse i konstant tid, så att nyckeln inte kan gissas
            # tecken för tecken utifrån svarstiden.
            if not hmac.compare_digest(given, nyckel_bytes):
                return JSONResponse({"fel": "Ogiltig API-nyckel."}, status_code=403)
            return await call_next(request)

    app = mcp.streamable_http_app(host=host)
    app.add_middleware(BearerAuth)
    logger.info("MCP-servern lyssnar på http://%s:%s/mcp", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")
