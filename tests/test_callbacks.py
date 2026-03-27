from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hive.client import A2AClient
from hive.discovery import DiscoveryClient
from hive.executor import delegate_to_peer
from hive.models import AgentConfig, SkillDef
from hive.server import create_app
from hive.subtask_tracker import SubtaskTracker


# -- SubtaskTracker unit tests -----------------------------------------------


class TestSubtaskTracker:
    def setup_method(self):
        self.tracker = SubtaskTracker()

    def test_complete_two_subtasks_parent_ready(self):
        """Register 2 subtasks, complete one -> not ready. Complete second -> ready."""
        self.tracker.register_subtask("parent-1", "sub-a", "agent-a")
        self.tracker.register_subtask("parent-1", "sub-b", "agent-b")

        result = self.tracker.complete_subtask("sub-a", {"text": "done-a"})
        assert result is None
        assert not self.tracker.is_parent_ready("parent-1")

        result = self.tracker.complete_subtask("sub-b", {"text": "done-b"})
        assert result == "parent-1"
        assert self.tracker.is_parent_ready("parent-1")

    def test_fail_subtask_resolves_parent(self):
        """Fail a subtask -> parent still resolves when all subtasks resolved."""
        self.tracker.register_subtask("parent-2", "sub-c", "agent-c")
        self.tracker.register_subtask("parent-2", "sub-d", "agent-d")

        result = self.tracker.fail_subtask("sub-c", "timeout")
        assert result is None

        result = self.tracker.complete_subtask("sub-d", {"text": "ok"})
        assert result == "parent-2"
        assert self.tracker.is_parent_ready("parent-2")

    def test_get_subtask_results(self):
        """get_subtask_results returns all infos with correct statuses."""
        self.tracker.register_subtask("parent-3", "sub-e", "agent-e")
        self.tracker.register_subtask("parent-3", "sub-f", "agent-f")

        self.tracker.complete_subtask("sub-e", {"text": "result-e"})
        self.tracker.fail_subtask("sub-f", "error")

        results = self.tracker.get_subtask_results("parent-3")
        assert len(results) == 2

        by_id = {r.subtask_id: r for r in results}
        assert by_id["sub-e"].status == "completed"
        assert by_id["sub-e"].result == {"text": "result-e"}
        assert by_id["sub-f"].status == "failed"
        assert by_id["sub-f"].result == {"reason": "error"}

    def test_complete_unknown_subtask_returns_none(self):
        """Complete unknown subtask_id -> returns None (no crash)."""
        result = self.tracker.complete_subtask("nonexistent")
        assert result is None

    def test_fail_unknown_subtask_returns_none(self):
        """Fail unknown subtask_id -> returns None (no crash)."""
        result = self.tracker.fail_subtask("nonexistent", "reason")
        assert result is None

    def test_get_parent_for_subtask(self):
        """get_parent_for_subtask returns correct parent."""
        self.tracker.register_subtask("parent-4", "sub-g", "agent-g")
        assert self.tracker.get_parent_for_subtask("sub-g") == "parent-4"
        assert self.tracker.get_parent_for_subtask("unknown") is None

    def test_cleanup_removes_tracking_data(self):
        """cleanup removes all tracking data for a parent."""
        self.tracker.register_subtask("parent-5", "sub-h", "agent-h")
        self.tracker.register_subtask("parent-5", "sub-i", "agent-i")

        self.tracker.cleanup("parent-5")

        assert self.tracker.get_subtask_results("parent-5") == []
        assert self.tracker.get_parent_for_subtask("sub-h") is None
        assert self.tracker.get_parent_for_subtask("sub-i") is None

    def test_is_parent_ready_no_subtasks(self):
        """is_parent_ready returns False for unknown parent."""
        assert not self.tracker.is_parent_ready("unknown-parent")


# -- Callback endpoint tests -------------------------------------------------


@pytest.fixture()
def config() -> AgentConfig:
    return AgentConfig(
        name="callback-agent",
        role="Coordinator",
        description="Agent with callback endpoint",
        skills=[SkillDef(id="coordinate", name="Coordinate")],
    )


@pytest.fixture()
def app(config: AgentConfig):
    return create_app(config, use_echo=True)


