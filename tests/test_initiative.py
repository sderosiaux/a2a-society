from __future__ import annotations

import asyncio
import json

import pytest

from hive.budget import BudgetManager
from hive.initiative import InitiativeLoop
from hive.models import AgentConfig, BudgetConfig


def _make_config(**overrides) -> AgentConfig:
    defaults = {
        "name": "test-agent",
        "role": "tester",
        "objectives": ["Ship features", "Improve SEO"],
        "initiative_interval_minutes": 1,
    }
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _make_budget(daily: float = 10.0, weekly: float = 50.0) -> BudgetManager:
    return BudgetManager(BudgetConfig(daily_max_usd=daily, weekly_max_usd=weekly))


def _mock_claude(response_json: str):
    """Return an async callable that mimics invoke_claude signature."""

    async def _fn(prompt: str, system_prompt: str):
        return response_json, 0.05, None

    return _fn


class FakeQueue:
    """Minimal mock for TaskQueue."""

    def __init__(self):
        self.tasks: list[dict] = []

    async def enqueue(self, task_id: str, message_text: str, metadata: dict, context_id: str = ""):
        self.tasks.append({"task_id": task_id, "message_text": message_text, "metadata": metadata})


class FakeDiscovery:
    """Minimal mock for DiscoveryClient."""

    def __init__(self, peers: list[dict] | None = None):
        self._peers = peers or []

    async def discover_by_skill(self, skill_id: str) -> list[dict]:
        return self._peers


class FakeA2AClient:
    """Minimal mock for A2AClient."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send_task(self, peer_url: str, message_text: str, from_agent: str, **kwargs):
        self.sent.append({"peer_url": peer_url, "message_text": message_text, "from_agent": from_agent})
        return {"task_id": "delegated-001", "status": "submitted"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_skipped_when_budget_warning():
    budget = _make_budget(daily=10.0)
    budget.record_cost(8.0)  # 80% -> warning
    assert budget.status.value == "warning"

    loop = InitiativeLoop(
        config=_make_config(),
        claude_fn=_mock_claude('{"decision": "nothing"}'),
        budget=budget,
    )
    result = await loop.tick()
    assert result is None


@pytest.mark.asyncio
async def test_tick_skipped_when_budget_vacation():
    budget = _make_budget(daily=10.0)
    budget.record_cost(10.0)  # 100% -> vacation
    assert budget.status.value == "vacation"

    loop = InitiativeLoop(
        config=_make_config(),
        claude_fn=_mock_claude('{"decision": "nothing"}'),
        budget=budget,
    )
    result = await loop.tick()
    assert result is None


@pytest.mark.asyncio
async def test_tick_nothing_decision():
    loop = InitiativeLoop(
        config=_make_config(),
        claude_fn=_mock_claude('{"decision": "nothing"}'),
    )
    result = await loop.tick()
    assert result == {"decision": "nothing"}


@pytest.mark.asyncio
async def test_tick_self_task_enqueues():
    queue = FakeQueue()
    response = json.dumps({"decision": "self_task", "description": "Write a blog post about SEO"})

    loop = InitiativeLoop(
        config=_make_config(),
        claude_fn=_mock_claude(response),
        queue=queue,
    )
    result = await loop.tick()
    assert result["decision"] == "self_task"
    assert len(queue.tasks) == 1
    assert queue.tasks[0]["message_text"] == "Write a blog post about SEO"


@pytest.mark.asyncio
async def test_tick_delegate_sends_to_peer():
    client = FakeA2AClient()
    discovery = FakeDiscovery(peers=[{"name": "seo-agent", "url": "http://localhost:9001"}])
    response = json.dumps({"decision": "delegate", "skill_needed": "seo", "message": "Audit the homepage"})

    loop = InitiativeLoop(
        config=_make_config(),
        claude_fn=_mock_claude(response),
        client=client,
        discovery=discovery,
    )
    result = await loop.tick()
    assert result["decision"] == "delegate"
    assert len(client.sent) == 1
    assert client.sent[0]["peer_url"] == "http://localhost:9001"
    assert client.sent[0]["message_text"] == "Audit the homepage"


@pytest.mark.asyncio
async def test_tick_invalid_json_returns_none():
    loop = InitiativeLoop(
        config=_make_config(),
        claude_fn=_mock_claude("Sure! I think we should do nothing."),
    )
    result = await loop.tick()
    assert result is None


@pytest.mark.asyncio
async def test_tick_extracts_json_from_text():
    """Claude sometimes wraps JSON in prose — verify extraction works."""
    raw = 'Here is my decision:\n{"decision": "nothing"}\nHope that helps!'
    loop = InitiativeLoop(
        config=_make_config(),
        claude_fn=_mock_claude(raw),
    )
    result = await loop.tick()
    assert result == {"decision": "nothing"}


@pytest.mark.asyncio
async def test_start_stop_lifecycle():
    loop = InitiativeLoop(
        config=_make_config(initiative_interval_minutes=60),
        claude_fn=_mock_claude('{"decision": "nothing"}'),
    )
    await loop.start()
    assert loop._running is True
    assert loop._task is not None
    await asyncio.sleep(0.05)
    await loop.stop()
    assert loop._running is False


@pytest.mark.asyncio
async def test_tick_records_cost_on_budget():
    budget = _make_budget(daily=10.0)
    assert budget.spent_today == 0.0

    loop = InitiativeLoop(
        config=_make_config(),
        claude_fn=_mock_claude('{"decision": "nothing"}'),
        budget=budget,
    )
    await loop.tick()
    assert budget.spent_today == 0.05  # cost from mock


@pytest.mark.asyncio
async def test_tick_claude_error_returns_none():
    """If Claude call raises, tick returns None gracefully."""

    async def _failing_claude(prompt, system_prompt):
        raise RuntimeError("API down")

    loop = InitiativeLoop(
        config=_make_config(),
        claude_fn=_failing_claude,
    )
    result = await loop.tick()
    assert result is None
