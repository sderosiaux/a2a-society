from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Literal

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from a2a.server.apps.jsonrpc.fastapi_app import A2AFastAPIApplication
from a2a.server.events import InMemoryQueueManager
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill

from hive.auth import require_auth
from hive.budget import BudgetManager
from hive.client import A2AClient
from hive.discovery import DiscoveryClient
from hive.executor import ClaudeExecutor, TaskWorker, create_executor
from hive.initiative import InitiativeLoop
from hive.models import AgentConfig
from hive.queue import TaskPriority, TaskQueue
from hive.subtask_tracker import SubtaskTracker

logger = logging.getLogger(__name__)


class CallbackRequest(BaseModel):
    task_id: str
    status: Literal["completed", "failed", "rejected"] = "completed"
    result: dict | None = None
    from_agent: str | None = None


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
    """Build the JSON dict to POST to the registry.

    Derives base fields from _build_agent_card() to stay in sync,
    then layers hive-specific extensions on top.
    """
    card = _build_agent_card(config)
    base: dict[str, Any] = {
        "name": card.name,
        "description": card.description,
        "url": card.url,
        "skills": [{"id": s.id, "name": s.name} for s in card.skills],
    }
    base["hive"] = {
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
    }
    return base


_PUBLIC_PATHS = {
    "/.well-known/agent.json",
    "/.well-known/agent-card.json",
}


def create_app(config: AgentConfig, *, use_echo: bool = False) -> FastAPI:
    """Build and return the FastAPI A2A application for the given agent config."""
    agent_card = _build_agent_card(config)
    reg_card = _build_registration_card(config)
    budget_mgr = BudgetManager(config.budget)

    # Build task queue and components
    task_queue = TaskQueue(max_backlog=10, agent_superior=config.reports_to)
    subtask_tracker = SubtaskTracker()

    discovery = DiscoveryClient(
        registry_url=config.registry_url,
        peers=[{"url": p.url} for p in config.peers],
        auth_token=config.auth_token,
    )

    a2a_client = A2AClient()

    executor = create_executor(
        config,
        use_echo=use_echo,
        budget_manager=budget_mgr,
        task_queue=task_queue if not use_echo else None,
        discovery=discovery,
        subtask_tracker=subtask_tracker,
        a2a_client=a2a_client,
    )

    task_store = InMemoryTaskStore()
    queue_manager = InMemoryQueueManager()
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
        queue_manager=queue_manager,
    )

    auth_dep = require_auth(config.auth_token)

    worker: TaskWorker | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal worker

        # Startup: register + discover peers
        if config.registry_url:
            await discovery.register(reg_card)
            await discovery.start_heartbeat(reg_card)
            await discovery.discover_all()
        if config.peers:
            await discovery.fetch_peer_cards()
        app.state.discovery = discovery  # type: ignore[attr-defined]
        app.state.subtask_tracker = subtask_tracker  # type: ignore[attr-defined]
        app.state.task_queue = task_queue  # type: ignore[attr-defined]

        # Start TaskWorker for queue-based execution (production only)
        if not use_echo and isinstance(executor, ClaudeExecutor):
            worker = TaskWorker(task_queue, executor)
            await worker.start()
            app.state.worker = worker  # type: ignore[attr-defined]

        # Start initiative loop if agent has objectives
        initiative: InitiativeLoop | None = None
        if config.objectives:
            from hive.claude import invoke_claude

            initiative = InitiativeLoop(
                config=config,
                claude_fn=invoke_claude,
                client=a2a_client,
                discovery=discovery,
                budget=budget_mgr,
                queue=task_queue,
            )
            await initiative.start()
            app.state.initiative = initiative  # type: ignore[attr-defined]

        yield
        # Shutdown
        if worker:
            await worker.stop()
        if initiative:
            await initiative.stop()
        await discovery.stop_heartbeat()
        await discovery.close()

    a2a_app = A2AFastAPIApplication(agent_card, handler)
    app = a2a_app.build(lifespan=lifespan)

    # Auth middleware: protect non-public paths when auth_token is set
    if config.auth_token:

        @app.middleware("http")
        async def auth_middleware(request: Request, call_next):
            if request.url.path not in _PUBLIC_PATHS:
                auth_header = request.headers.get("Authorization", "")
                if not auth_header.startswith("Bearer "):
                    return JSONResponse(
                        {"detail": "Missing bearer token"}, status_code=401
                    )
                if auth_header[7:] != config.auth_token:
                    return JSONResponse(
                        {"detail": "Invalid token"}, status_code=403
                    )
            return await call_next(request)

    # Eagerly set on state so it's available even without lifespan (tests)
    app.state.subtask_tracker = subtask_tracker  # type: ignore[attr-defined]
    app.state.budget = budget_mgr  # type: ignore[attr-defined]
    app.state.task_queue = task_queue  # type: ignore[attr-defined]

    @app.get("/status")
    async def status() -> JSONResponse:
        return JSONResponse(
            {
                "name": config.name,
                "role": config.role,
                "status": budget_mgr.status.value,
                "budget_remaining": budget_mgr.remaining_today,
                "queue_depth": task_queue.size(),
            }
        )

    @app.post("/callbacks")
    async def handle_callback(request: Request, body: CallbackRequest) -> JSONResponse:
        """Receive push notification from a peer when a delegated subtask completes."""
        task_id = body.task_id
        cb_status = body.status
        result = body.result
        from_agent = body.from_agent or "unknown"

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
            # Re-queue parent task for synthesis with high priority
            tq: TaskQueue = request.app.state.task_queue
            subtask_results = tracker.get_subtask_results(parent_id)
            results_summary = "\n".join(
                f"- [{s.peer_name}] ({s.status}): {s.result}"
                for s in subtask_results
            )
            synthesis_prompt = (
                "Your delegated subtasks have completed. "
                "Here are the results:\n"
                f"{results_summary}\n\n"
                "Synthesize a final response."
            )
            try:
                await tq.enqueue(
                    task_id=f"synthesis-{parent_id}",
                    message_text=synthesis_prompt,
                    metadata={
                        "from_agent": config.name,
                        "priority": "escalation",
                        "parent_task_id": parent_id,
                    },
                    context_id=parent_id,
                )
            except Exception:
                logger.exception("Failed to re-queue synthesis for parent %s", parent_id)

            tracker.cleanup(parent_id)

        return JSONResponse({"ok": True, "parent_ready": parent_id is not None})

    return app


def run_server(config: AgentConfig) -> None:
    """Run the A2A server with uvicorn."""
    app = create_app(config)
    uvicorn.run(app, host=config.host, port=config.port)
