from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hive.registry.server import create_registry_app
from hive.registry.store import RegistryStore
from tests.conftest import make_card

# -- HTTP integration tests --------------------------------------------------


@pytest.fixture()
def app():
    return create_registry_app(heartbeat_interval=60)


@pytest_asyncio.fixture()
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_register_and_list(client: AsyncClient):
    card = make_card("agent-a")
    resp = await client.post("/agents/register", json=card)
    assert resp.status_code == 200

    resp = await client.get("/agents")
    assert resp.status_code == 200
    agents = resp.json()
    assert len(agents) == 1
    assert agents[0]["name"] == "agent-a"


@pytest.mark.asyncio
async def test_by_skill_filters(client: AsyncClient):
    card_a = make_card("agent-a", skills=[{"id": "seo", "name": "SEO"}])
    card_b = make_card("agent-b", skills=[{"id": "dev", "name": "Dev"}])
    await client.post("/agents/register", json=card_a)
    await client.post("/agents/register", json=card_b)

    resp = await client.get("/agents/by-skill/seo")
    agents = resp.json()
    assert len(agents) == 1
    assert agents[0]["name"] == "agent-a"


@pytest.mark.asyncio
async def test_get_by_name_and_404(client: AsyncClient):
    card = make_card("agent-a")
    await client.post("/agents/register", json=card)

    resp = await client.get("/agents/agent-a")
    assert resp.status_code == 200
    assert resp.json()["name"] == "agent-a"

    resp = await client.get("/agents/unknown")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_by_role_case_insensitive(client: AsyncClient):
    card_a = make_card("agent-a", role="SEO Specialist")
    card_b = make_card("agent-b", role="Engineer")
    await client.post("/agents/register", json=card_a)
    await client.post("/agents/register", json=card_b)

    resp = await client.get("/agents/by-role/seo specialist")
    agents = resp.json()
    assert len(agents) == 1
    assert agents[0]["name"] == "agent-a"

    resp = await client.get("/agents/by-role/SEO SPECIALIST")
    assert len(resp.json()) == 1


@pytest.mark.asyncio
async def test_heartbeat_expires_agent(app, client: AsyncClient):
    card = make_card("agent-a")
    await client.post("/agents/register", json=card)

    store: RegistryStore = app.state.store
    # Manually expire the agent by backdating last_seen.
    entry = store._agents["agent-a"]
    entry.last_seen = datetime.now(UTC) - timedelta(seconds=300)

    marked = store.check_heartbeats()
    assert "agent-a" in marked

    resp = await client.get("/agents")
    assert len(resp.json()) == 0


@pytest.mark.asyncio
async def test_re_register_refreshes(app, client: AsyncClient):
    card = make_card("agent-a")
    await client.post("/agents/register", json=card)

    store: RegistryStore = app.state.store
    entry = store._agents["agent-a"]
    entry.last_seen = datetime.now(UTC) - timedelta(seconds=300)

    # Re-register (heartbeat) should refresh last_seen and set active.
    await client.post("/agents/register", json=card)
    assert store._agents["agent-a"].status == "active"

    resp = await client.get("/agents")
    assert len(resp.json()) == 1


@pytest.mark.asyncio
async def test_deregister(app, client: AsyncClient):
    card = make_card("agent-a")
    await client.post("/agents/register", json=card)

    store: RegistryStore = app.state.store
    assert store.deregister("agent-a") is True
    assert store.deregister("agent-a") is False

    resp = await client.get("/agents")
    assert len(resp.json()) == 0


# -- Auth tests for registry --------------------------------------------------

REG_TOKEN = "registry-secret"


@pytest.fixture()
def auth_app():
    return create_registry_app(heartbeat_interval=60, auth_token=REG_TOKEN)


@pytest_asyncio.fixture()
async def auth_client(auth_app):
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_registry_auth_register_rejected(auth_client: AsyncClient):
    """POST /agents/register without token returns 401."""
    card = make_card("agent-a")
    resp = await auth_client.post("/agents/register", json=card)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_registry_auth_register_wrong_token(auth_client: AsyncClient):
    """POST /agents/register with wrong token returns 403."""
    card = make_card("agent-a")
    resp = await auth_client.post("/agents/register", json=card, headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_registry_auth_register_accepted(auth_client: AsyncClient):
    """POST /agents/register with correct token returns 200."""
    card = make_card("agent-a")
    resp = await auth_client.post(
        "/agents/register",
        json=card,
        headers={"Authorization": f"Bearer {REG_TOKEN}"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_registry_auth_get_endpoints_public(auth_client: AsyncClient):
    """GET endpoints remain public even with auth enabled."""
    resp = await auth_client.get("/agents")
    assert resp.status_code == 200

    resp = await auth_client.get("/agents/by-skill/seo")
    assert resp.status_code == 200

    resp = await auth_client.get("/agents/by-role/Engineer")
    assert resp.status_code == 200
