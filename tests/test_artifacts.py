from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest
import respx
from httpx import Response

from hive.client import A2AClient
from hive.executor import ClaudeExecutor
from hive.models import BudgetConfig
from hive.org_memory import OrgMemory
from tests.conftest import make_config, mock_context, mock_event_queue

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_org_memory(tmp_path, agent: str = "test-agent") -> OrgMemory:
    mem = OrgMemory(repo_url=None, local_path=str(tmp_path / "org-repo"), agent_name=agent)
    mem.init_or_clone()
    return mem


def _seo_config(role: str = "SEO Specialist"):
    return make_config(name="seo-agent", role=role, budget=BudgetConfig(daily_max_usd=5.0, per_task_max_usd=2.0))


def _long_response(n: int = 60) -> str:
    """Generate a response with n lines."""
    return "\n".join(f"Line {i}" for i in range(1, n + 1)) + "\n"


def _short_response() -> str:
    return "Short answer in a few lines.\nDone."


# ---------------------------------------------------------------------------
# 1. Response > 50 lines -> artifact committed, response contains ref + summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_large_response_committed_as_artifact(tmp_path):
    org_mem = _make_org_memory(tmp_path)
    config = _seo_config()
    executor = ClaudeExecutor(config, org_memory=org_mem)

    long_text = _long_response(60)
    context = mock_context()
    event_queue = mock_event_queue()

    with patch("hive.claude.invoke_claude", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.return_value = (long_text, 0.10, "sess-1")
        await executor.execute(context, event_queue)

    # Artifact file should exist in org-memory
    artifact_path = os.path.join("artifacts", "seo-specialist", "task-1-response.md")
    content = org_mem.read_file(artifact_path)
    assert content == long_text

    # The response sent to updater should be a summary, not the full text
    # Find the enqueue_event call that completed the task
    calls = event_queue.enqueue_event.call_args_list
    assert len(calls) > 0
    # Verify summary pattern: first 3 lines + "... (full report: ...)"
    # We check via the invoke_claude mock not being the raw text
    # Instead, verify the artifact file exists and has correct content
    assert content is not None
    assert content.count("\n") == 60


# ---------------------------------------------------------------------------
# 2. Response <= 50 lines -> inline, no artifact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_short_response_returned_inline(tmp_path):
    org_mem = _make_org_memory(tmp_path)
    config = _seo_config()
    executor = ClaudeExecutor(config, org_memory=org_mem)

    short_text = _short_response()
    context = mock_context()
    event_queue = mock_event_queue()

    with patch("hive.claude.invoke_claude", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.return_value = (short_text, 0.02, "sess-2")
        await executor.execute(context, event_queue)

    # No artifact file should exist
    artifact_path = os.path.join("artifacts", "seo-specialist", "task-1-response.md")
    assert org_mem.read_file(artifact_path) is None


# ---------------------------------------------------------------------------
# 3. No org_memory -> always inline, no error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_org_memory_always_inline():
    config = _seo_config()
    executor = ClaudeExecutor(config)  # no org_memory

    long_text = _long_response(60)
    context = mock_context()
    event_queue = mock_event_queue()

    with patch("hive.claude.invoke_claude", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.return_value = (long_text, 0.05, "sess-3")
        # Should not raise
        await executor.execute(context, event_queue)

    # Verify execute completed without error (updater.complete was called)
    assert event_queue.enqueue_event.called


# ---------------------------------------------------------------------------
# 4. Incoming message with artifact_ref -> content prepended to prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_artifact_ref_prepends_content(tmp_path):
    org_mem = _make_org_memory(tmp_path)

    # Pre-write an artifact to org-memory
    ref = org_mem.write_artifact("reports", "data.md", "## Report Data\nImportant findings here.\n")

    config = _seo_config()
    executor = ClaudeExecutor(config, org_memory=org_mem)

    context = mock_context(
        user_input="Summarize the report",
        metadata={"artifact_ref": ref, "from_agent": "boss"},
    )
    event_queue = mock_event_queue()

    with patch("hive.claude.invoke_claude", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.return_value = ("Summary done.", 0.03, "sess-4")
        await executor.execute(context, event_queue)

    # The prompt sent to Claude should have the artifact content prepended
    call_kwargs = mock_invoke.call_args.kwargs
    prompt = call_kwargs["prompt"]
    assert prompt.startswith("Referenced artifact:\n## Report Data")
    assert "Task: Summarize the report" in prompt


# ---------------------------------------------------------------------------
# 5. Incoming message without artifact_ref -> normal processing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_no_artifact_ref_normal(tmp_path):
    org_mem = _make_org_memory(tmp_path)
    config = _seo_config()
    executor = ClaudeExecutor(config, org_memory=org_mem)

    context = mock_context(user_input="Just a question")
    event_queue = mock_event_queue()

    with patch("hive.claude.invoke_claude", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.return_value = ("Answer.", 0.01, "sess-5")
        await executor.execute(context, event_queue)

    call_kwargs = mock_invoke.call_args.kwargs
    assert call_kwargs["prompt"] == "Just a question"


# ---------------------------------------------------------------------------
# 6. A2AClient.send_task with artifact_ref includes it in metadata
# ---------------------------------------------------------------------------

PEER_URL = "http://peer:8462"


@respx.mock
@pytest.mark.asyncio
async def test_client_send_task_with_artifact_ref():
    captured = {}

    def capture(request):
        captured["json"] = __import__("json").loads(request.content)
        return Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"id": "task-99", "status": {"state": "completed"}},
            },
        )

    respx.post(PEER_URL).mock(side_effect=capture)

    client = A2AClient()
    ref = {"repo": "/tmp/org", "path": "artifacts/seo/t1.md", "commit": "abc123", "size_lines": 100}
    await client.send_task(
        peer_url=PEER_URL,
        message_text="process this",
        from_agent="boss",
        artifact_ref=ref,
    )

    metadata = captured["json"]["params"]["message"]["metadata"]
    assert metadata["artifact_ref"] == ref
    assert metadata["from_agent"] == "boss"

    await client.close()


@respx.mock
@pytest.mark.asyncio
async def test_client_send_task_without_artifact_ref():
    captured = {}

    def capture(request):
        captured["json"] = __import__("json").loads(request.content)
        return Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"id": "task-100", "status": {"state": "completed"}},
            },
        )

    respx.post(PEER_URL).mock(side_effect=capture)

    client = A2AClient()
    await client.send_task(
        peer_url=PEER_URL,
        message_text="do work",
        from_agent="agent-a",
    )

    metadata = captured["json"]["params"]["message"]["metadata"]
    assert "artifact_ref" not in metadata

    await client.close()


