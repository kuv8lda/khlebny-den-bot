"""Authenticated receiver for panel catalogue updates."""

import asyncio
import hmac
import logging

from aiohttp import web

from .catalog import CATALOG
from .panel import PanelError, parse_catalog
from .storage import SqliteStorage

log = logging.getLogger(__name__)


def create_catalog_app(storage: SqliteStorage, token: str) -> web.Application:
    app = web.Application(client_max_size=1024 * 1024)
    lock = asyncio.Lock()

    async def receive(request: web.Request) -> web.Response:
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
            raise web.HTTPUnauthorized()
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("invalid payload")
            version = payload.get("version")
            if type(version) is not int or not 0 < version <= 2**63 - 1:
                raise ValueError("invalid version")
            catalog = parse_catalog(payload.get("catalog"))
        except (ValueError, PanelError):
            raise web.HTTPBadRequest() from None
        async with lock:
            applied = await storage.sync_catalog_version(catalog, version)
            if applied:
                CATALOG[:] = catalog
                log.info("catalog updated from panel (version %s)", version)
        return web.json_response({"ok": True, "applied": applied})

    app.router.add_post("/catalog", receive)
    return app
