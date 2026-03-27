from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone, timedelta

import yaml

from hive.org_memory import OrgMemory


def _make_memory(tmp_path, agent: str = "alice") -> OrgMemory:
    mem = OrgMemory(repo_url=None, local_path=str(tmp_path / "repo"), agent_name=agent)
    mem.init_or_clone()
    return mem


# --- init ---


def test_init_new_repo(tmp_path):
    mem = _make_memory(tmp_path)
    assert mem.repo is not None
    assert os.path.isdir(os.path.join(str(tmp_path / "repo"), ".git"))
    # HEAD should exist (initial commit)
    assert mem.repo.head.commit is not None


def test_init_existing_repo(tmp_path):
    """Opening an already-initialized repo should reuse it."""
    path = str(tmp_path / "repo")
    mem1 = OrgMemory(repo_url=None, local_path=path, agent_name="a")
    mem1.init_or_clone()
    sha1 = mem1.repo.head.commit.hexsha

    mem2 = OrgMemory(repo_url=None, local_path=path, agent_name="b")
    mem2.init_or_clone()
    assert mem2.repo.head.commit.hexsha == sha1


# --- write_artifact / read_file ---


def test_write_artifact_creates_file_and_returns_ref(tmp_path):
    mem = _make_memory(tmp_path)
    content = "line1\nline2\nline3\n"
    ref = mem.write_artifact("reports", "q1.md", content)

    assert ref["path"] == os.path.join("artifacts", "reports", "q1.md")
    assert ref["size_lines"] == 3
    assert len(ref["commit"]) == 40  # full SHA
    assert ref["repo"] == str(tmp_path / "repo")

    # File actually exists
    assert mem.read_file(os.path.join("artifacts", "reports", "q1.md")) == content


def test_read_file_returns_none_for_missing(tmp_path):
    mem = _make_memory(tmp_path)
    assert mem.read_file("does/not/exist.txt") is None


def test_read_file_returns_content(tmp_path):
    mem = _make_memory(tmp_path)
    mem.write_artifact("docs", "hello.txt", "hello world")
    assert mem.read_file(os.path.join("artifacts", "docs", "hello.txt")) == "hello world"


# --- append_event ---


def test_append_event_creates_yaml(tmp_path):
    mem = _make_memory(tmp_path)
    mem.append_event("task_completed", {"task_id": "42", "result": "ok"})

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    events_dir = os.path.join(str(tmp_path / "repo"), "events", today)
    assert os.path.isdir(events_dir)

    files = os.listdir(events_dir)
    assert len(files) == 1
    assert files[0].endswith("-alice-task_completed.yaml")

    with open(os.path.join(events_dir, files[0])) as f:
        event = yaml.safe_load(f)

    assert event["agent"] == "alice"
    assert event["event_type"] == "task_completed"
    assert event["task_id"] == "42"
    assert event["result"] == "ok"
    assert "timestamp" in event


# --- append_budget_log ---


def test_append_budget_log_creates_jsonl(tmp_path):
    mem = _make_memory(tmp_path)
    mem.append_budget_log({"cost": 1.5, "task": "t1"})
    mem.append_budget_log({"cost": 0.3, "task": "t2"})

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_path = os.path.join(str(tmp_path / "repo"), "budget-logs", "alice", f"{today}.jsonl")
    assert os.path.isfile(log_path)

    with open(log_path) as f:
        lines = f.read().strip().split("\n")

    assert len(lines) == 2
    assert json.loads(lines[0])["cost"] == 1.5
    assert json.loads(lines[1])["task"] == "t2"


# --- list_events ---


def test_list_events_returns_all(tmp_path):
    mem = _make_memory(tmp_path)
    mem.append_event("ev1", {"k": "v1"})
    # Small delay so filenames differ
    time.sleep(0.01)
    mem.append_event("ev2", {"k": "v2"})

    events = mem.list_events()
    assert len(events) == 2
    assert events[0]["event_type"] == "ev1"
    assert events[1]["event_type"] == "ev2"


def test_list_events_filters_by_date(tmp_path):
    mem = _make_memory(tmp_path)
    mem.append_event("ev1", {})

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert len(mem.list_events(date=today)) == 1
    assert len(mem.list_events(date="1999-01-01")) == 0


def test_list_events_filters_by_agent(tmp_path):
    repo_path = str(tmp_path / "repo")
    alice = OrgMemory(repo_url=None, local_path=repo_path, agent_name="alice")
    alice.init_or_clone()
    alice.append_event("ev_a", {})

    bob = OrgMemory(repo_url=None, local_path=repo_path, agent_name="bob")
    bob.init_or_clone()
    bob.append_event("ev_b", {})

    assert len(alice.list_events(agent="alice")) == 1
    assert len(alice.list_events(agent="bob")) == 1
    assert len(alice.list_events(agent="charlie")) == 0


# --- locking ---


def test_acquire_lock_creates_lock_file(tmp_path):
    mem = _make_memory(tmp_path)
    assert mem.acquire_lock("plans/roadmap.md") is True

    lock_path = os.path.join(str(tmp_path / "repo"), ".lock", "plans/roadmap.md.lock")
    assert os.path.isfile(lock_path)

    with open(lock_path) as f:
        data = json.load(f)
    assert data["agent"] == "alice"
    assert "until" in data


def test_second_acquire_returns_false(tmp_path):
    mem = _make_memory(tmp_path)
    assert mem.acquire_lock("resource.txt") is True
    assert mem.acquire_lock("resource.txt") is False


def test_release_lock_removes_file(tmp_path):
    mem = _make_memory(tmp_path)
    mem.acquire_lock("resource.txt")
    mem.release_lock("resource.txt")

    lock_path = os.path.join(str(tmp_path / "repo"), ".lock", "resource.txt.lock")
    assert not os.path.isfile(lock_path)


def test_release_then_acquire_succeeds(tmp_path):
    mem = _make_memory(tmp_path)
    mem.acquire_lock("resource.txt")
    mem.release_lock("resource.txt")
    assert mem.acquire_lock("resource.txt") is True


def test_expired_lock_can_be_overwritten(tmp_path):
    mem = _make_memory(tmp_path)
    # Acquire with 1-second timeout
    mem.acquire_lock("resource.txt", timeout_seconds=1)

    # Manually expire the lock by rewriting with past timestamp
    lock_path = os.path.join(str(tmp_path / "repo"), ".lock", "resource.txt.lock")
    expired = {
        "agent": "alice",
        "until": (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),
    }
    with open(lock_path, "w") as f:
        json.dump(expired, f)

    # A new acquire should succeed since the lock is expired
    mem2 = OrgMemory(repo_url=None, local_path=str(tmp_path / "repo"), agent_name="bob")
    mem2.init_or_clone()
    assert mem2.acquire_lock("resource.txt") is True

    with open(lock_path) as f:
        data = json.load(f)
    assert data["agent"] == "bob"