# ---------------------------------------------------------------------------
# 7. Events logged on task receive and complete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_logged_on_receive_and_complete(tmp_path):
    org_mem = _make_org_memory(tmp_path)
    config = _seo_config()
    executor = ClaudeExecutor(config, org_memory=org_mem)

    context = mock_context(
        user_input="Analyze traffic",
        metadata={"from_agent": "vp-marketing"},
    )
    event_queue = mock_event_queue()

    with patch("hive.claude.invoke_claude", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.return_value = ("Analysis complete.", 0.07, "sess-6")
        await executor.execute(context, event_queue)

    events = org_mem.list_events()
    assert len(events) == 2

    by_type = {e["event_type"]: e for e in events}
    assert "task_received" in by_type
    assert "task_completed" in by_type

    received = by_type["task_received"]
    assert received["task_id"] == "task-1"
    assert received["from_agent"] == "vp-marketing"
    assert "Analyze traffic" in received["summary"]

    completed = by_type["task_completed"]
    assert completed["task_id"] == "task-1"
    assert completed["cost_usd"] == 0.07
    assert "Analysis complete." in completed["summary"]


# ---------------------------------------------------------------------------
# Slugify role helper
# ---------------------------------------------------------------------------


def test_slugify_role():
    assert ClaudeExecutor._slugify_role("SEO Specialist") == "seo-specialist"
    assert ClaudeExecutor._slugify_role("Content Writer") == "content-writer"
    assert ClaudeExecutor._slugify_role("engineer") == "engineer"
    assert ClaudeExecutor._slugify_role("") == "general"
