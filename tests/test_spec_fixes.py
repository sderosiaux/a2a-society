"""Tests for spec alignment fixes I1-I7."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient

from hive.budget import BudgetManager
from hive.discovery import DiscoveryClient
from hive.models import AgentConfig, BudgetConfig, BudgetStatus, SkillDef
from hive.prompt_builder import build_system_prompt
from hive.server import (
    _budget_reset_loop,
    _load_knowledge_files,
    _send_intro_message,
    create_app,
)
from tests.conftest import FakeA2AClient, FakeDiscovery, make_config

# ---------------------------------------------------------------------------
# I1: Introduction message on join
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_intro_message_finds_superior_and_sends():
    """_send_intro_message sends to the superior's URL."""
    config = AgentConfig(
        name="junior",
        role="Writer",
        reports_to="boss",
        skills=[SkillDef(id="seo", name="SEO")],
        objectives=["Write content"],
    )
    fake_client = FakeA2AClient()
    fake_discovery = FakeDiscovery(
        all_agents=[
            {"name": "boss", "url": "http://boss:8462"},
            {"name": "junior", "url": "http://junior:8462"},
        ]
    )
    await _send_intro_message(config, fake_discovery, fake_client)

    assert len(fake_client.sent) == 1
    msg = fake_client.sent[0]
    assert msg["peer_url"] == "http://boss:8462"
    assert "I just joined as Writer" in msg["message_text"]
    assert "seo" in msg["message_text"]
    assert "Write content" in msg["message_text"]
    assert msg["from_agent"] == "junior"


@pytest.mark.asyncio
async def test_send_intro_message_superior_not_found_logs_warning():
    """_send_intro_message logs warning when superior is not in registry."""
    config = AgentConfig(
        name="junior",
        role="Writer",
        reports_to="boss",
    )
    fake_client = FakeA2AClient()
    fake_discovery = FakeDiscovery(all_agents=[{"name": "other", "url": "http://other:8462"}])

    # Should not raise
    await _send_intro_message(config, fake_discovery, fake_client)
    assert len(fake_client.sent) == 0


@pytest.mark.asyncio
async def test_send_intro_no_objectives():
    """Intro message handles empty objectives gracefully."""
    config = AgentConfig(
        name="junior",
        role="Writer",
        reports_to="boss",
        skills=[],
        objectives=[],
    )
    fake_client = FakeA2AClient()
    fake_discovery = FakeDiscovery(
        all_agents=[{"name": "boss", "url": "http://boss:8462"}]
    )
    await _send_intro_message(config, fake_discovery, fake_client)
    assert len(fake_client.sent) == 1
    assert "none" in fake_client.sent[0]["message_text"]


# ---------------------------------------------------------------------------
# I2: Heartbeat refreshes budget data
# ---------------------------------------------------------------------------

REGISTRY = "http://registry:9999"


@pytest.mark.asyncio
async def test_heartbeat_calls_card_builder():
    """Heartbeat uses card_builder when provided."""
    call_count = {"n": 0}

    def builder() -> dict:
        call_count["n"] += 1
        return {"name": "fresh", "budget": call_count["n"]}

    with respx.mock:
        respx.post(f"{REGISTRY}/agents/register").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        client = DiscoveryClient(registry_url=REGISTRY)
        try:
            await client.start_heartbeat(
                {"name": "stale"}, interval=0.05, card_builder=builder
            )
            await asyncio.sleep(0.2)
            await client.stop_heartbeat()
            # Builder should have been called at least twice
            assert call_count["n"] >= 2
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_heartbeat_without_builder_uses_static_card():
    """Heartbeat without card_builder posts the static card."""
    with respx.mock:
        route = respx.post(f"{REGISTRY}/agents/register").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        client = DiscoveryClient(registry_url=REGISTRY)
        try:
            await client.start_heartbeat({"name": "static"}, interval=0.05)
            await asyncio.sleep(0.15)
            await client.stop_heartbeat()
            assert route.call_count >= 2
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# I3: Shutdown endpoint
# ---------------------------------------------------------------------------


