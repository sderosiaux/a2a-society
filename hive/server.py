from __future__ import annotations

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from a2a.server.apps.jsonrpc.fastapi_app import A2AFastAPIApplication
from a2a.server.events import InMemoryQueueManager
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill

from hive.executor import create_executor
from hive.models import AgentConfig


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


def create_app(config: AgentConfig, *, use_echo: bool = False) -> FastAPI:
    """Build and return the FastAPI A2A application for the given agent config."""
    agent_card = _build_agent_card(config)
    executor = create_executor(config, use_echo=use_echo)
    task_store = InMemoryTaskStore()
    queue_manager = InMemoryQueueManager()
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
        queue_manager=queue_manager,
    )
    a2a_app = A2AFastAPIApplication(agent_card, handler)
    app = a2a_app.build()

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
