"""Integration test: two agents talking via A2A with a shared registry.

Verifies the full round-trip: two agent nodes + registry, one delegates
to the other. Both agents use echo mode (no real Claude calls).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import pytest_asyncio
import yaml
from httpx import ASGITransport, AsyncClient

from hive.models import AgentConfig
from hive.registry.server import create_registry_app
from hive.server import create_app, _build_registration_card
from hive.subtask_tracker import SubtaskTracker

CONFIGS_DIR = Path(__file__).parent / "configs"


def _load_config(filename: str) -> AgentConfig:
    with open(CONFIGS_DIR / filename) as f:
        return AgentConfig(**yaml.safe_load(f))


# -- fixtures ----------------------------------------------------------------


@pytest.fixture()
def config_a() -> AgentConfig:
    return _load_config("agent-a.yaml")


@pytest.fixture()
def config_b() -> AgentConfig:
    return _load_config("agent-b.yaml")


@pytest.fixture()
def registry_app():
    return create_registry_app(heartbeat_interval=300)


@pytest.fixture()
def app_a(config_a: AgentConfig):
    return create_app(config_a, use_echo=True)


@pytest.fixture()
def app_b(config_b: AgentConfig):
    return create_app(config_b, use_echo=True)


@pytest_asyncio.fixture()
async def registry_client(registry_app):
    transport = ASGITransport(app=registry_app)
    async with AsyncClient(transport=transport, base_url="http://registry") as c:
        yield c


@pytest_asyncio.fixture()
async def client_a(app_a):
    transport = ASGITransport(app=app_a)
    async with AsyncClient(transport=transport, base_url="http://agent-a") as c:
        yield c


@pytest_asyncio.fixture()
async def client_b(app_b):
    transport = ASGITransport(app=app_b)
    async with AsyncClient(transport=transport, base_url="http://agent-b") as c:
        yield c


# -- helpers -----------------------------------------------------------------


def _make_message_send(text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {
            "message": {
                "messageId": str(uuid.uuid4()),
                "role": "user",
                "parts": [{"kind": "text", "text": text}],
            }
        },
    }


# -- tests -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_with_both_agents(
    registry_client: AsyncClient,
    config_a: AgentConfig,
    config_b: AgentConfig,
):
    """Register both agents, verify listing and skill-based discovery."""
    card_a = _build_registration_card(config_a)
    card_b = _build_registration_card(config_b)

    resp = await registry_client.post("/agents/register", json=card_a)
    assert resp.status_code == 200
    resp = await registry_client.post("/agents/register", json=card_b)
    assert resp.status_code == 200

    # Both agents listed
    resp = await registry_client.get("/agents")
    agents = resp.json()
    assert len(agents) == 2
    names = {a["name"] for a in agents}
    assert names == {"vp-marketing", "seo-agent"}

    # Discover agent-b by skill "seo-audit"
    resp = await registry_client.get("/agents/by-skill/seo-audit")
    seo_agents = resp.json()
    assert len(seo_agents) == 1
    assert seo_agents[0]["name"] == "seo-agent"

    # Discover agent-a by skill "marketing-strategy"
    resp = await registry_client.get("/agents/by-skill/marketing-strategy")
    mkt_agents = resp.json()
    assert len(mkt_agents) == 1
    assert mkt_agents[0]["name"] == "vp-marketing"

    # Skill that no one has -> empty
    resp = await registry_client.get("/agents/by-skill/nonexistent")
    assert resp.json() == []


@pytest.mark.asyncio
async def test_agent_a_receives_and_echoes(client_a: AsyncClient):
    """Send a message/send to agent-a, verify echo response."""
    payload = _make_message_send("Plan Q3 marketing campaign")
    resp = await client_a.post("/", json=payload)
    assert resp.status_code == 200

    data = resp.json()
    assert "result" in data
    result = data["result"]
    assert result["status"]["state"] == "completed"

    parts = result["status"]["message"]["parts"]
    assert any(
        "[vp-marketing] received: Plan Q3 marketing campaign" in p["text"]
        for p in parts
    )


@pytest.mark.asyncio
async def test_agent_b_receives_and_echoes(client_b: AsyncClient):
    """Send a message/send to agent-b, verify echo response."""
    payload = _make_message_send("Run SEO audit on blog.example.com")
    resp = await client_b.post("/", json=payload)
    assert resp.status_code == 200

    data = resp.json()
    result = data["result"]
    assert result["status"]["state"] == "completed"

    parts = result["status"]["message"]["parts"]
    assert any(
        "[seo-agent] received: Run SEO audit on blog.example.com" in p["text"]
        for p in parts
    )


@pytest.mark.asyncio
async def test_cross_agent_discovery(
    registry_client: AsyncClient,
    config_a: AgentConfig,
    config_b: AgentConfig,
):
    """Agent-a can discover agent-b by skill via the registry."""
    card_a = _build_registration_card(config_a)
    card_b = _build_registration_card(config_b)
    await registry_client.post("/agents/register", json=card_a)
    await registry_client.post("/agents/register", json=card_b)

    # Simulate what agent-a's DiscoveryClient would do:
    # GET /agents/by-skill/seo-audit
    resp = await registry_client.get("/agents/by-skill/seo-audit")
    peers = resp.json()
    assert len(peers) == 1
    peer = peers[0]
    assert peer["name"] == "seo-agent"
    assert peer["url"] == "http://127.0.0.1:8463"

    # Also verify role-based discovery
    resp = await registry_client.get("/agents/by-role/SEO Specialist")
    role_peers = resp.json()
    assert len(role_peers) == 1
    assert role_peers[0]["name"] == "seo-agent"

    # VP Marketing discoverable by role
    resp = await registry_client.get("/agents/by-role/VP Marketing")
    vp_peers = resp.json()
    assert len(vp_peers) == 1
    assert vp_peers[0]["name"] == "vp-marketing"


@pytest.mark.asyncio
async def test_callback_simulates_completed_subtask(
    client_a: AsyncClient, app_a
):
    """Send a callback to agent-a simulating a subtask completed by agent-b."""
    tracker: SubtaskTracker = app_a.state.subtask_tracker

    # Simulate: agent-a delegated subtask "seo-task-1" to agent-b
    tracker.register_subtask("parent-task-1", "seo-task-1", "seo-agent")

    # Agent-b sends callback to agent-a
    resp = await client_a.post(
        "/callbacks",
        json={
            "task_id": "seo-task-1",
            "status": "completed",
            "result": {"text": "SEO audit complete: 42 issues found"},
            "from_agent": "seo-agent",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["parent_ready"] is True

    # After callback resolves parent, cleanup removes tracking data
    results = tracker.get_subtask_results("parent-task-1")
    assert results == []


@pytest.mark.asyncio
async def test_status_endpoints(
    client_a: AsyncClient, client_b: AsyncClient
):
    """Both agents' /status endpoints return correct metadata."""
    resp_a = await client_a.get("/status")
    assert resp_a.status_code == 200
    status_a = resp_a.json()
    assert status_a["name"] == "vp-marketing"
    assert status_a["role"] == "VP Marketing"
    assert status_a["status"] == "active"
    assert status_a["budget_remaining"] == 10.0

    resp_b = await client_b.get("/status")
    assert resp_b.status_code == 200
    status_b = resp_b.json()
    assert status_b["name"] == "seo-agent"
    assert status_b["role"] == "SEO Specialist"
    assert status_b["status"] == "active"
    assert status_b["budget_remaining"] == 5.0


