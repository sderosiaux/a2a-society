from __future__ import annotations

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.utils import new_agent_text_message


class EchoExecutor(AgentExecutor):
    """Echo executor that returns the received message prefixed with agent name.

    Placeholder implementation. Task 1.3 replaces this with Claude Code integration.
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