@pytest_asyncio.fixture()
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_callback_completed(client: AsyncClient, app):
    """POST /callbacks with completed subtask -> 200 response."""
    tracker: SubtaskTracker = app.state.subtask_tracker
    tracker.register_subtask("parent-cb", "sub-cb-1", "worker-1")

    resp = await client.post(
        "/callbacks",
        json={
            "task_id": "sub-cb-1",
            "status": "completed",
            "result": {"text": "done"},
            "from_agent": "worker-1",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    # Single subtask -> parent is ready
    assert data["parent_ready"] is True


@pytest.mark.asyncio
async def test_callback_failed(client: AsyncClient, app):
    """POST /callbacks with failed subtask -> 200 and correct tracking."""
    tracker: SubtaskTracker = app.state.subtask_tracker
    tracker.register_subtask("parent-fail", "sub-fail-1", "worker-1")
    tracker.register_subtask("parent-fail", "sub-fail-2", "worker-2")

    resp = await client.post(
        "/callbacks",
        json={
            "task_id": "sub-fail-1",
            "status": "failed",
            "result": {"reason": "timeout"},
            "from_agent": "worker-1",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["parent_ready"] is False

    resp = await client.post(
        "/callbacks",
        json={
            "task_id": "sub-fail-2",
            "status": "completed",
            "result": {"text": "ok"},
            "from_agent": "worker-2",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["parent_ready"] is True


@pytest.mark.asyncio
async def test_callback_missing_task_id(client: AsyncClient):
    """POST /callbacks without task_id -> 422 (Pydantic validation)."""
    resp = await client.post(
        "/callbacks",
        json={"status": "completed", "from_agent": "x"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_callback_unknown_task_id(client: AsyncClient):
    """POST /callbacks with unknown task_id -> 200 (no crash), parent_ready False."""
    resp = await client.post(
        "/callbacks",
        json={
            "task_id": "nonexistent",
            "status": "completed",
            "result": {"text": "ok"},
            "from_agent": "unknown",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["parent_ready"] is False


# -- delegate_to_peer tests --------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_to_peer_success():
    """Mock discovery + client, verify subtask registered in tracker."""
    mock_discovery = AsyncMock(spec=DiscoveryClient)
    mock_discovery.discover_by_skill.return_value = [
        {"name": "writer-agent", "url": "http://writer:8462"}
    ]

    mock_client = AsyncMock(spec=A2AClient)
    mock_client.send_task.return_value = {
        "task_id": "delegated-task-1",
        "status": "working",
        "response": {},
    }

    tracker = SubtaskTracker()

    subtask_id = await delegate_to_peer(
        client=mock_client,
        discovery=mock_discovery,
        subtask_tracker=tracker,
        parent_task_id="parent-del",
        skill_needed="writing",
        message_text="write a summary",
        from_agent="coordinator",
        callback_url="http://coordinator:8462/callbacks",
    )

    assert subtask_id == "delegated-task-1"
    assert tracker.get_parent_for_subtask("delegated-task-1") == "parent-del"

    mock_discovery.discover_by_skill.assert_awaited_once_with("writing")
    mock_client.send_task.assert_awaited_once_with(
        peer_url="http://writer:8462",
        message_text="write a summary",
        from_agent="coordinator",
        callback_url="http://coordinator:8462/callbacks",
    )

    results = tracker.get_subtask_results("parent-del")
    assert len(results) == 1
    assert results[0].peer_name == "writer-agent"
    assert results[0].status == "pending"


@pytest.mark.asyncio
async def test_delegate_to_peer_no_peer():
    """No peer found -> returns None."""
    mock_discovery = AsyncMock(spec=DiscoveryClient)
    mock_discovery.discover_by_skill.return_value = []

    mock_client = AsyncMock(spec=A2AClient)
    tracker = SubtaskTracker()

    result = await delegate_to_peer(
        client=mock_client,
        discovery=mock_discovery,
        subtask_tracker=tracker,
        parent_task_id="parent-no",
        skill_needed="nonexistent-skill",
        message_text="do something",
        from_agent="coordinator",
        callback_url="http://coordinator:8462/callbacks",
    )

    assert result is None
    mock_client.send_task.assert_not_awaited()
    assert tracker.get_subtask_results("parent-no") == []