@pytest.fixture()
def shutdown_config() -> AgentConfig:
    return AgentConfig(
        name="shutdown-agent",
        role="Tester",
        description="Agent with shutdown",
        skills=[SkillDef(id="echo", name="Echo skill")],
    )


@pytest.fixture()
def shutdown_app(shutdown_config):
    return create_app(shutdown_config, use_echo=True)


@pytest_asyncio.fixture()
async def shutdown_client(shutdown_app):
    transport = ASGITransport(app=shutdown_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_admin_shutdown_endpoint_exists(shutdown_client: AsyncClient):
    """POST /admin/shutdown returns 200."""
    resp = await shutdown_client.post("/admin/shutdown")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "Shutting down" in data["message"]


@pytest.mark.asyncio
async def test_admin_shutdown_protected_by_auth():
    """POST /admin/shutdown is protected when auth_token is set."""
    config = AgentConfig(
        name="auth-shutdown-agent",
        role="Tester",
        auth_token="secret-123",
        skills=[SkillDef(id="echo", name="Echo")],
    )
    app = create_app(config, use_echo=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Without token -> 401
        resp = await client.post("/admin/shutdown")
        assert resp.status_code == 401

        # With token -> 200
        resp = await client.post(
            "/admin/shutdown",
            headers={"Authorization": "Bearer secret-123"},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# I4: Budget logs written to org-memory (tested via executor)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_log_written_after_record_cost():
    """After record_cost, executor writes budget log to org_memory."""
    from hive.executor import ClaudeExecutor
    from tests.conftest import mock_context, mock_event_queue

    config = make_config(reports_to=None)
    budget = BudgetManager(BudgetConfig(daily_max_usd=10.0, per_task_max_usd=5.0))

    mock_org = MagicMock()
    mock_org.append_budget_log = MagicMock()
    mock_org.append_event = MagicMock()
    mock_org.pull = MagicMock()
    mock_org.read_file = MagicMock(return_value=None)
    mock_org.write_artifact = MagicMock()

    executor = ClaudeExecutor(
        config,
        budget_manager=budget,
        org_memory=mock_org,
    )

    ctx = mock_context()
    eq = mock_event_queue()

    with patch("hive.claude.invoke_claude", new_callable=AsyncMock) as mock_claude:
        mock_claude.return_value = ("Done", 1.5, "sess-1")
        await executor._execute_task(ctx, eq)

    # Budget log should have been written
    mock_org.append_budget_log.assert_called_once()
    log_entry = mock_org.append_budget_log.call_args[0][0]
    assert log_entry["spent_today"] == 1.5
    assert log_entry["status"] == "active"


# ---------------------------------------------------------------------------
# I5: Daily/weekly reset scheduler (unit test the loop logic)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_reset_loop_resets_daily():
    """Budget reset loop calls reset_daily after sleep."""
    budget = BudgetManager(BudgetConfig(daily_max_usd=10.0))
    budget.record_cost(8.0)
    assert budget.status == BudgetStatus.warning

    # Patch asyncio.sleep to skip the wait, and break after one iteration
    call_count = {"n": 0}

    async def fake_sleep(seconds):
        call_count["n"] += 1
        if call_count["n"] > 1:
            raise asyncio.CancelledError
        # Don't actually sleep

    with patch("hive.server.asyncio.sleep", side_effect=fake_sleep):
        with contextlib.suppress(asyncio.CancelledError):
            await _budget_reset_loop(budget)

    # Daily should have been reset
    assert budget.spent_today == 0.0
    assert budget.status == BudgetStatus.active


# ---------------------------------------------------------------------------
# I6: Vacation notification to superior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vacation_notification_sent_on_budget_depletion():
    """When budget hits vacation, executor sends notification to superior."""
    from hive.executor import ClaudeExecutor
    from tests.conftest import mock_context, mock_event_queue

    config = make_config(reports_to="boss")
    budget = BudgetManager(BudgetConfig(daily_max_usd=2.0, per_task_max_usd=5.0))
    fake_client = FakeA2AClient()
    fake_discovery = FakeDiscovery(
        all_agents=[{"name": "boss", "url": "http://boss:8462"}]
    )

    executor = ClaudeExecutor(
        config,
        budget_manager=budget,
        discovery=fake_discovery,
        a2a_client=fake_client,
    )

    ctx = mock_context()
    eq = mock_event_queue()

    # Cost of 2.0 on a daily_max of 2.0 should trigger vacation
    with patch("hive.claude.invoke_claude", new_callable=AsyncMock) as mock_claude:
        mock_claude.return_value = ("Done", 2.0, "sess-1")
        await executor._execute_task(ctx, eq)

    assert budget.status == BudgetStatus.vacation
    # Vacation notification should have been sent
    assert len(fake_client.sent) == 1
    assert "Budget depleted" in fake_client.sent[0]["message_text"]
    assert fake_client.sent[0]["peer_url"] == "http://boss:8462"


@pytest.mark.asyncio
async def test_no_vacation_notification_when_still_active():
    """No vacation notification when budget is still active."""
    from hive.executor import ClaudeExecutor
    from tests.conftest import mock_context, mock_event_queue

    config = make_config(reports_to="boss")
    budget = BudgetManager(BudgetConfig(daily_max_usd=10.0, per_task_max_usd=5.0))
    fake_client = FakeA2AClient()
    fake_discovery = FakeDiscovery(
        all_agents=[{"name": "boss", "url": "http://boss:8462"}]
    )

    executor = ClaudeExecutor(
        config,
        budget_manager=budget,
        discovery=fake_discovery,
        a2a_client=fake_client,
    )

    ctx = mock_context()
    eq = mock_event_queue()

    with patch("hive.claude.invoke_claude", new_callable=AsyncMock) as mock_claude:
        mock_claude.return_value = ("Done", 1.0, "sess-1")
        await executor._execute_task(ctx, eq)

    assert budget.status == BudgetStatus.active
    assert len(fake_client.sent) == 0


# ---------------------------------------------------------------------------
# I7: Knowledge dir loaded into Claude context
# ---------------------------------------------------------------------------


def test_load_knowledge_files_reads_md(tmp_path):
    """_load_knowledge_files reads and concatenates .md files."""
    (tmp_path / "01-intro.md").write_text("# Intro\nBasics.")
    (tmp_path / "02-advanced.md").write_text("# Advanced\nDeep stuff.")
    (tmp_path / "notes.txt").write_text("ignored")

    content = _load_knowledge_files(str(tmp_path))
    assert content is not None
    assert "# Intro" in content
    assert "# Advanced" in content
    assert "ignored" not in content


def test_load_knowledge_files_missing_dir():
    """_load_knowledge_files returns None for missing dir."""
    result = _load_knowledge_files("/nonexistent/path")
    assert result is None


def test_load_knowledge_files_empty_dir(tmp_path):
    """_load_knowledge_files returns None for dir with no .md files."""
    (tmp_path / "data.csv").write_text("a,b,c")
    result = _load_knowledge_files(str(tmp_path))
    assert result is None


def test_build_system_prompt_includes_knowledge():
    """build_system_prompt includes knowledge_content section."""
    config = make_config()
    prompt = build_system_prompt(config, knowledge_content="SEO best practices go here.")
    assert "## Domain Knowledge" in prompt
    assert "SEO best practices go here." in prompt


def test_build_system_prompt_no_knowledge():
    """build_system_prompt without knowledge_content has no knowledge section."""
    config = make_config()
    prompt = build_system_prompt(config)
    assert "## Domain Knowledge" not in prompt


def test_knowledge_dir_wired_through_create_app(tmp_path):
    """create_app reads knowledge files when knowledge_dir is set."""
    (tmp_path / "domain.md").write_text("Domain knowledge content.")
    config = AgentConfig(
        name="knowledge-agent",
        role="Expert",
        knowledge_dir=str(tmp_path),
        skills=[SkillDef(id="echo", name="Echo")],
    )
    # Should not raise during creation
    app = create_app(config, use_echo=True)
    assert app is not None
