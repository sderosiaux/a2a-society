from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SubtaskInfo:
    subtask_id: str
    peer_name: str
    status: str = "pending"  # pending | completed | failed | rejected
    result: dict | None = None


class SubtaskTracker:
    """Tracks delegated subtasks and their completion for parent tasks."""

    def __init__(self) -> None:
        self._parent_to_subtasks: dict[str, list[SubtaskInfo]] = {}
        self._subtask_to_parent: dict[str, str] = {}

    def register_subtask(
        self, parent_task_id: str, subtask_id: str, peer_name: str
    ) -> None:
        """Register a new subtask under a parent task."""
        info = SubtaskInfo(subtask_id=subtask_id, peer_name=peer_name)
        self._parent_to_subtasks.setdefault(parent_task_id, []).append(info)
        self._subtask_to_parent[subtask_id] = parent_task_id

    def complete_subtask(
        self, subtask_id: str, result: dict | None = None
    ) -> str | None:
        """Mark a subtask as completed with optional result.

        Returns parent_task_id if ALL subtasks for that parent are now done.
        Returns None otherwise.
        """
        parent_id = self._subtask_to_parent.get(subtask_id)
        if parent_id is None:
            return None
        for info in self._parent_to_subtasks.get(parent_id, []):
            if info.subtask_id == subtask_id:
                info.status = "completed"
                info.result = result
                break
        if self.is_parent_ready(parent_id):
            return parent_id
        return None

    def fail_subtask(self, subtask_id: str, reason: str) -> str | None:
        """Mark subtask as failed. Returns parent_task_id if all subtasks resolved."""
        parent_id = self._subtask_to_parent.get(subtask_id)
        if parent_id is None:
            return None
        for info in self._parent_to_subtasks.get(parent_id, []):
            if info.subtask_id == subtask_id:
                info.status = "failed"
                info.result = {"reason": reason}
                break
        if self.is_parent_ready(parent_id):
            return parent_id
        return None

    def is_parent_ready(self, parent_task_id: str) -> bool:
        """True if all subtasks for this parent are completed or failed."""
        subtasks = self._parent_to_subtasks.get(parent_task_id)
        if not subtasks:
            return False
        return all(s.status in ("completed", "failed") for s in subtasks)

    def get_subtask_results(self, parent_task_id: str) -> list[SubtaskInfo]:
        """Get all subtask infos for a parent task."""
        return list(self._parent_to_subtasks.get(parent_task_id, []))

    def get_parent_for_subtask(self, subtask_id: str) -> str | None:
        """Look up which parent task owns this subtask."""
        return self._subtask_to_parent.get(subtask_id)

    def cleanup(self, parent_task_id: str) -> None:
        """Remove all tracking data for a completed parent task."""
        subtasks = self._parent_to_subtasks.pop(parent_task_id, [])
        for info in subtasks:
            self._subtask_to_parent.pop(info.subtask_id, None)
