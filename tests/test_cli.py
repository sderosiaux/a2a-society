from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from hive.cli import cli


@pytest.fixture()
def runner():
    return CliRunner()


def test_help_shows_commands(runner: CliRunner):
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    for cmd in ("join", "leave", "registry", "status"):
        assert cmd in result.output


def test_join_defaults_name_from_role(runner: CliRunner):
    """--name defaults to slugified --role; run_server is mocked."""
    captured = {}

    def fake_run_server(config):
        captured["config"] = config

    with patch("hive.server.run_server", fake_run_server):
        result = runner.invoke(cli, ["join", "--role", "SEO Specialist", "--port", "9999"])

    assert result.exit_code == 0, result.output
    cfg = captured["config"]
    assert cfg.name == "seo-specialist"
    assert cfg.role == "SEO Specialist"
    assert cfg.port == 9999
    assert cfg.budget.daily_max_usd == 5.0
    assert "Joining Hive as 'seo-specialist'" in result.output


def test_join_from_config_file(runner: CliRunner, tmp_path: Path):
    """--config loads from YAML, run_server is mocked."""
    yaml_content = textwrap.dedent("""\
        name: "yaml-agent"
        role: "Content Writer"
        port: 7777
    """)
    cfg_file = tmp_path / "agent.yaml"
    cfg_file.write_text(yaml_content)

    captured = {}

    def fake_run_server(config):
        captured["config"] = config

    with patch("hive.server.run_server", fake_run_server):
        result = runner.invoke(cli, ["join", "--role", "ignored", "--config", str(cfg_file)])

    assert result.exit_code == 0, result.output
    cfg = captured["config"]
    assert cfg.name == "yaml-agent"
    assert cfg.role == "Content Writer"
    assert cfg.port == 7777


def test_leave_graceful(runner: CliRunner):
    mock_response = MagicMock()
    mock_response.status_code = 200
    with patch("httpx.post", return_value=mock_response):
        result = runner.invoke(cli, ["leave", "--graceful", "--port", "9999"])
    assert result.exit_code == 0
    assert "Gracefully leaving" in result.output
    assert "Shutdown request accepted" in result.output


def test_leave_force(runner: CliRunner):
    mock_response = MagicMock()
    mock_response.status_code = 200
    with patch("httpx.post", return_value=mock_response):
        result = runner.invoke(cli, ["leave", "--force", "--port", "9999"])
    assert result.exit_code == 0
    assert "Force leaving" in result.output


def test_leave_unreachable_agent(runner: CliRunner):
    """Leave when agent is not running should exit with error."""
    import httpx

    with patch("httpx.post", side_effect=httpx.ConnectError("refused")):
        result = runner.invoke(cli, ["leave", "--port", "9999"])
    assert result.exit_code == 1
    assert "Cannot connect" in result.output


def test_leave_with_auth_token(runner: CliRunner):
    """Leave passes auth token in header."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    with patch("httpx.post", return_value=mock_response) as mock_post:
        result = runner.invoke(
            cli, ["leave", "--port", "9999", "--auth-token", "secret"]
        )
    assert result.exit_code == 0
    call_kwargs = mock_post.call_args
    assert call_kwargs[1]["headers"]["Authorization"] == "Bearer secret"


def test_status_prints_agent_table(runner: CliRunner):
    """Mock httpx.get to return a fake agent list."""
    mock_response = MagicMock()
    mock_response.json.return_value = [
        {
            "name": "seo-agent",
            "hive": {
                "role": "SEO Specialist",
                "status": "active",
                "budget": {"remaining_today_usd": 4.5, "daily_max": 5.0},
            },
        },
    ]

    with patch("httpx.get", return_value=mock_response):
        result = runner.invoke(cli, ["status", "--registry", "http://localhost:8080"])

    assert result.exit_code == 0
    assert "seo-agent" in result.output
    assert "SEO Specialist" in result.output
    assert "$4.5/$5.0" in result.output


def test_status_unreachable_registry(runner: CliRunner):
    """Unreachable registry shows error and exits 1."""
    with patch("httpx.get", side_effect=ConnectionError("refused")):
        result = runner.invoke(cli, ["status", "--registry", "http://localhost:9999"])

    assert result.exit_code == 1
    assert "Failed to reach registry" in result.output


def test_status_no_agents(runner: CliRunner):
    """Empty agent list prints 'No agents registered.'"""
    mock_response = MagicMock()
    mock_response.json.return_value = []

    with patch("httpx.get", return_value=mock_response):
        result = runner.invoke(cli, ["status", "--registry", "http://localhost:8080"])

    assert result.exit_code == 0
    assert "No agents registered." in result.output
