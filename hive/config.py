from __future__ import annotations

from pathlib import Path

import yaml

from hive.models import AgentConfig


def load_config(path: str) -> AgentConfig:
    """Load and validate an agent config from a YAML file."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with p.open() as f:
        data = yaml.safe_load(f)
    return AgentConfig.model_validate(data)
