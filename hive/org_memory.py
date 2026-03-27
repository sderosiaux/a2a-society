from __future__ import annotations

import json
import logging
import os
import time

import git
import yaml
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class OrgMemory:
    """Git-backed organizational memory. Clone, read, write, commit, push."""

    def __init__(self, repo_url: str | None, local_path: str, agent_name: str) -> None:
        self._repo_url = repo_url
        self._local_path = local_path
        self._agent_name = agent_name
        self._repo: git.Repo | None = None

    def _safe_path(self, relative_path: str) -> str:
        """Resolve path and verify it's within the repo root."""
        full = os.path.normpath(os.path.join(self._local_path, relative_path))
        root = os.path.normpath(self._local_path)
        if not full.startswith(root + os.sep) and full != root:
            raise ValueError(f"Path traversal blocked: {relative_path}")
        return full

    @property
    def repo(self) -> git.Repo:
        if self._repo is None:
            raise RuntimeError("Repo not initialized. Call init_or_clone() first.")
        return self._repo

    def init_or_clone(self) -> None:
        """
        If local_path exists and is a git repo, open it.
        If repo_url is set and local_path doesn't exist, clone.
        If no repo_url and no existing repo, init a new one.
        """
        if os.path.isdir(os.path.join(self._local_path, ".git")):
            self._repo = git.Repo(self._local_path)
            return

        if self._repo_url and not os.path.exists(self._local_path):
            self._repo = git.Repo.clone_from(self._repo_url, self._local_path)
            return

        os.makedirs(self._local_path, exist_ok=True)
        self._repo = git.Repo.init(self._local_path)

        # Create initial commit so HEAD exists.
        readme = os.path.join(self._local_path, ".gitkeep")
        with open(readme, "w") as f:
            f.write("")
        self._repo.index.add([".gitkeep"])
        self._repo.index.commit("init org-memory")

    def pull(self) -> None:
        """Pull latest from remote. No-op if no remote configured."""
        if not self._has_remote():
            return
        self.repo.remotes.origin.pull()

    def _has_remote(self) -> bool:
        try:
            return len(self.repo.remotes) > 0
        except Exception:
            return False

    def _commit_and_push(self, file_paths: list[str], message: str) -> str:
        """
        Stage files, commit, push to remote.
        Returns the commit SHA.
        On push conflict: pull --rebase, retry push (max 3 attempts).
        No-op push if no remote.
        """
        self.repo.index.add(file_paths)
        commit = self.repo.index.commit(message)
        sha = commit.hexsha

        if self._has_remote():
            for attempt in range(3):
                try:
                    self.repo.remotes.origin.push()
                    break
                except git.GitCommandError:
                    logger.warning("Push conflict, attempt %d/3, rebasing...", attempt + 1)
                    self.repo.remotes.origin.pull(rebase=True)
            else:
                logger.error("Push failed after 3 attempts for: %s", message)

        return sha

    def read_file(self, relative_path: str) -> str | None:
        """Read a file from the repo. Returns None if not found."""
        full = self._safe_path(relative_path)
        if not os.path.isfile(full):
            return None
        with open(full) as f:
            return f.read()

    def write_artifact(self, domain: str, filename: str, content: str) -> dict:
        """
        Write to artifacts/{domain}/{filename}, commit, push.
        Returns artifact_ref dict: {repo, path, commit, size_lines}
        """
        rel_path = os.path.join("artifacts", domain, filename)
        full_path = self._safe_path(rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)

        with open(full_path, "w") as f:
            f.write(content)

        sha = self._commit_and_push(
            [rel_path],
            f"artifact({domain}): {filename} by {self._agent_name}",
        )
        size_lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)

        return {
            "repo": self._repo_url or self._local_path,
            "path": rel_path,
            "commit": sha,
            "size_lines": size_lines,
        }

    def append_event(self, event_type: str, data: dict) -> None:
        """
        Write a YAML event file to events/YYYY-MM-DD/HH-MM-SS-{agent}-{event}.yaml
        Includes: timestamp, agent, event type, plus all data keys.
        Commit and push.
        """
        now = datetime.now(timezone.utc)
        date_dir = now.strftime("%Y-%m-%d")
        time_part = now.strftime("%H-%M-%S")
        filename = f"{time_part}-{self._agent_name}-{event_type}.yaml"

        rel_path = os.path.join("events", date_dir, filename)
        full_path = self._safe_path(rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)

        event = {
            "timestamp": now.isoformat(),
            "agent": self._agent_name,
            "event_type": event_type,
            **data,
        }

        with open(full_path, "w") as f:
            yaml.dump(event, f, default_flow_style=False, sort_keys=False)

        self._commit_and_push([rel_path], f"event({event_type}): {self._agent_name}")

    def append_budget_log(self, entry: dict) -> None:
        """
        Append a JSON line to budget-logs/{agent_name}/YYYY-MM-DD.jsonl
        Commit and push.
        """
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d")
        rel_path = os.path.join("budget-logs", self._agent_name, f"{date_str}.jsonl")
        full_path = self._safe_path(rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)

        with open(full_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

        self._commit_and_push([rel_path], f"budget-log: {self._agent_name} {date_str}")

    def acquire_lock(self, file_path: str, timeout_seconds: int = 300) -> bool:
        """
        Create .lock/{file_path}.lock with {agent, until} content.
        Returns False if lock exists and not expired.
        Commit and push on success.
        """
        rel_lock = os.path.join(".lock", f"{file_path}.lock")
        full_lock = self._safe_path(rel_lock)

        if os.path.isfile(full_lock):
            with open(full_lock) as f:
                try:
                    lock_data = json.load(f)
                except (json.JSONDecodeError, ValueError):
                    lock_data = {}

            until = lock_data.get("until", "")
            if until:
                try:
                    expiry = datetime.fromisoformat(until)
                    if datetime.now(timezone.utc) < expiry:
                        return False
                except ValueError:
                    pass
            else:
                return False

        os.makedirs(os.path.dirname(full_lock), exist_ok=True)
        now = datetime.now(timezone.utc)
        until = now.timestamp() + timeout_seconds
        until_dt = datetime.fromtimestamp(until, tz=timezone.utc)

        lock_content = {
            "agent": self._agent_name,
            "until": until_dt.isoformat(),
        }
        with open(full_lock, "w") as f:
            json.dump(lock_content, f)

        self._commit_and_push([rel_lock], f"lock: {file_path} by {self._agent_name}")
        return True

    def release_lock(self, file_path: str) -> None:
        """Remove the lock file, commit, push."""
        rel_lock = os.path.join(".lock", f"{file_path}.lock")
        full_lock = self._safe_path(rel_lock)

        if os.path.isfile(full_lock):
            os.remove(full_lock)
            # Stage the removal
            self.repo.index.remove([rel_lock])
            commit = self.repo.index.commit(f"unlock: {file_path} by {self._agent_name}")

            if self._has_remote():
                try:
                    self.repo.remotes.origin.push()
                except git.GitCommandError:
                    logger.warning("Push failed on unlock for %s", file_path)

    def list_events(self, date: str | None = None, agent: str | None = None) -> list[dict]:
        """
        Read events from events/ directory.
        Optional filter by date (YYYY-MM-DD) and agent name.
        Returns list of parsed YAML event dicts.
        """
        events_dir = os.path.join(self._local_path, "events")
        if not os.path.isdir(events_dir):
            return []

        results: list[dict] = []

        for date_folder in sorted(os.listdir(events_dir)):
            if date and date_folder != date:
                continue

            folder_path = os.path.join(events_dir, date_folder)
            if not os.path.isdir(folder_path):
                continue

            for fname in sorted(os.listdir(folder_path)):
                if not fname.endswith(".yaml"):
                    continue

                fpath = os.path.join(folder_path, fname)
                with open(fpath) as f:
                    event = yaml.safe_load(f)

                if agent and event.get("agent") != agent:
                    continue

                results.append(event)

        return results
