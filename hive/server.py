from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from a2a.server.apps.jsonrpc.fastapi_app import A2AFastAPIApplication
from a2a.server.events import InMemoryQueueManager
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill

from hive.discovery import DiscoveryClient
from hive.executor import create_executor
from hive.models import AgentConfig

logger = logging.getLogger(__name__)


def _build_agent_card(config: AgentConfig) -> AgentCard:
    """Build an A2A AgentCard from a Hive AgentConfig."""
    skills = [
        AgentSkill(
            id=s.id,
            name=s.name,
            description=s.name,
            tags=[s.id],
        )
        for s in config.skills
    ]
    return AgentCard(
        name=config.name,
        description=config.description or config.role,
        url=f"http://{config.host}:{config.port}",
        version="0.1.0",
        defaultInputModes=["text/plain"],
        defaultOutputModes=["text/plain"],
        capabilities=AgentCapabilities(streaming=False),
        skills=skills,
    )


def _build_registration_card(config: AgentConfig) -> dict[str, Any]:
    """Build the JSON dict to POST to the registry."""
    return {
        "name": config.name,
        "description": config.description or config.role,
        "url": f"http://{config.host}:{config.port}",
        "skills": [{"id": s.id, "name": s.name} for s in config.skills],
        "hive": {
            "role": config.role,
            "reports_to": config.reports_to,
            "tools_exclusive": config.tools_exclusive,
            "objectives": config.objectives,
            "status": "active",
            "budget": {
                "remaining_today_usd": config.budget.daily_max_usd,
                "daily_max": config.budget.daily_max_usd,
                "weekly_max": config.budget.weekly_max_usd,
            },
        },
    }


def create_app(config: AgentConfig, *, use_echo: bool = False) -> FastAPI:
    """Build and return the FastAPI A2A application for the given agent config."""
    agent_card = _build_agent_card(config)
    reg_card = _build_registration_card(config)
    executor = create_executor(config, use_echo=use_echo)
    task_store = InMemoryTaskStore()
    queue_manager = InMemoryQueueManager()
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
        queue_manager=queue_manager,
    )

    discovery = DiscoveryClient(
        registry_url=config.registry_url,
        peers=[{"url": p.url} for p in config.peers],
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup: register + discover peers
        if config.registry_url:
            await discovery.register(reg_card)
            await discovery.start_heartbeat(reg_card)
            await discovery.discover_all()
        if config.peers:
            await discovery.fetch_peer_cards()
        app.state.discovery = discovery  # type: ignore[attr-defined]
        yield
        # Shutdown
        await discovery.stop_heartbeat()
        await discovery.close()

    a2a_app = A2AFastAPIApplication(agent_card, handler)
    app = a2a_app.build(lifespan=lifespan)

    budget_remaining = config.budget.daily_max_usd

    @app.get("/status")
    async def status() -> JSONResponse:
        return JSONResponse(
            {
                "name": config.name,
                "role": config.role,
                "status": "active",
                "budget_remaining": budget_remaining,
                "queue_depth": 0,
            }
        )

    return app


def run_server(config: AgentConfig) -> None:
    """Run the A2A server with uvicorn."""
    app = create_app(config)
    uvicorn.run(app, host=config.host, port=config.port)
