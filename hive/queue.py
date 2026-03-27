from __future__ import annotations

import asyncio
import heapq
import time
from dataclasses import dataclass, field
from enum import IntEnum


class TaskPriority(IntEnum):
    ESCALATION = 1  # highest
    FROM_SUPERIOR = 2
    CONSULTATION = 3
    BROADCAST = 4  # lowest


@dataclass(order=True)
class QueuedTask:
    priority: int
    timestamp: float = field(compare=True)
    task_id: str = field(compare=False)
    message_text: str = field(compare=False)
    metadata: dict = field(compare=False, default_factory=dict)
    context_id: str = field(compare=False, default="")


class TaskQueueFullError(Exception):
    """Raised when queue is at capacity."""


class TaskQueue:
    """Priority-sorted task inbox for an agent.

    Uses asyncio.Condition for race-free enqueue/dequeue coordination.
    """

    def __init__(self, max_backlog: int = 10, agent_superior: str | None = None):
        self._queue: list[QueuedTask] = []
        self._max_backlog = max_backlog
        self._agent_superior = agent_superior
        self._cond = asyncio.Condition()

    def _classify_priority(self, metadata: dict) -> TaskPriority:
        """Determine priority based on metadata.

        - metadata.priority == "escalation" -> ESCALATION
        - metadata.from_agent == self._agent_superior -> FROM_SUPERIOR
        - metadata.priority == "broadcast" -> BROADCAST
        - else -> CONSULTATION
        """
        priority_val = metadata.get("priority", "")
        if priority_val == "escalation":
            return TaskPriority.ESCALATION
        if self._agent_superior and metadata.get("from_agent") == self._agent_superior:
            return TaskPriority.FROM_SUPERIOR
        if priority_val == "broadcast":
            return TaskPriority.BROADCAST
        return TaskPriority.CONSULTATION

    async def enqueue(
        self,
        task_id: str,
        message_text: str,
        metadata: dict,
        context_id: str = "",
    ) -> None:
        """Add task to queue. Raises TaskQueueFullError if at max_backlog."""
        async with self._cond:
            if len(self._queue) >= self._max_backlog:
                raise TaskQueueFullError(
                    f"Queue full ({self._max_backlog} tasks)"
                )
            priority = self._classify_priority(metadata)
            task = QueuedTask(
                priority=int(priority),
                timestamp=time.monotonic(),
                task_id=task_id,
                message_text=message_text,
                metadata=metadata,
                context_id=context_id,
            )
            heapq.heappush(self._queue, task)
            self._cond.notify()

    async def dequeue(self) -> QueuedTask:
        """Pop highest priority task. Blocks until a task is available."""
        async with self._cond:
            while not self._queue:
                await self._cond.wait()
            return heapq.heappop(self._queue)

    def size(self) -> int:
        """Current queue depth."""
        return len(self._queue)

    def is_full(self) -> bool:
        """True if at max_backlog."""
        return len(self._queue) >= self._max_backlog
