from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from hive.models import AgentConfig, ReportingConfig
from hive.reporting import ReportGenerator


# -- helpers ---------------------------------------------------------------


def _make_config(**overrides) -> AgentConfig:
    defaults = {
        "name": "eng-lead",
        "role": "Engineering Lead",
        "objectives": ["Ship v2", "Reduce tech debt"],
        "initiative_interval_minutes": 1,
    }
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _mock_claude(text: str = "# Status Report\nAll good."):
    async def _fn(prompt: str, system_prompt: str):
        return text, 0.03, None

    return _fn


class FakeOrgMemory:
    """Minimal stub for OrgMemory."""

    def __init__(self, events: list[dict] | None = None):
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


class FakeDiscovery:
    def __init__(self, by_role: list[dict] | None = None, all_agents: list[dict] | None = None):
        self._by_role = by_role or []
        self._all = all_agents or []

    async def discover_by_role(self, role: str) -> list[dict]:
        return self._by_role

    async def discover_all(self) -> list[dict]:
        return self._all


class FakeA2AClient:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_task(self, peer_url: str, message_text: str, from_agent: str, **kwargs):
        self.sent.append({
            "peer_url": peer_url,
            "message_text": message_text,
            "from_agent": from_agent,
            **kwargs,
        })
        return {"task_id": "report-001", "status": "submitted"}


# -- tests -----------------------------------------------------------------


def test_should_report_false_no_reporting_config(tmp_path):
    """No reporting config -> should_report returns False."""
    config = _make_config(reporting=None)
    rg = ReportGenerator(
        config=config,
        claude_fn=_mock_claude(),
        state_file=str(tmp_path / "state.json"),
    )
    assert rg.should_report() is False


def test_should_report_true_first_call(tmp_path):
    """First call with reporting config -> True (no prior report)."""
    config = _make_config(reporting=ReportingConfig(to="cto", frequency="daily"))
    rg = ReportGenerator(
        config=config,
        claude_fn=_mock_claude(),
        state_file=str(tmp_path / "state.json"),
    )
    assert rg.should_report() is True


def test_should_report_false_interval_not_elapsed(tmp_path):
    """Interval not elapsed -> False."""
    state_file = tmp_path / "state.json"
    now = datetime.now(timezone.utc)
    state_file.write_text(json.dumps({"last_report": now.isoformat()}))

    config = _make_config(reporting=ReportingConfig(to="cto", frequency="daily"))
    rg = ReportGenerator(
        config=config,
        claude_fn=_mock_claude(),
        state_file=str(state_file),
    )
    assert rg.should_report() is False


def test_should_report_true_interval_elapsed(tmp_path):
    """Interval elapsed -> True."""
    state_file = tmp_path / "state.json"
    old = datetime.now(timezone.utc) - timedelta(hours=25)
    state_file.write_text(json.dumps({"last_report": old.isoformat()}))

    config = _make_config(reporting=ReportingConfig(to="cto", frequency="daily"))
    rg = ReportGenerator(
        config=config,
        claude_fn=_mock_claude(),
        state_file=str(state_file),
    )
    assert rg.should_report() is True


@pytest.mark.asyncio
async def test_generate_and_send_commits_to_org_memory(tmp_path):
    """Report is committed as artifact, returns artifact_ref."""
    org = FakeOrgMemory(events=[
        {"agent": "eng-lead", "timestamp": "2026-03-27T10:00:00", "event": "task_done", "summary": "Shipped v2 API"},
    ])
    config = _make_config(reporting=ReportingConfig(to="cto", frequency="daily"))

    rg = ReportGenerator(
        config=config,
        claude_fn=_mock_claude("# Report\nShipped v2 API."),
        org_memory=org,
        state_file=str(tmp_path / "state.json"),
    )
    ref = await rg.generate_and_send()

    assert ref is not None
    assert ref["path"].startswith("artifacts/engineering/reports/")
    assert ref["path"].endswith("-status.md")
    assert len(org.written) == 1
    assert org.written[0]["content"] == "# Report\nShipped v2 API."


@pytest.mark.asyncio
async def test_generate_and_send_sends_a2a_to_superior(tmp_path):
    """Report is sent via A2A to the superior."""
    client = FakeA2AClient()
    discovery = FakeDiscovery(by_role=[{"name": "cto-agent", "url": "http://cto:8462"}])
    config = _make_config(reporting=ReportingConfig(to="cto", frequency="daily"))

    rg = ReportGenerator(
        config=config,
        claude_fn=_mock_claude("Short report."),
        client=client,
        discovery=discovery,
        state_file=str(tmp_path / "state.json"),
    )
    await rg.generate_and_send()

    assert len(client.sent) == 1
    assert client.sent[0]["peer_url"] == "http://cto:8462"
    assert "Status report from eng-lead" in client.sent[0]["message_text"]
    assert client.sent[0]["from_agent"] == "eng-lead"


@pytest.mark.asyncio
async def test_last_report_persisted_and_loaded(tmp_path):
    """Timestamp persists across instances."""
    state_file = str(tmp_path / "state.json")
    config = _make_config(reporting=ReportingConfig(to="cto", frequency="daily"))

    rg1 = ReportGenerator(
        config=config,
        claude_fn=_mock_claude(),
        state_file=state_file,
    )
    assert rg1.should_report() is True

    await rg1.generate_and_send()
    assert rg1.should_report() is False

    # New instance reads persisted state
    rg2 = ReportGenerator(
        config=config,
        claude_fn=_mock_claude(),
        state_file=state_file,
    )
    assert rg2.should_report() is False
    assert rg2._last_report is not None


@pytest.mark.asyncio
async def test_generate_and_send_no_org_memory_still_sends(tmp_path):
    """No org_memory -> skip commit, still send A2A."""
    client = FakeA2AClient()
    discovery = FakeDiscovery(by_role=[{"name": "cto-agent", "url": "http://cto:8462"}])
    config = _make_config(reporting=ReportingConfig(to="cto", frequency="daily"))

    rg = ReportGenerator(
        config=config,
        claude_fn=_mock_claude("Report without org-memory."),
        org_memory=None,
        client=client,
        discovery=discovery,
        state_file=str(tmp_path / "state.json"),
    )
    ref = await rg.generate_and_send()

    # No artifact committed
    assert ref is None
    # But A2A message was sent
    assert len(client.sent) == 1
    assert "Status report from eng-lead" in client.sent[0]["message_text"]
