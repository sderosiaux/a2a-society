from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState
from a2a.utils import new_agent_text_message

from hive.budget import BudgetManager
from hive.client import A2AClient
from hive.discovery import DiscoveryClient
from hive.models import AgentConfig, BudgetStatus
from hive.org_memory import OrgMemory
from hive.prompt_builder import build_system_prompt
from hive.queue import QueuedTask, TaskQueue, TaskQueueFullError
from hive.subtask_tracker import SubtaskTracker

ARTIFACT_LINE_THRESHOLD = 50
DELEGATION_PATTERN = re.compile(
    r'\{"delegate"\s*:\s*\{[^}]*"skill"\s*:\s*"[^"]*"[^}]*\}\}',
    re.DOTALL,
)

logger = logging.getLogger(__name__)


def parse_delegation(text: str) -> dict | None:
    """Extract a delegation intent from Claude's response.

    Looks for: {"delegate": {"skill": "...", "message": "..."}}
    Returns the inner dict {"skill": ..., "message": ...} or None.
    """
    match = DELEGATION_PATTERN.search(text)
    if not match:
        return None
    try:
        parsed = json.loads(match.group())
        delegate = parsed.get("delegate", {})
        if delegate.get("skill"):
            return delegate
    except (json.JSONDecodeError, AttributeError):
        pass
    return None


class EchoExecutor(AgentExecutor):
    """Echo executor that returns the received message prefixed with agent name.

    Useful for testing without real Claude API calls.
    """

    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id
        context_id = context.context_id
        updater = TaskUpdater(event_queue, task_id, context_id)

        text = context.get_user_input()
        response = new_agent_text_message(
            f"[{self.agent_name}] received: {text}",
            context_id,
            task_id,
        )
        await updater.complete(response)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.current_task:
            updater = TaskUpdater(event_queue, context.task_id, context.context_id)
            await updater.cancel()


