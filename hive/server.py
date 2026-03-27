from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from a2a.server.apps.jsonrpc.fastapi_app import A2AFastAPIApplication
from a2a.server.events import InMemoryQueueManager
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill

from hive.budget import BudgetManager
from hive.client import A2AClient
from hive.discovery import DiscoveryClient
from hive.executor import create_executor
from hive.initiative import InitiativeLoop
from hive.models import AgentConfig
from hive.subtask_tracker import SubtaskTracker

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
    budget_mgr = BudgetManager(config.budget)
    executor = create_executor(config, use_echo=use_echo, budget_manager=budget_mgr)
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
    subtask_tracker = SubtaskTracker()

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
        app.state.subtask_tracker = subtask_tracker  # type: ignore[attr-defined]

        # Start initiative loop if agent has objectives
        initiative: InitiativeLoop | None = None
        if config.objectives:
            from hive.claude import invoke_claude

            a2a_client = A2AClient()
            initiative = InitiativeLoop(
                config=config,
                claude_fn=invoke_claude,
                client=a2a_client,
                discovery=discovery,
                budget=budget_mgr,
                queue=None,  # queue wiring added when TaskWorker is integrated
            )
            await initiative.start()
            app.state.initiative = initiative  # type: ignore[attr-defined]

        yield
        # Shutdown
        if initiative:
            await initiative.stop()
        await discovery.stop_heartbeat()
        await discovery.close()

    a2a_app = A2AFastAPIApplication(agent_card, handler)
    app = a2a_app.build(lifespan=lifespan)

    # Eagerly set on state so it's available even without lifespan (tests)
    app.state.subtask_tracker = subtask_tracker  # type: ignore[attr-defined]
    app.state.budget = budget_mgr  # type: ignore[attr-defined]

    @app.get("/status")
    async def status() -> JSONResponse:
        return JSONResponse(
            {
                "name": config.name,
                "role": config.role,
                "status": budget_mgr.status.value,
                "budget_remaining": budget_mgr.remaining_today,
                "queue_depth": 0,
            }
        )

    @app.post("/callbacks")
    async def handle_callback(request: Request) -> JSONResponse:
        """Receive push notification from a peer when a delegated subtask completes.

        Expected body:
        {
            "task_id": "...",
            "status": "completed" | "failed" | "rejected",
            "result": { "text": "...", "artifact_ref": {...} },
            "from_agent": "..."
        }
        """
        body = await request.json()
        task_id = body.get("task_id")
        cb_status = body.get("status", "completed")
        result = body.get("result")
        from_agent = body.get("from_agent", "unknown")

        if not task_id:
            return JSONResponse({"error": "missing task_id"}, status_code=400)

        tracker: SubtaskTracker = request.app.state.subtask_tracker

        if cb_status == "completed":
            parent_id = tracker.complete_subtask(task_id, result)
        else:
            reason = (result or {}).get("reason", cb_status)
            parent_id = tracker.fail_subtask(task_id, reason)

        if parent_id:
            logger.info(
                "All subtasks resolved for parent %s (callback from %s)",
                parent_id,
                from_agent,
            )
            # TODO: re-queue parent task for synthesis (Task 4.1)

        return JSONResponse({"ok": True, "parent_ready": parent_id is not None})

    return app


def run_server(config: AgentConfig) -> None:
    """Run the A2A server with uvicorn."""
    app = create_app(config)
    uvicorn.run(app, host=config.host, port=config.port)
