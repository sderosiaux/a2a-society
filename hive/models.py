from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class SkillDef(BaseModel):
    id: str
    name: str


class ReportingConfig(BaseModel):
    to: str
    frequency: str = "weekly"


class BudgetConfig(BaseModel):
    daily_max_usd: float = 5.0
    weekly_max_usd: float = 25.0
    per_task_max_usd: float = 2.0


class PeerConfig(BaseModel):
    url: str


class AgentConfig(BaseModel):
    name: str
    role: str
    description: str = ""
    reports_to: str | None = None
    skills: list[SkillDef] = []
    tools: list[str] = []
    tools_exclusive: list[str] = []
    objectives: list[str] = []
    reporting: ReportingConfig | None = None
    budget: BudgetConfig = BudgetConfig()
    knowledge_dir: str | None = None
    initiative_interval_minutes: int = 30
    peers: list[PeerConfig] = []
    registry_url: str | None = None
    org_memory_url: str | None = None
    host: str = "0.0.0.0"
    port: int = 8462


class BudgetStatus(str, Enum):
    active = "active"
    warning = "warning"
    vacation = "vacation"
    offline = "offline"


class BudgetState(BaseModel):
    config: BudgetConfig
    spent_today: float = 0.0
    spent_week: float = 0.0
    status: BudgetStatus = BudgetStatus.active


class BudgetSummary(BaseModel):
    remaining_today_usd: float
    daily_max: float
    weekly_max: float


class HiveExtensions(BaseModel):
    role: str
    reports_to: str | None = None
    tools_exclusive: list[str] = []
    objectives: list[str] = []
    reporting: ReportingConfig | None = None
    budget: BudgetSummary | None = None
    status: BudgetStatus = BudgetStatus.active


class TaskMetadata(BaseModel):
    from_agent: str
    priority: str = "normal"
    callback_url: str | None = None
    artifact_ref: dict | None = None


class ArtifactRef(BaseModel):
    repo: str
    path: str
    commit: str
    size_lines: int