class ClaudeExecutor(AgentExecutor):
    """Executor that routes tasks through the priority queue and delegates to Claude.

    When a TaskQueue is attached, tasks are enqueued and processed by the
    background TaskWorker in priority order.  When no queue is attached (e.g.
    in tests), tasks are executed inline as before.
    """

    def __init__(
        self,
        config: AgentConfig,
        budget_manager: BudgetManager | None = None,
        org_memory: OrgMemory | None = None,
        task_queue: TaskQueue | None = None,
        discovery: DiscoveryClient | None = None,
        subtask_tracker: SubtaskTracker | None = None,
        a2a_client: A2AClient | None = None,
        knowledge_content: str | None = None,
    ) -> None:
        self.config = config
        self.budget = budget_manager
        self.org_memory = org_memory
        self.task_queue = task_queue
        self.discovery = discovery
        self.subtask_tracker = subtask_tracker
        self.a2a_client = a2a_client
        self.knowledge_content = knowledge_content
        # Map of task_id -> Future for queue-based execution signaling
        self._pending_futures: dict[str, asyncio.Future] = {}

    @staticmethod
    def _slugify_role(role: str) -> str:
        """Convert role to a slug for artifact domain: 'SEO Specialist' -> 'seo-specialist'."""
        slug = re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-")
        return slug or "general"

    def _extract_artifact_ref(self, context: RequestContext) -> dict | None:
        """Extract artifact_ref from incoming message metadata, if present."""
        msg = context.message
        if msg and msg.metadata and "artifact_ref" in msg.metadata:
            return msg.metadata["artifact_ref"]
        return None

    def _resolve_inbound_artifact(self, artifact_ref: dict) -> str | None:
        """Pull org_memory and read the referenced artifact file."""
        if not self.org_memory:
            return None
        try:
            self.org_memory.pull()
            return self.org_memory.read_file(artifact_ref["path"])
        except Exception:
            logger.warning("Failed to read artifact %s", artifact_ref.get("path"))
            return None

    async def _notify_vacation(self) -> None:
        """Send a vacation notification to the superior agent."""
        if not self.config.reports_to or not self.discovery or not self.a2a_client:
            return
        agents = await self.discovery.discover_all()
        superior = next(
            (a for a in agents if a.get("name") == self.config.reports_to),
            None,
        )
        if not superior:
            logger.warning("Superior '%s' not found for vacation notice", self.config.reports_to)
            return
        msg = f"Budget depleted ({self.config.name}). On vacation until next reset."
        await self.a2a_client.send_task(
            peer_url=superior["url"],
            message_text=msg,
            from_agent=self.config.name,
        )
        logger.info("Sent vacation notification to %s", self.config.reports_to)

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Route the task through the priority queue if available.

        If queue is full, reject the task immediately.
        If queue is available, enqueue and wait for the worker to process.
        If no queue, execute inline (backward compat / tests).
        """
        task_id = context.task_id
        context_id = context.context_id
        updater = TaskUpdater(event_queue, task_id, context_id)

        if self.task_queue is not None:
            # Extract metadata from the incoming message
            msg = context.message
            metadata = dict(msg.metadata) if msg and msg.metadata else {}
            text = context.get_user_input()

            # Try to enqueue
            try:
                await self.task_queue.enqueue(
                    task_id=task_id,
                    message_text=text,
                    metadata=metadata,
                    context_id=context_id,
                )
            except TaskQueueFullError:
                response = new_agent_text_message(
                    "At capacity — queue full. Task rejected.",
                    context_id,
                    task_id,
                )
                await updater.failed(response)
                return

            # Create a future for the worker to signal completion
            loop = asyncio.get_running_loop()
            future: asyncio.Future = loop.create_future()
            self._pending_futures[task_id] = future

            # Wait for the worker to process and provide context + event_queue
            # Store context/event_queue so the worker can use them
            future.context = context  # type: ignore[attr-defined]
            future.event_queue = event_queue  # type: ignore[attr-defined]

            await future  # blocks until worker resolves it
            self._pending_futures.pop(task_id, None)
        else:
            # No queue: execute inline (tests, simple setups)
            await self._execute_task(context, event_queue)

    async def _execute_task(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Core execution logic: call Claude, handle delegation, complete task."""
        from hive.claude import invoke_claude

        task_id = context.task_id
        context_id = context.context_id
        updater = TaskUpdater(event_queue, task_id, context_id)

        # Budget guard
        if self.budget:
            allowed, max_budget = self.budget.check_before_execution()
            if not allowed:
                response = new_agent_text_message(
                    "On vacation — budget exhausted. Task rejected.",
                    context_id,
                    task_id,
                )
                await updater.complete(response)
                return
        else:
            budget_cfg = self.config.budget
            max_budget = min(budget_cfg.per_task_max_usd, budget_cfg.daily_max_usd)

        text = context.get_user_input()

        # --- Inbound: resolve artifact_ref if present ---
        artifact_ref = self._extract_artifact_ref(context)
        if artifact_ref and self.org_memory:
            content = self._resolve_inbound_artifact(artifact_ref)
            if content:
                text = f"Referenced artifact:\n{content}\n\nTask: {text}"

        # Log task_received event
        if self.org_memory:
            try:
                msg = context.message
                from_agent = (msg.metadata or {}).get("from_agent", "unknown") if msg else "unknown"
                summary = text[:120]
                self.org_memory.append_event(
                    "task_received",
                    {"task_id": task_id, "from_agent": from_agent, "summary": summary},
                )
            except Exception:
                logger.warning("Failed to log task_received event for %s", task_id)

        system_prompt = build_system_prompt(self.config, knowledge_content=self.knowledge_content)

        response_text, cost_usd, _session_id = await invoke_claude(
            prompt=text,
            system_prompt=system_prompt,
            allowed_tools=self.config.tools or None,
            max_budget_usd=max_budget,
            permission_mode=self.config.permission_mode,
            max_turns=self.config.max_turns,
        )

        # Record cost
        if self.budget:
            prev_status = self.budget.status
            new_status = self.budget.record_cost(cost_usd)
            if new_status != prev_status:
                logger.info("Budget status changed: %s -> %s", prev_status.value, new_status.value)

            # I4: Write budget log to org-memory
            if self.org_memory:
                try:
                    self.org_memory.append_budget_log(self.budget.to_log_entry())
                except Exception:
                    logger.warning("Failed to write budget log for task %s", task_id)

            # I6: Notify superior on vacation
            if new_status != prev_status and new_status == BudgetStatus.vacation and self.discovery and self.a2a_client:
                try:
                    await self._notify_vacation()
                except Exception:
                    logger.warning("Failed to send vacation notification")

        # --- C5: Check for delegation intent ---
        delegation = parse_delegation(response_text)
        if delegation and self.discovery and self.a2a_client and self.subtask_tracker:
            skill = delegation["skill"]
            del_message = delegation.get("message", text)
            callback_url = f"http://{self.config.host}:{self.config.port}/callbacks"

            subtask_id = await delegate_to_peer(
                client=self.a2a_client,
                discovery=self.discovery,
                subtask_tracker=self.subtask_tracker,
                parent_task_id=task_id,
                skill_needed=skill,
                message_text=del_message,
                from_agent=self.config.name,
                callback_url=callback_url,
            )
            if subtask_id:
                # Task is now waiting for subtask completion
                working_msg = new_agent_text_message(
                    "Delegated to peer, awaiting response.",
                    context_id,
                    task_id,
                )
                await updater.update_status(TaskState.working, working_msg)
                # Log delegation event
                if self.org_memory:
                    try:
                        self.org_memory.append_event(
                            "task_delegated",
                            {"task_id": task_id, "subtask_id": subtask_id, "skill": skill},
                        )
                    except Exception:
                        logger.warning("Failed to log delegation event for %s", task_id)
                return
            # If no peer found, fall through to normal completion
            logger.info("No peer for skill %s, completing task normally", skill)

        # --- Outbound: commit large responses as artifacts ---
        response_artifact_ref = None
        line_count = response_text.count("\n") + (1 if response_text and not response_text.endswith("\n") else 0)
        if line_count > ARTIFACT_LINE_THRESHOLD and self.org_memory:
            try:
                domain = self._slugify_role(self.config.role)
                filename = f"{task_id}-response.md"
                response_artifact_ref = self.org_memory.write_artifact(domain, filename, response_text)
                first_lines = "\n".join(response_text.split("\n")[:3])
                path = response_artifact_ref["path"]
                response_text = f"{first_lines}\n... (full report: {path}, {line_count} lines)"
            except Exception:
                logger.warning("Failed to commit artifact for task %s", task_id)

        # Log task_completed event
        if self.org_memory:
            try:
                summary = response_text[:120]
                self.org_memory.append_event(
                    "task_completed",
                    {"task_id": task_id, "cost_usd": cost_usd, "summary": summary},
                )
            except Exception:
                logger.warning("Failed to log task_completed event for %s", task_id)

        response = new_agent_text_message(
            response_text,
            context_id,
            task_id,
        )
        # Attach artifact_ref to response metadata if available
        if response_artifact_ref and hasattr(response, "metadata"):
            if response.metadata is None:
                response.metadata = {}
            response.metadata["artifact_ref"] = response_artifact_ref

        await updater.complete(response)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.current_task:
            updater = TaskUpdater(event_queue, context.task_id, context.context_id)
            await updater.cancel()


