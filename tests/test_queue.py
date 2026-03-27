from __future__ import annotations

import asyncio

import pytest

from hive.queue import TaskPriority, TaskQueue, TaskQueueFullError


@pytest.mark.asyncio
async def test_dequeue_returns_highest_priority_first():
    q = TaskQueue(max_backlog=10)
    await q.enqueue("t-broadcast", "low", {"priority": "broadcast"})
    await q.enqueue("t-escalation", "high", {"priority": "escalation"})
    await q.enqueue("t-consult", "mid", {})

    first = await q.dequeue()
    second = await q.dequeue()
    third = await q.dequeue()

    assert first.task_id == "t-escalation"
    assert second.task_id == "t-consult"
    assert third.task_id == "t-broadcast"


@pytest.mark.asyncio
async def test_from_superior_priority():
    q = TaskQueue(max_backlog=10, agent_superior="boss-agent")
    await q.enqueue("t-normal", "normal", {"from_agent": "peer"})
    await q.enqueue("t-boss", "from boss", {"from_agent": "boss-agent"})

    first = await q.dequeue()
    assert first.task_id == "t-boss"
    assert first.priority == TaskPriority.FROM_SUPERIOR


@pytest.mark.asyncio
async def test_escalation_beats_everything():
    q = TaskQueue(max_backlog=10, agent_superior="boss-agent")
    await q.enqueue("t-boss", "boss", {"from_agent": "boss-agent"})
    await q.enqueue("t-esc", "escalation", {"priority": "escalation"})
    await q.enqueue("t-broadcast", "broadcast", {"priority": "broadcast"})

    first = await q.dequeue()
    assert first.task_id == "t-esc"
    assert first.priority == TaskPriority.ESCALATION


@pytest.mark.asyncio
async def test_same_priority_fifo():
    q = TaskQueue(max_backlog=10)
    await q.enqueue("t-1", "first", {})
    await q.enqueue("t-2", "second", {})
    await q.enqueue("t-3", "third", {})

    first = await q.dequeue()
    second = await q.dequeue()
    third = await q.dequeue()

    assert first.task_id == "t-1"
    assert second.task_id == "t-2"
    assert third.task_id == "t-3"


@pytest.mark.asyncio
async def test_queue_full_raises():
    q = TaskQueue(max_backlog=2)
    await q.enqueue("t-1", "a", {})
    await q.enqueue("t-2", "b", {})

    assert q.is_full()
    with pytest.raises(TaskQueueFullError):
        await q.enqueue("t-3", "c", {})


@pytest.mark.asyncio
async def test_dequeue_blocks_until_available():
    q = TaskQueue(max_backlog=10)

    async def delayed_enqueue():
        await asyncio.sleep(0.05)
        await q.enqueue("t-delayed", "hello", {})

    asyncio.create_task(delayed_enqueue())

    task = await asyncio.wait_for(q.dequeue(), timeout=1.0)
    assert task.task_id == "t-delayed"


@pytest.mark.asyncio
async def test_dequeue_timeout_on_empty():
    q = TaskQueue(max_backlog=10)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(q.dequeue(), timeout=0.05)


@pytest.mark.asyncio
async def test_classify_priority_escalation():
    q = TaskQueue(max_backlog=10, agent_superior="boss")
    assert q._classify_priority({"priority": "escalation"}) == TaskPriority.ESCALATION


@pytest.mark.asyncio
async def test_classify_priority_from_superior():
    q = TaskQueue(max_backlog=10, agent_superior="boss")
    assert q._classify_priority({"from_agent": "boss"}) == TaskPriority.FROM_SUPERIOR


@pytest.mark.asyncio
async def test_classify_priority_broadcast():
    q = TaskQueue(max_backlog=10)
    assert q._classify_priority({"priority": "broadcast"}) == TaskPriority.BROADCAST


@pytest.mark.asyncio
async def test_classify_priority_default():
    q = TaskQueue(max_backlog=10)
    assert q._classify_priority({}) == TaskPriority.CONSULTATION
    assert q._classify_priority({"from_agent": "random"}) == TaskPriority.CONSULTATION


@pytest.mark.asyncio
async def test_size_and_is_full():
    q = TaskQueue(max_backlog=3)
    assert q.size() == 0
    assert not q.is_full()

    await q.enqueue("t-1", "a", {})
    assert q.size() == 1

    await q.enqueue("t-2", "b", {})
    await q.enqueue("t-3", "c", {})
    assert q.size() == 3
    assert q.is_full()

    await q.dequeue()
    assert q.size() == 2
    assert not q.is_full()
