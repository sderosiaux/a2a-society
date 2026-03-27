from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import uvicorn
from a2a.server.apps.jsonrpc.fastapi_app import A2AFastAPIApplication
from a2a.server.events import InMemoryQueueManager
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from hive.auth import require_auth
from hive.budget import BudgetManager
from hive.client import A2AClient
from hive.discovery import DiscoveryClient
from hive.executor import ClaudeExecutor, TaskWorker, create_executor
from hive.initiative import InitiativeLoop
from hive.models import AgentConfig
from hive.queue import TaskQueue
from hive.subtask_tracker import SubtaskTracker

logger = logging.getLogger(__name__)


class CallbackRequest(BaseModel):
    task_id: str
    status: Literal["completed", "failed", "rejected"] = "completed"
    result: dict | None = None
    from_agent: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_knowledge_files(knowledge_dir: str) -> str | None:
    """Read all .md files from a knowledge directory and concatenate contents."""
    p = Path(knowledge_dir)
    if not p.is_dir():
        logger.warning("Knowledge dir not found: %s", knowledge_dir)
        return None
    parts: list[str] = []
    for md_file in sorted(p.glob("*.md")):
        try:
            parts.append(md_file.read_text())
        except Exception:
            logger.warning("Failed to read knowledge file: %s", md_file)
    return "\n\n".join(parts) if parts else None


async def _send_intro_message(
    config: AgentConfig,
    discovery: DiscoveryClient,
    a2a_client: A2AClient,
) -> None:
    """Send an introduction message to the superior agent after joining."""
    agents = await discovery.discover_all()
    superior = None
    for agent in agents:
        if agent.get("name") == config.reports_to:
            superior = agent
            break
    if not superior:
        logger.warning(
            "Superior '%s' not found in registry; skipping intro message",
            config.reports_to,
        )
        return
    skills_str = ", ".join(s.id for s in config.skills)
    objectives_str = ", ".join(config.objectives) if config.objectives else "none"
    intro = (
        f"I just joined as {config.role}. "
        f"My skills: [{skills_str}]. "
        f"My objectives: [{objectives_str}]. "
        f"How can I help?"
    )
    try:
        await a2a_client.send_task(
            peer_url=superior["url"],
            message_text=intro,
            from_agent=config.name,
        )
        logger.info("Sent intro message to %s", config.reports_to)
    except Exception as exc:
        logger.warning("Failed to send intro to %s: %s", config.reports_to, exc)


async def _budget_reset_loop(budget_mgr: BudgetManager) -> None:
    """Background task that resets daily/weekly budgets at midnight UTC."""
    while True:
        now = datetime.now(UTC)
        tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        seconds_until_midnight = (tomorrow - now).total_seconds()
        await asyncio.sleep(seconds_until_midnight)

        budget_mgr.reset_daily()
        logger.info("Budget daily reset completed")

        # Monday = weekday 0
        now_after = datetime.now(UTC)
        if now_after.weekday() == 0:
            budget_mgr.reset_weekly()
            logger.info("Budget weekly reset completed (Monday)")


# ---------------------------------------------------------------------------
# Card builders
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(config: AgentConfig, *, use_echo: bool = False) -> FastAPI:
    """Build and return the FastAPI A2A application for the given agent config."""
    agent_card = _build_agent_card(config)
    reg_card = _build_registration_card(config)
    budget_mgr = BudgetManager(config.budget)

    # I7: Load knowledge files at startup
    knowledge_content: str | None = None
    if config.knowledge_dir:
        knowledge_content = _load_knowledge_files(config.knowledge_dir)

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
        knowledge_content=knowledge_content,
    )

    task_store = InMemoryTaskStore()
    queue_manager = InMemoryQueueManager()
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
        queue_manager=queue_manager,
    )

    require_auth(config.auth_token)

    worker: TaskWorker | None = None
    reset_task: asyncio.Task | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal worker, reset_task

        # Startup: register + discover peers
        if config.registry_url:
            await discovery.register(reg_card)

            # I2: heartbeat with fresh budget data
            def _heartbeat_card_builder() -> dict[str, Any]:
                fresh = _build_registration_card(config)
                fresh["hive"]["budget"] = budget_mgr.to_heartbeat_data()
                fresh["hive"]["status"] = budget_mgr.status.value
                return fresh

            await discovery.start_heartbeat(
                reg_card, card_builder=_heartbeat_card_builder
            )
            await discovery.discover_all()

        if config.peers:
            await discovery.fetch_peer_cards()
        app.state.discovery = discovery  # type: ignore[attr-defined]
        app.state.subtask_tracker = subtask_tracker  # type: ignore[attr-defined]
        app.state.task_queue = task_queue  # type: ignore[attr-defined]

        # I1: Send intro message to superior
        if config.reports_to and config.registry_url:
            try:
                await _send_intro_message(config, discovery, a2a_client)
            except Exception as exc:
                logger.warning("Intro message failed: %s", exc)

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

        # I5: Start budget reset scheduler
        reset_task = asyncio.create_task(_budget_reset_loop(budget_mgr))

        yield
        # Shutdown
        if reset_task:
            reset_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reset_task
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

    # I3: Admin shutdown endpoint
    @app.post("/admin/shutdown")
    async def admin_shutdown() -> JSONResponse:
        """Gracefully shut down the agent: deregister, notify superior, stop."""
        # Send goodbye to superior
        if config.reports_to and config.registry_url:
            try:
                agents = await discovery.discover_all()
                superior = next(
                    (a for a in agents if a.get("name") == config.reports_to),
                    None,
                )
                if superior:
                    await a2a_client.send_task(
                        peer_url=superior["url"],
                        message_text=f"{config.name} is leaving the network. Goodbye.",
                        from_agent=config.name,
                    )
            except Exception as exc:
                logger.warning("Failed to send goodbye: %s", exc)

        # Deregister from registry
        if config.registry_url:
            await discovery.deregister(config.name)

        return JSONResponse({"ok": True, "message": "Shutting down"})

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
