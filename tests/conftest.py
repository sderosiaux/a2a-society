"""Shared test helpers and fixtures."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from hive.models import AgentConfig, BudgetConfig

# ---------------------------------------------------------------------------
# Agent card builder (for registry / discovery tests)
# ---------------------------------------------------------------------------


def make_card(
    name: str,
    role: str = "Engineer",
    skills: list[dict] | None = None,
    url: str | None = None,
) -> dict:
    """Build a registry agent card dict for testing."""
    return {
        "name": name,
        "description": f"{name} agent",
        "url": url or f"http://{name}:8462",
        "skills": skills or [],
        "hive": {"role": role, "status": "active"},
    }


# ---------------------------------------------------------------------------
# AgentConfig builder
# ---------------------------------------------------------------------------


def make_config(**overrides) -> AgentConfig:
    """Build an AgentConfig with sensible test defaults."""
    defaults: dict = {
        "name": "test-agent",
        "role": "Tester",
        "budget": BudgetConfig(daily_max_usd=5.0, per_task_max_usd=2.0),
    }
    defaults.update(overrides)
    return AgentConfig(**defaults)


# ---------------------------------------------------------------------------
# Fake collaborators (for initiative / reporting tests)
# ---------------------------------------------------------------------------


class FakeQueue:
    """Minimal mock for TaskQueue."""

    def __init__(self) -> None:
        self.tasks: list[dict] = []

    async def enqueue(
        self,
        task_id: str,
        message_text: str,
        metadata: dict,
        context_id: str = "",
    ) -> None:
        self.tasks.append({"task_id": task_id, "message_text": message_text, "metadata": metadata})


class FakeDiscovery:
    """Minimal mock for DiscoveryClient."""

    def __init__(
        self,
        peers: list[dict] | None = None,
        by_role: list[dict] | None = None,
        all_agents: list[dict] | None = None,
    ) -> None:
        self._peers = peers or []
        self._by_role = by_role or []
        self._all = all_agents or []

    async def discover_by_skill(self, skill_id: str) -> list[dict]:
        return self._peers

    async def discover_by_role(self, role: str) -> list[dict]:
        return self._by_role

    async def discover_all(self) -> list[dict]:
        return self._all


class FakeA2AClient:
    """Minimal mock for A2AClient."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_task(self, peer_url: str, message_text: str, from_agent: str, **kwargs) -> dict:
        self.sent.append(
            {
                "peer_url": peer_url,
                "message_text": message_text,
                "from_agent": from_agent,
                **kwargs,
            }
        )
        return {"task_id": "delegated-001", "status": "submitted"}


class FakeOrgMemory:
    """Minimal stub for OrgMemory."""

    def __init__(self, events: list[dict] | None = None) -> None:
        self._events = events or []
        self.written: list[dict] = []

    def pull(self) -> None:
        pass

    def list_events(self, agent: str | None = None) -> list[dict]:
        if agent:
            return [e for e in self._events if e.get("agent") == agent]
        return list(self._events)

    def write_artifact(self, domain: str, filename: str, content: str) -> dict:
        ref = {
            "repo": "/tmp/org",
            "path": f"artifacts/{domain}/{filename}",
            "commit": "abc123",
            "size_lines": content.count("\n") + 1,
        }
        self.written.append({"domain": domain, "filename": filename, "content": content, "ref": ref})
        return ref


# ---------------------------------------------------------------------------
# Mock context / event queue helpers (for executor tests)
# ---------------------------------------------------------------------------


def mock_context(
    task_id: str = "task-1",
    context_id: str = "ctx-1",
    user_input: str = "Do work",
    metadata: dict | None = None,
):
    """Build a mocked RequestContext."""
    from a2a.server.agent_execution import RequestContext

    context = MagicMock(spec=RequestContext)
    context.task_id = task_id
    context.context_id = context_id
    context.get_user_input.return_value = user_input
    context.current_task = None

    msg = MagicMock()
    msg.metadata = metadata
    context.message = msg

    return context


def mock_event_queue():
    """Build a mocked EventQueue."""
    from a2a.server.events import EventQueue

    eq = MagicMock(spec=EventQueue)
    eq.enqueue_event = AsyncMock()
    return eq
