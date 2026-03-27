from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from hive.config import load_config
from hive.models import AgentConfig, BudgetConfig


@pytest.fixture()
def tmp_yaml(tmp_path: Path):
    """Helper that writes YAML content to a temp file and returns its path."""

    def _write(content: str) -> str:
        p = tmp_path / "agent.yaml"
        p.write_text(textwrap.dedent(content))
        return str(p)

    return _write


def test_load_full_config(tmp_yaml):
    path = tmp_yaml("""\
        name: "seo-agent"
        role: "SEO Specialist"
        description: "SEO specialist with semrush access"
        reports_to: "vp-marketing"
        skills:
          - id: "seo-audit"
            name: "SEO site audit"
          - id: "keyword-research"
            name: "Keyword research"
        tools: ["semrush"]
        tools_exclusive: ["semrush"]
        objectives:
          - "Increase organic traffic by 20% in Q2"
        reporting:
          to: "vp-marketing"
          frequency: "weekly"
        budget:
          daily_max_usd: 5.00
          weekly_max_usd: 25.00
          per_task_max_usd: 2.00
        knowledge_dir: "./knowledge/"
        initiative_interval_minutes: 30
        peers: []
        registry_url: null
        org_memory_url: null
        host: "0.0.0.0"
        port: 8462
    """)
    cfg = load_config(path)
    assert cfg.name == "seo-agent"
    assert cfg.role == "SEO Specialist"
    assert len(cfg.skills) == 2
    assert cfg.skills[0].id == "seo-audit"
    assert cfg.tools == ["semrush"]
    assert cfg.reporting is not None
    assert cfg.reporting.to == "vp-marketing"
    assert cfg.budget.daily_max_usd == 5.0
    assert cfg.port == 8462


def test_missing_required_field_raises(tmp_yaml):
    path = tmp_yaml("""\
        role: "SEO Specialist"
    """)
    with pytest.raises(ValidationError) as exc_info:
        load_config(path)
    assert "name" in str(exc_info.value)


def test_budget_defaults(tmp_yaml):
    path = tmp_yaml("""\
        name: "test-agent"
        role: "Tester"
    """)
    cfg = load_config(path)
    assert cfg.budget.daily_max_usd == 5.0
    assert cfg.budget.weekly_max_usd == 25.0
    assert cfg.budget.per_task_max_usd == 2.0


def test_file_not_found():
    with pytest.raises(FileNotFoundError, match="Config file not found"):
        load_config("/nonexistent/path/agent.yaml")


def test_minimal_config(tmp_yaml):
    path = tmp_yaml("""\
        name: "minimal-agent"
        role: "Worker"
    """)
    cfg = load_config(path)
    assert cfg.name == "minimal-agent"
    assert cfg.role == "Worker"
    assert cfg.description == ""
    assert cfg.reports_to is None
    assert cfg.skills == []
    assert cfg.tools == []
    assert cfg.tools_exclusive == []
    assert cfg.objectives == []
    assert cfg.reporting is None
    assert cfg.budget == BudgetConfig()
    assert cfg.knowledge_dir is None
    assert cfg.initiative_interval_minutes == 30
    assert cfg.peers == []
    assert cfg.registry_url is None
    assert cfg.org_memory_url is None
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 8462