@pytest.mark.asyncio
async def test_agent_cards_served(
    client_a: AsyncClient, client_b: AsyncClient
):
    """Both agents serve their A2A agent cards at well-known paths."""
    resp_a = await client_a.get("/.well-known/agent.json")
    assert resp_a.status_code == 200
    card_a = resp_a.json()
    assert card_a["name"] == "vp-marketing"
    assert card_a["skills"][0]["id"] == "marketing-strategy"

    resp_b = await client_b.get("/.well-known/agent.json")
    assert resp_b.status_code == 200
    card_b = resp_b.json()
    assert card_b["name"] == "seo-agent"
    assert card_b["skills"][0]["id"] == "seo-audit"


@pytest.mark.asyncio
async def test_full_flow_register_discover_send_callback(
    registry_client: AsyncClient,
    client_a: AsyncClient,
    client_b: AsyncClient,
    config_a: AgentConfig,
    config_b: AgentConfig,
    app_a,
):
    """End-to-end: register -> discover -> send tasks -> callback.

    Exercises the full round-trip across all three components
    (registry + two agents) in a single test flow.
    """
    # 1. Register both agents with the registry
    card_a = _build_registration_card(config_a)
    card_b = _build_registration_card(config_b)
    await registry_client.post("/agents/register", json=card_a)
    await registry_client.post("/agents/register", json=card_b)

    # 2. Agent-a discovers agent-b by skill
    resp = await registry_client.get("/agents/by-skill/seo-audit")
    peers = resp.json()
    assert len(peers) == 1
    peer_b = peers[0]
    assert peer_b["name"] == "seo-agent"

    # 3. Send task to agent-a (user request)
    payload_a = _make_message_send("Improve our search rankings")
    resp = await client_a.post("/", json=payload_a)
    assert resp.json()["result"]["status"]["state"] == "completed"

    # 4. Agent-a delegates to agent-b (simulated via direct call)
    payload_b = _make_message_send("Audit blog.example.com for SEO issues")
    resp = await client_b.post("/", json=payload_b)
    result_b = resp.json()["result"]
    assert result_b["status"]["state"] == "completed"

    # 5. Agent-b sends callback to agent-a
    tracker: SubtaskTracker = app_a.state.subtask_tracker
    subtask_id = result_b.get("id", "subtask-from-b")
    tracker.register_subtask("parent-improve-seo", subtask_id, "seo-agent")

    resp = await client_a.post(
        "/callbacks",
        json={
            "task_id": subtask_id,
            "status": "completed",
            "result": {"text": "Audit done: 15 issues fixed"},
            "from_agent": "seo-agent",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["parent_ready"] is True

    # 6. After callback resolves parent, cleanup removes tracking data
    results = tracker.get_subtask_results("parent-improve-seo")
    assert results == []
