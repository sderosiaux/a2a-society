"""Tests for C4-C7: queue wiring, delegation detection, callback re-queue, Condition."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hive.executor import parse_delegation
from hive.models import AgentConfig, SkillDef
from hive.queue import TaskPriority, TaskQueue, TaskQueueFullError
from hive.server import create_app
from hive.subtask_tracker import SubtaskTracker

# ---------------------------------------------------------------------------
# C5: parse_delegation tests
# ---------------------------------------------------------------------------


class TestParseDelegation:
    def test_valid_delegation_block(self):
        text = 'I need help. {"delegate": {"skill": "seo-audit", "message": "Run an SEO audit on example.com"}}'
        result = parse_delegation(text)
        assert result is not None
        assert result["skill"] == "seo-audit"
        assert result["message"] == "Run an SEO audit on example.com"

    def test_delegation_block_alone(self):
        text = '{"delegate": {"skill": "writing", "message": "Write a blog post"}}'
        result = parse_delegation(text)
        assert result is not None
        assert result["skill"] == "writing"

    def test_no_delegation_block(self):
        text = "Here is my analysis. No delegation needed."
        result = parse_delegation(text)
        assert result is None

    def test_empty_skill_returns_none(self):
        text = '{"delegate": {"skill": "", "message": "something"}}'
        result = parse_delegation(text)
        assert result is None

    def test_malformed_json_returns_none(self):
        text = '{"delegate": {"skill": "seo" broken json'
        result = parse_delegation(text)
        assert result is None

    def test_delegation_surrounded_by_prose(self):
        text = (
            "After reviewing the request, I believe we need a specialist.\n"
            '{"delegate": {"skill": "data-analysis", "message": "Analyze Q1 revenue data"}}\n'
            "I'll synthesize the results when they come back."
        )
        result = parse_delegation(text)
        assert result is not None
        assert result["skill"] == "data-analysis"
        assert result["message"] == "Analyze Q1 revenue data"

    def test_delegation_with_extra_whitespace(self):
        text = '{"delegate" :  {"skill" : "seo", "message" : "audit site"}}'
        result = parse_delegation(text)
        assert result is not None
        assert result["skill"] == "seo"


# ---------------------------------------------------------------------------
# C7: Queue with asyncio.Condition tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_condition_concurrent_enqueue_dequeue():
    """Multiple producers and a consumer work correctly under Condition."""
    q = TaskQueue(max_backlog=20)
    results = []

    async def producer(task_ids: list[str]):
        for tid in task_ids:
            await q.enqueue(tid, f"msg-{tid}", {})

    async def consumer(count: int):
        for _ in range(count):
            task = await q.dequeue()
            results.append(task.task_id)

    # Start consumer first (it will block), then producers
    consumer_task = asyncio.create_task(consumer(6))
    await asyncio.sleep(0.01)  # let consumer start waiting

    await asyncio.gather(
        producer(["a1", "a2", "a3"]),
        producer(["b1", "b2", "b3"]),
    )

    await asyncio.wait_for(consumer_task, timeout=2.0)
    assert len(results) == 6
    assert set(results) == {"a1", "a2", "a3", "b1", "b2", "b3"}


@pytest.mark.asyncio
async def test_condition_priority_preserved_under_concurrency():
    """Enqueue multiple priorities concurrently, dequeue respects priority."""
    q = TaskQueue(max_backlog=10)

    await q.enqueue("t-broadcast", "low", {"priority": "broadcast"})
    await q.enqueue("t-normal", "mid", {})
    await q.enqueue("t-escalation", "high", {"priority": "escalation"})

    first = await q.dequeue()
    second = await q.dequeue()
    third = await q.dequeue()

    assert first.task_id == "t-escalation"
    assert second.task_id == "t-normal"
    assert third.task_id == "t-broadcast"


@pytest.mark.asyncio
async def test_condition_notify_wakes_blocked_dequeue():
    """Dequeue blocks until enqueue notifies via Condition."""
    q = TaskQueue(max_backlog=5)

    async def slow_enqueue():
        await asyncio.sleep(0.05)
        await q.enqueue("delayed-1", "msg", {})

    asyncio.create_task(slow_enqueue())
    task = await asyncio.wait_for(q.dequeue(), timeout=1.0)
    assert task.task_id == "delayed-1"


@pytest.mark.asyncio
async def test_condition_queue_full_error():
    """TaskQueueFullError raised under concurrent enqueue."""
    q = TaskQueue(max_backlog=2)
    await q.enqueue("t1", "a", {})
    await q.enqueue("t2", "b", {})

    with pytest.raises(TaskQueueFullError):
        await q.enqueue("t3", "c", {})


# ---------------------------------------------------------------------------
# C6: Callback re-queue tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def requeue_config() -> AgentConfig:
    return AgentConfig(
        name="coordinator-agent",
        role="Coordinator",
        description="Agent that re-queues on callback",
        skills=[SkillDef(id="coordinate", name="Coordinate")],
    )


@pytest.fixture()
def requeue_app(requeue_config: AgentConfig):
    return create_app(requeue_config, use_echo=True)


@pytest_asyncio.fixture()
async def requeue_client(requeue_app):
    transport = ASGITransport(app=requeue_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_callback_requeue_enqueues_synthesis(requeue_client: AsyncClient, requeue_app):
    """When all subtasks complete, a synthesis task is enqueued and tracker is cleaned."""
    tracker: SubtaskTracker = requeue_app.state.subtask_tracker
    tq: TaskQueue = requeue_app.state.task_queue

    tracker.register_subtask("parent-rq", "sub-rq-1", "worker-1")

    initial_size = tq.size()

    resp = await requeue_client.post(
        "/callbacks",
        json={
            "task_id": "sub-rq-1",
            "status": "completed",
            "result": {"text": "analysis done"},
            "from_agent": "worker-1",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["parent_ready"] is True

    # Synthesis task was enqueued
    assert tq.size() == initial_size + 1

    # Dequeue and verify it's the synthesis task
    task = await tq.dequeue()
    assert task.task_id == "synthesis-parent-rq"
    assert "delegated subtasks have completed" in task.message_text.lower()
    assert task.priority == TaskPriority.ESCALATION  # "escalation" priority
    assert task.metadata.get("parent_task_id") == "parent-rq"

    # Tracker was cleaned up
    assert tracker.get_subtask_results("parent-rq") == []
    assert tracker.get_parent_for_subtask("sub-rq-1") is None


@pytest.mark.asyncio
async def test_callback_partial_no_requeue(requeue_client: AsyncClient, requeue_app):
    """When only some subtasks complete, no synthesis is enqueued."""
    tracker: SubtaskTracker = requeue_app.state.subtask_tracker
    tq: TaskQueue = requeue_app.state.task_queue

    tracker.register_subtask("parent-partial", "sub-p1", "worker-1")
    tracker.register_subtask("parent-partial", "sub-p2", "worker-2")

    initial_size = tq.size()

    resp = await requeue_client.post(
        "/callbacks",
        json={
            "task_id": "sub-p1",
            "status": "completed",
            "result": {"text": "done"},
            "from_agent": "worker-1",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["parent_ready"] is False

    # No synthesis enqueued
    assert tq.size() == initial_size

    # Tracker still has data
    assert len(tracker.get_subtask_results("parent-partial")) == 2


@pytest.mark.asyncio
async def test_callback_requeue_includes_failed_results(requeue_client: AsyncClient, requeue_app):
    """Synthesis prompt includes both completed and failed subtask results."""
    tracker: SubtaskTracker = requeue_app.state.subtask_tracker
    tq: TaskQueue = requeue_app.state.task_queue

    tracker.register_subtask("parent-mixed", "sub-m1", "worker-1")
    tracker.register_subtask("parent-mixed", "sub-m2", "worker-2")

    # First subtask fails
    await requeue_client.post(
        "/callbacks",
        json={
            "task_id": "sub-m1",
            "status": "failed",
            "result": {"reason": "timeout"},
            "from_agent": "worker-1",
        },
    )

    # Second subtask completes -> triggers synthesis
    resp = await requeue_client.post(
        "/callbacks",
        json={
            "task_id": "sub-m2",
            "status": "completed",
            "result": {"text": "success"},
            "from_agent": "worker-2",
        },
    )
    assert resp.json()["parent_ready"] is True

    task = await tq.dequeue()
    assert "worker-1" in task.message_text
    assert "worker-2" in task.message_text
    assert "failed" in task.message_text
    assert "completed" in task.message_text


# ---------------------------------------------------------------------------
# C4: Queue wiring - status endpoint shows queue depth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_shows_queue_depth(requeue_client: AsyncClient, requeue_app):
    """Status endpoint reports live queue depth."""
    tq: TaskQueue = requeue_app.state.task_queue

    resp = await requeue_client.get("/status")
    assert resp.json()["queue_depth"] == 0

    await tq.enqueue("t1", "test", {})
    await tq.enqueue("t2", "test", {})

    resp = await requeue_client.get("/status")
    assert resp.json()["queue_depth"] == 2


# ---------------------------------------------------------------------------
# C5: Delegation instructions in system prompt
# ---------------------------------------------------------------------------


def test_system_prompt_contains_delegation_instructions():
    from hive.prompt_builder import build_system_prompt

    config = AgentConfig(name="test", role="Engineer")
    prompt = build_system_prompt(config)
    assert '{"delegate":' in prompt
    assert '"skill"' in prompt
    assert '"message"' in prompt
    assert "specialist" in prompt.lower()
