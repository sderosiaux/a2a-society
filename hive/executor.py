from __future__ import annotations

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.utils import new_agent_text_message

from hive.models import AgentConfig
from hive.prompt_builder import build_system_prompt


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
