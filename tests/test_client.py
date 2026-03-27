from __future__ import annotations

import httpx
import pytest
import respx
from httpx import Response

from hive.client import A2AClient, A2AClientError, A2ATaskRejectedError

PEER_URL = "http://peer-agent:8462"


@pytest.fixture()
def client():
    return A2AClient()


@respx.mock
@pytest.mark.asyncio
async def test_send_task_payload(client: A2AClient):
    """Verify the JSON-RPC payload sent to the peer is correct."""
    captured = {}

    def capture(request):
        captured["body"] = request.content
        captured["json"] = __import__("json").loads(request.content)
        return Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "id": "task-123",
                    "status": {"state": "completed"},
                },
            },
        )

    respx.post(PEER_URL).mock(side_effect=capture)

    await client.send_task(
        peer_url=PEER_URL,
        message_text="summarize this",
        from_agent="boss-agent",
        priority="high",
        callback_url="http://boss:8462/callback",
    )

    body = captured["json"]
    assert body["jsonrpc"] == "2.0"
    assert body["method"] == "message/send"
    assert "id" in body

    msg = body["params"]["message"]
    assert msg["role"] == "user"
    assert msg["parts"] == [{"kind": "text", "text": "summarize this"}]
    assert msg["metadata"]["from_agent"] == "boss-agent"
    assert msg["metadata"]["priority"] == "high"
    assert msg["metadata"]["callback_url"] == "http://boss:8462/callback"

    await client.close()


@respx.mock
@pytest.mark.asyncio
async def test_send_task_success(client: A2AClient):
    """Successful response returns dict with task_id and status."""
    respx.post(PEER_URL).mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "id": "task-456",
                    "status": {"state": "completed"},
                },
            },
        )
    )

    result = await client.send_task(
        peer_url=PEER_URL,
        message_text="do work",
        from_agent="agent-a",
    )

    assert result["task_id"] == "task-456"
    assert result["status"] == "completed"
    assert "response" in result

    await client.close()


@respx.mock
@pytest.mark.asyncio
async def test_jsonrpc_error_raises(client: A2AClient):
    """JSON-RPC error response raises A2AClientError."""
    respx.post(PEER_URL).mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32600, "message": "Invalid request"},
            },
        )
    )

    with pytest.raises(A2AClientError, match="Invalid request"):
        await client.send_task(
            peer_url=PEER_URL,
            message_text="bad",
            from_agent="agent-a",
        )

    await client.close()


@respx.mock
@pytest.mark.asyncio
async def test_connection_refused_raises(client: A2AClient):
    """Connection error raises A2AClientError."""
    respx.post(PEER_URL).mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(A2AClientError, match="Connection error"):
        await client.send_task(
            peer_url=PEER_URL,
            message_text="hello",
            from_agent="agent-a",
        )

    await client.close()


@respx.mock
@pytest.mark.asyncio
async def test_rejected_task_raises(client: A2AClient):
    """Rejected task raises A2ATaskRejectedError with reason."""
    respx.post(PEER_URL).mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "id": "task-789",
                    "status": {
                        "state": "rejected",
                        "timestamp": "2026-03-28T10:00:00Z",
                        "message": {
                            "role": "agent",
                            "parts": [{"kind": "text", "text": "at_capacity"}],
                        },
                    },
                },
            },
        )
    )

    with pytest.raises(A2ATaskRejectedError, match="at_capacity") as exc_info:
        await client.send_task(
            peer_url=PEER_URL,
            message_text="do work",
            from_agent="agent-a",
        )

    assert exc_info.value.reason == "at_capacity"
    assert exc_info.value.resume_at == "2026-03-28T10:00:00Z"

    await client.close()
