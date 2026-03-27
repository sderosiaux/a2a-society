from __future__ import annotations

import logging
from datetime import UTC, datetime

from hive.models import BudgetConfig, BudgetStatus

logger = logging.getLogger(__name__)


class BudgetManager:
    """Tracks spending and enforces budget limits with circuit breaker states."""

    def __init__(self, config: BudgetConfig) -> None:
        self._config = config
        self._spent_today: float = 0.0
        self._spent_week: float = 0.0
        self._status: BudgetStatus = BudgetStatus.active

    @property
    def status(self) -> BudgetStatus:
        return self._status

    @property
    def spent_today(self) -> float:
        return self._spent_today

    @property
    def spent_week(self) -> float:
        return self._spent_week

    @property
    def remaining_today(self) -> float:
        return max(0.0, self._config.daily_max_usd - self._spent_today)

    @property
    def remaining_week(self) -> float:
        return max(0.0, self._config.weekly_max_usd - self._spent_week)

    def check_before_execution(self) -> tuple[bool, float]:
        """Check if execution is allowed.

        Returns: (allowed, max_budget_for_task)

        If remaining budget <= 0: not allowed, return (False, 0.0).
        Otherwise: allowed, max = min(per_task_max, remaining_today, remaining_week).
        """
        remaining_day = self.remaining_today
        remaining_wk = self.remaining_week
        remaining = min(remaining_day, remaining_wk)

        if remaining <= 0:
            return False, 0.0

        max_budget = min(self._config.per_task_max_usd, remaining)
        return True, max_budget

    def record_cost(self, cost_usd: float) -> BudgetStatus:
        """Record actual cost after a Claude call.

        Updates spent_today and spent_week. Re-evaluates status:
        - spent >= 100% daily or weekly -> VACATION
        - spent >= 80% daily -> WARNING
        - else -> ACTIVE

        Returns the new status.
        """
        self._spent_today += cost_usd
        self._spent_week += cost_usd
        self._status = self._evaluate_status()
        return self._status

    def _evaluate_status(self) -> BudgetStatus:
        daily_pct = self._spent_today / self._config.daily_max_usd if self._config.daily_max_usd > 0 else 1.0
        weekly_pct = self._spent_week / self._config.weekly_max_usd if self._config.weekly_max_usd > 0 else 1.0

        if daily_pct >= 1.0 or weekly_pct >= 1.0:
            return BudgetStatus.vacation
        if daily_pct >= 0.8:
            return BudgetStatus.warning
        return BudgetStatus.active

    def reset_daily(self) -> None:
        """Reset daily spend to 0. Re-evaluate status."""
        self._spent_today = 0.0
        self._status = self._evaluate_status()

    def reset_weekly(self) -> None:
        """Reset both daily and weekly spend to 0."""
        self._spent_today = 0.0
        self._spent_week = 0.0
        self._status = self._evaluate_status()

    def to_log_entry(self) -> dict:
        """Return a dict for appending to budget-logs JSONL."""
        return {
            "timestamp": datetime.now(UTC).isoformat(),
            "spent_today": self._spent_today,
            "spent_week": self._spent_week,
            "remaining_today": self.remaining_today,
            "status": self._status.value,
        }

    def to_heartbeat_data(self) -> dict:
        """Return budget data for registry heartbeat."""
        return {
            "remaining_today_usd": self.remaining_today,
            "daily_max": self._config.daily_max_usd,
            "weekly_max": self._config.weekly_max_usd,
            "status": self._status.value,
        }
