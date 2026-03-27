from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from rich.panel import Panel
from rich.table import Table

from hive.cli import cli
from hive.dashboard import HiveDashboard

AGENTS = [
    {
        "name": "seo-agent",
        "queue_depth": 3,
        "hive": {
            "role": "SEO Specialist",
            "status": "active",
            "budget": {"remaining_today_usd": 4.0, "daily_max": 5.0},
        },
    },
    {
        "name": "writer-agent",
        "queue_depth": 0,
        "hive": {
            "role": "Content Writer",
            "status": "vacation",
            "budget": {"remaining_today_usd": 5.0, "daily_max": 5.0},
        },
    },
]


@pytest.fixture()
def dash() -> HiveDashboard:
    return HiveDashboard("http://localhost:8080")


def test_build_agent_table_with_agents(dash: HiveDashboard):
    table = dash.build_agent_table(AGENTS)
    assert isinstance(table, Table)
    assert table.row_count == 2


def test_build_agent_table_empty(dash: HiveDashboard):
    table = dash.build_agent_table([])
    assert isinstance(table, Table)
    assert table.row_count == 0


def test_build_activity_panel_with_events(dash: HiveDashboard):
    events = [
        {"timestamp": "2026-03-27T10:00:00", "agent": "seo-agent", "event": "task_done", "summary": "Finished audit"},
        {"timestamp": "2026-03-27T09:00:00", "agent": "writer-agent", "from": "seo-agent", "summary": "Delegated blog post"},
    ]
    panel = dash.build_activity_panel(events)
    assert isinstance(panel, Panel)
    rendered = str(panel.renderable)
    assert "seo-agent" in rendered
    assert "Finished audit" in rendered
    assert "writer-agent <- seo-agent" in rendered


def test_build_activity_panel_empty(dash: HiveDashboard):
    panel = dash.build_activity_panel([])
    assert isinstance(panel, Panel)
    assert "No recent activity" in str(panel.renderable)


def test_build_footer_calculates_spend(dash: HiveDashboard):
    footer = dash.build_footer(AGENTS)
    # seo-agent spent 1.0 (5.0-4.0), writer-agent spent 0.0 => total 1.0
    assert "$1.00" in footer
    assert "Active: 1" in footer
    assert "On vacation: 1" in footer


def test_fetch_agents_with_mock(dash: HiveDashboard):
    mock_resp = MagicMock()
    mock_resp.json.return_value = AGENTS
    with patch("httpx.get", return_value=mock_resp):
        agents = dash.fetch_agents()
    assert len(agents) == 2
    assert agents[0]["name"] == "seo-agent"


def test_fetch_agents_on_error(dash: HiveDashboard):
    with patch("httpx.get", side_effect=ConnectionError("refused")):
        agents = dash.fetch_agents()
    assert agents == []


def test_read_recent_events_from_tmpdir(tmp_path: Path):
    events_dir = tmp_path / "events" / "2026-03-27"
    events_dir.mkdir(parents=True)

    (events_dir / "001.yaml").write_text(
        "timestamp: '2026-03-27T10:00:00'\nagent: seo-agent\nevent: task_done\nsummary: Finished audit\n"
    )
    (events_dir / "002.yaml").write_text(
        "timestamp: '2026-03-27T11:00:00'\nagent: writer-agent\nevent: report\nsummary: Weekly report\n"
    )

    dash = HiveDashboard("http://localhost:8080", org_memory_path=str(tmp_path))
    events = dash.read_recent_events()
    assert len(events) == 2
    # Most recent first
    assert events[0]["agent"] == "writer-agent"
    assert events[1]["agent"] == "seo-agent"


def test_read_recent_events_no_org_memory(dash: HiveDashboard):
    events = dash.read_recent_events()
    assert events == []


def test_dashboard_help_in_cli():
    runner = CliRunner()
    result = runner.invoke(cli, ["dashboard", "--help"])
    assert result.exit_code == 0
    assert "Live dashboard" in result.output
    assert "--registry" in result.output
    assert "--org-memory" in result.output
    assert "--refresh" in result.output
