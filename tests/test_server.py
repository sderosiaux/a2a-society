from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hive.models import AgentConfig, SkillDef
from hive.server import create_app


@pytest.fixture()
def config() -> AgentConfig:
    return AgentConfig(
        name="test-agent",
        role="Tester",
        description="A test agent",
        skills=[
            SkillDef(id="echo", name="Echo skill"),
            SkillDef(id="ping", name="Ping skill"),
        ],
    )


@pytest.fixture()
def app(config: AgentConfig):
    return create_app(config)


@pytest_asyncio.fixture()
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_agent_card(client: AsyncClient):
    resp = await client.get("/.well-known/agent.json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "test-agent"
    assert len(data["skills"]) == 2
    assert data["skills"][0]["id"] == "echo"
    assert data["skills"][1]["id"] == "ping"


@pytest.mark.asyncio
async def test_agent_card_new_path(client: AsyncClient):
    resp = await client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    assert resp.json()["name"] == "test-agent"


@pytest.mark.asyncio
async def test_message_send(client: AsyncClient):
    message_id = str(uuid.uuid4())
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {
            "message": {
                "messageId": message_id,
                "role": "user",
                "parts": [{"kind": "text", "text": "hello world"}],
            }
        },
    }
    resp = await client.post("/", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "result" in data
    result = data["result"]
    # The result should contain a status with state=completed
    assert result["status"]["state"] == "completed"
    # The response message should contain our echo text
    message = result["status"]["message"]
    parts = message["parts"]
    assert any("[test-agent] received: hello world" in p["text"] for p in parts)


@pytest.mark.asyncio
async def test_status_endpoint(client: AsyncClient):
    resp = await client.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "test-agent"
    assert data["role"] == "Tester"
    assert data["status"] == "active"
    assert "budget_remaining" in data
    assert "queue_depth" in data
