from __future__ import annotations

from hive.budget import BudgetManager
from hive.models import BudgetConfig, BudgetStatus


def _make_mgr(
    daily: float = 10.0,
    weekly: float = 50.0,
    per_task: float = 2.0,
) -> BudgetManager:
    return BudgetManager(
        BudgetConfig(
            daily_max_usd=daily,
            weekly_max_usd=weekly,
            per_task_max_usd=per_task,
        )
    )


def test_fresh_budget_allows_execution():
    mgr = _make_mgr()
    allowed, max_budget = mgr.check_before_execution()
    assert allowed is True
    assert max_budget == 2.0  # per_task_max


def test_spend_to_79_percent_stays_active():
    mgr = _make_mgr(daily=10.0)
    mgr.record_cost(7.9)
    assert mgr.status == BudgetStatus.active


def test_spend_to_80_percent_triggers_warning():
    mgr = _make_mgr(daily=10.0)
    mgr.record_cost(8.0)
    assert mgr.status == BudgetStatus.warning


def test_spend_to_100_percent_triggers_vacation():
    mgr = _make_mgr(daily=10.0)
    mgr.record_cost(10.0)
    assert mgr.status == BudgetStatus.vacation
    allowed, max_budget = mgr.check_before_execution()
    assert allowed is False
    assert max_budget == 0.0


def test_per_task_max_caps_budget():
    # remaining $3, per_task $2 -> returns $2
    mgr = _make_mgr(daily=10.0, per_task=2.0)
    mgr.record_cost(7.0)
    allowed, max_budget = mgr.check_before_execution()
    assert allowed is True
    assert max_budget == 2.0


def test_remaining_less_than_per_task():
    # remaining $1, per_task $2 -> returns $1
    mgr = _make_mgr(daily=10.0, per_task=2.0)
    mgr.record_cost(9.0)
    allowed, max_budget = mgr.check_before_execution()
    assert allowed is True
    assert max_budget == 1.0


def test_record_cost_updates_both_totals():
    mgr = _make_mgr()
    mgr.record_cost(1.5)
    mgr.record_cost(0.5)
    assert mgr.spent_today == 2.0
    assert mgr.spent_week == 2.0


def test_reset_daily_restores_active():
    mgr = _make_mgr(daily=10.0)
    mgr.record_cost(10.0)
    assert mgr.status == BudgetStatus.vacation

    mgr.reset_daily()
    assert mgr.spent_today == 0.0
    assert mgr.spent_week == 10.0  # weekly not reset
    assert mgr.status == BudgetStatus.active


def test_reset_weekly_resets_both():
    mgr = _make_mgr(daily=10.0, weekly=50.0)
    mgr.record_cost(10.0)
    mgr.reset_weekly()
    assert mgr.spent_today == 0.0
    assert mgr.spent_week == 0.0
    assert mgr.status == BudgetStatus.active


def test_weekly_budget_hit_triggers_vacation():
    mgr = _make_mgr(daily=10.0, weekly=15.0)
    # Day 1: spend 10, reset daily
    mgr.record_cost(10.0)
    mgr.reset_daily()
    assert mgr.status == BudgetStatus.active

    # Day 2: spend 5 -> weekly total 15 = 100%
    mgr.record_cost(5.0)
    assert mgr.status == BudgetStatus.vacation


def test_to_log_entry_contains_all_fields():
    mgr = _make_mgr(daily=10.0)
    mgr.record_cost(3.0)
    entry = mgr.to_log_entry()

    assert "timestamp" in entry
    assert entry["spent_today"] == 3.0
    assert entry["spent_week"] == 3.0
    assert entry["remaining_today"] == 7.0
    assert entry["status"] == "active"


def test_to_heartbeat_data_correct_remaining():
    mgr = _make_mgr(daily=10.0, weekly=50.0)
    mgr.record_cost(4.0)
    data = mgr.to_heartbeat_data()

    assert data["remaining_today_usd"] == 6.0
    assert data["daily_max"] == 10.0
    assert data["weekly_max"] == 50.0
    assert data["status"] == "active"
