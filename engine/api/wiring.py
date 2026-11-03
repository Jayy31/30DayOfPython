"""Builds the API for a running App and serves it with uvicorn inside the engine's event loop."""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI

from engine.api.readers import MemoryReader, Reader, SqlReader
from engine.api.server import create_api
from engine.app import App
from engine.store.base import MemoryStore
from engine.store.sql import SqlStore


def reader_for(app: App) -> Reader:
    if isinstance(app.store, SqlStore):
        return SqlReader(app.store.engine)
    assert isinstance(app.store, MemoryStore)
    return MemoryReader(app.store)


def api_for(app: App) -> FastAPI:
    s = app.settings
    return create_api(
        reader_for(app),
        lambda: app.live.snapshot() if app.live else None,
        token=s.engine_api_token.get_secret_value() if s.engine_api_token else None,
        starting_capital=app.cfg.rules.capital,
        cors_origins=s.cors_origins(),
        mode=s.app_mode.value,
    )


async def serve(app: App) -> None:
    cfg = uvicorn.Config(api_for(app), host=app.settings.engine_api_host, port=app.settings.engine_api_port, log_config=None, access_log=False)
    await uvicorn.Server(cfg).serve()
