from __future__ import annotations

import asyncio
import logging

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.utils import new_agent_text_message

from hive.client import A2AClient
from hive.discovery import DiscoveryClient
from hive.models import AgentConfig
from hive.prompt_builder import build_system_prompt
from hive.queue import QueuedTask, TaskQueue
from hive.subtask_tracker import SubtaskTracker

logger = logging.getLogger(__name__)


class EchoExecutor(AgentExecutor):
    """Echo executor that returns the received message prefixed with agent name.

    Useful for testing without real Claude API calls.
    """

    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name

    async def execute(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
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

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        if context.current_task:
            updater = TaskUpdater(
                event_queue, context.task_id, context.context_id
            )
            await updater.cancel()


class ClaudeExecutor(AgentExecutor):
    """Executor that delegates to Claude Code SDK with a role-based system prompt."""

    def __init__(self, config: AgentConfig) -> None:
        self.config = config

    async def execute(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        from hive.claude import invoke_claude

        task_id = context.task_id
        context_id = context.context_id
        updater = TaskUpdater(event_queue, task_id, context_id)

        text = context.get_user_input()
        system_prompt = build_system_prompt(self.config)

        budget = self.config.budget
        max_budget = min(budget.per_task_max_usd, budget.daily_max_usd)

        response_text, _cost, _session_id = await invoke_claude(
            prompt=text,
            system_prompt=system_prompt,
            allowed_tools=self.config.tools or None,
            max_budget_usd=max_budget,
        )

        response = new_agent_text_message(
            response_text,
            context_id,
            task_id,
        )
        await updater.complete(response)

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        if context.current_task:
            updater = TaskUpdater(
                event_queue, context.task_id, context.context_id
            )
            await updater.cancel()


def create_executor(config: AgentConfig, *, use_echo: bool = False) -> AgentExecutor:
    """Factory: returns EchoExecutor for tests, ClaudeExecutor for production."""
    if use_echo:
        return EchoExecutor(config.name)
    return ClaudeExecutor(config)


class TaskWorker:
    """Background worker that processes tasks from the queue one at a time."""

    def __init__(self, queue: TaskQueue, executor: AgentExecutor) -> None:
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
                logger.exception(
                    "Worker failed processing task %s", queued.task_id
                )

    async def _process(self, queued: QueuedTask) -> None:
        logger.info(
            "Processing task %s (priority=%s)", queued.task_id, queued.priority
        )
        # Future: create RequestContext + EventQueue and call executor.execute()
        # For now, log that the task was dequeued for processing.

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
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
