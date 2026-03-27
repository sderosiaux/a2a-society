from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import ResultMessage

from hive.models import AgentConfig, BudgetConfig
from hive.prompt_builder import build_system_prompt

# ---------------------------------------------------------------------------
# build_system_prompt tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def full_config() -> AgentConfig:
    return AgentConfig(
        name="seo-agent",
        role="SEO Specialist",
        reports_to="vp-marketing",
        objectives=[
            "Increase organic traffic by 20%",
            "Reduce bounce rate",
        ],
        tools=["semrush", "ahrefs"],
        tools_exclusive=["google-ads"],
        budget=BudgetConfig(
            daily_max_usd=10.0,
            per_task_max_usd=3.0,
        ),
    )


@pytest.fixture()
def minimal_config() -> AgentConfig:
    return AgentConfig(name="worker", role="Worker")


def test_build_system_prompt_full(full_config: AgentConfig):
    prompt = build_system_prompt(full_config, known_peers=["content-writer", "link-builder"])

    assert "SEO Specialist" in prompt
    assert "You report to vp-marketing." in prompt
    assert "content-writer" in prompt
    assert "link-builder" in prompt
    assert "Increase organic traffic by 20%" in prompt
    assert "Reduce bounce rate" in prompt
    assert "semrush" in prompt
    assert "ahrefs" in prompt
    assert "google-ads" in prompt
    assert "delegate to specialists" in prompt


def test_build_system_prompt_minimal(minimal_config: AgentConfig):
    prompt = build_system_prompt(minimal_config)

    assert "Worker" in prompt
    assert "top of the hierarchy" in prompt
    assert "When you receive a task:" in prompt
    assert "Your objectives:" not in prompt
    assert "Your tools:" not in prompt
    assert "delegate to specialists" not in prompt


def test_build_system_prompt_no_peers(full_config: AgentConfig):
    prompt = build_system_prompt(full_config)
    assert "Your direct reports:" not in prompt


# ---------------------------------------------------------------------------
# invoke_claude tests (mocked)
# ---------------------------------------------------------------------------


def _make_result_message(
    result: str = "test response",
    cost: float = 0.05,
    session_id: str | None = None,
):
    """Create a fake ResultMessage that passes isinstance checks."""
    msg = MagicMock(spec=ResultMessage)
    msg.result = result
    msg.total_cost_usd = cost
    msg.session_id = session_id or str(uuid.uuid4())
    return msg


@pytest.mark.asyncio
async def test_invoke_claude_basic():
    fake_result = _make_result_message(result="Hello from Claude", cost=0.12, session_id="sess-123")

    async def fake_query(**kwargs):
        yield fake_result

    with patch("hive.claude.query", new=fake_query):
        from hive.claude import invoke_claude

        text, cost, session_id = await invoke_claude(
            prompt="Do something",
            system_prompt="You are a tester",
        )

    assert text == "Hello from Claude"
    assert cost == 0.12
    assert session_id == "sess-123"


@pytest.mark.asyncio
async def test_invoke_claude_with_session():
    fake_result = _make_result_message(result="Continued", cost=0.01, session_id="sess-456")

    async def fake_query(**kwargs):
        opts = kwargs.get("options")
        assert opts.continue_conversation is True
        assert opts.resume == "sess-456"
        yield fake_result

    with patch("hive.claude.query", new=fake_query):
        from hive.claude import invoke_claude

        text, _cost, session_id = await invoke_claude(
            prompt="Continue",
            system_prompt="You are a tester",
            session_id="sess-456",
        )

    assert text == "Continued"
    assert session_id == "sess-456"


@pytest.mark.asyncio
async def test_invoke_claude_default_permission_mode():
    fake_result = _make_result_message()

    async def fake_query(**kwargs):
        opts = kwargs.get("options")
        assert opts.permission_mode == "default"
        yield fake_result

    with patch("hive.claude.query", new=fake_query):
        from hive.claude import invoke_claude

        await invoke_claude(
            prompt="Do it",
            system_prompt="sys",
        )


@pytest.mark.asyncio
async def test_invoke_claude_custom_permission_mode():
    fake_result = _make_result_message()

    async def fake_query(**kwargs):
        opts = kwargs.get("options")
        assert opts.permission_mode == "plan"
        yield fake_result

    with patch("hive.claude.query", new=fake_query):
        from hive.claude import invoke_claude

        await invoke_claude(
            prompt="Do it",
            system_prompt="sys",
            permission_mode="plan",
        )


@pytest.mark.asyncio
async def test_invoke_claude_with_budget():
    fake_result = _make_result_message()

    async def fake_query(**kwargs):
        opts = kwargs.get("options")
        assert opts.max_budget_usd == 2.5
        yield fake_result

    with patch("hive.claude.query", new=fake_query):
        from hive.claude import invoke_claude

        await invoke_claude(
            prompt="Do it",
            system_prompt="sys",
            max_budget_usd=2.5,
        )


# ---------------------------------------------------------------------------
# ClaudeExecutor tests (mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_executor():
    from a2a.server.agent_execution import RequestContext
    from a2a.server.events import EventQueue

    from hive.executor import ClaudeExecutor

    config = AgentConfig(
        name="test-claude",
        role="Engineer",
        objectives=["Ship fast"],
        tools=["Bash", "Read"],
        budget=BudgetConfig(daily_max_usd=5.0, per_task_max_usd=2.0),
    )
    executor = ClaudeExecutor(config)

    context = MagicMock(spec=RequestContext)
    context.task_id = "task-1"
    context.context_id = "ctx-1"
    context.get_user_input.return_value = "Write a test"
    context.current_task = None

    event_queue = MagicMock(spec=EventQueue)
    event_queue.enqueue_event = AsyncMock()

    with patch("hive.claude.invoke_claude", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.return_value = ("Done: test written", 0.08, "sess-x")

        await executor.execute(context, event_queue)

        mock_invoke.assert_called_once()
        call_kwargs = mock_invoke.call_args.kwargs
        assert call_kwargs["prompt"] == "Write a test"
        assert "Engineer" in call_kwargs["system_prompt"]
        assert call_kwargs["allowed_tools"] == ["Bash", "Read"]
        assert call_kwargs["max_budget_usd"] == 2.0  # min(per_task, daily)


def test_create_executor_echo():
    from hive.executor import EchoExecutor, create_executor

    config = AgentConfig(name="echo-test", role="Tester")
    executor = create_executor(config, use_echo=True)
    assert isinstance(executor, EchoExecutor)


def test_create_executor_claude():
    from hive.executor import ClaudeExecutor, create_executor

    config = AgentConfig(name="claude-test", role="Dev")
    executor = create_executor(config, use_echo=False)
    assert isinstance(executor, ClaudeExecutor)
