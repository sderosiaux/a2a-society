from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException

from hive.registry.store import RegistryStore


def create_registry_app(heartbeat_interval: int = 60) -> FastAPI:
    """Create a standalone FastAPI registry service."""
    store = RegistryStore(heartbeat_interval=heartbeat_interval)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(_heartbeat_loop(store, heartbeat_interval))
        yield
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    app = FastAPI(title="Hive Registry", lifespan=lifespan)
    # Expose store for testing.
    app.state.store = store  # type: ignore[attr-defined]

    @app.get("/agents")
    async def list_agents() -> list[dict[str, Any]]:
        return store.get_all()

    @app.post("/agents/register")
    async def register_agent(body: dict[str, Any]) -> dict[str, str]:
        store.register(body)
        return {"status": "ok"}

    @app.get("/agents/by-skill/{skill_id}")
    async def agents_by_skill(skill_id: str) -> list[dict[str, Any]]:
        return store.get_by_skill(skill_id)

    @app.get("/agents/by-role/{role}")
    async def agents_by_role(role: str) -> list[dict[str, Any]]:
        return store.get_by_role(role)

    @app.get("/agents/{name}")
    async def get_agent(name: str) -> dict[str, Any]:
        card = store.get_by_name(name)
        if card is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        return card

    return app


async def _heartbeat_loop(store: RegistryStore, interval: int) -> None:
    """Periodically check heartbeats."""
    while True:
        await asyncio.sleep(interval)
        store.check_heartbeats()