def create_executor(
    config: AgentConfig,
    *,
    use_echo: bool = False,
    budget_manager: BudgetManager | None = None,
    org_memory: OrgMemory | None = None,
    task_queue: TaskQueue | None = None,
    discovery: DiscoveryClient | None = None,
    subtask_tracker: SubtaskTracker | None = None,
    a2a_client: A2AClient | None = None,
    knowledge_content: str | None = None,
) -> AgentExecutor:
    """Factory: returns EchoExecutor for tests, ClaudeExecutor for production."""
    if use_echo:
        return EchoExecutor(config.name)
    return ClaudeExecutor(
        config,
        budget_manager=budget_manager,
        org_memory=org_memory,
        task_queue=task_queue,
        discovery=discovery,
        subtask_tracker=subtask_tracker,
        a2a_client=a2a_client,
        knowledge_content=knowledge_content,
    )


class TaskWorker:
    """Background worker that dequeues tasks and processes them via the executor."""

    def __init__(self, queue: TaskQueue, executor: ClaudeExecutor) -> None:
        self._queue = queue
        self._executor = executor
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the worker loop as a background task."""
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while self._running:
            queued = await self._queue.dequeue()
            try:
                await self._process(queued)
            except Exception:
                logger.exception("Worker failed processing task %s", queued.task_id)
                # Resolve the pending future with an error so the
                # execute() caller doesn't hang forever
                future = self._executor._pending_futures.get(queued.task_id)
                if future and not future.done():
                    future.set_result(None)

    async def _process(self, queued: QueuedTask) -> None:
        logger.info("Processing task %s (priority=%s)", queued.task_id, queued.priority)
        future = self._executor._pending_futures.get(queued.task_id)
        if future is not None:
            # Execute using the stored context/event_queue from the future
            context = future.context  # type: ignore[attr-defined]
            event_queue = future.event_queue  # type: ignore[attr-defined]
            try:
                await self._executor._execute_task(context, event_queue)
            finally:
                if not future.done():
                    future.set_result(None)
        else:
            logger.warning("No pending future for task %s, skipping", queued.task_id)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


async def delegate_to_peer(
    client: A2AClient,
    discovery: DiscoveryClient,
    subtask_tracker: SubtaskTracker,
    parent_task_id: str,
    skill_needed: str,
    message_text: str,
    from_agent: str,
    callback_url: str,
) -> str | None:
    """Find a peer with the needed skill and send them a task.

    Returns subtask_id on success, None if no peer found.
    """
    peers = await discovery.discover_by_skill(skill_needed)
    if not peers:
        logger.info("No peer found for skill %s", skill_needed)
        return None

    peer = peers[0]  # simple: pick first available
    result = await client.send_task(
        peer_url=peer["url"],
        message_text=message_text,
        from_agent=from_agent,
        callback_url=callback_url,
    )

    subtask_id = result["task_id"]
    subtask_tracker.register_subtask(
        parent_task_id=parent_task_id,
        subtask_id=subtask_id,
        peer_name=peer["name"],
    )
    logger.info(
        "Delegated subtask %s to %s for parent %s",
        subtask_id,
        peer["name"],
        parent_task_id,
    )
    return subtask_id
