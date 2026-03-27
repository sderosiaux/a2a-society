from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class RegisteredAgent:
    card: dict
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = "active"


class RegistryStore:
    """In-memory store for agent cards with TTL-based health tracking."""

    def __init__(self, heartbeat_interval: int = 60) -> None:
        self._agents: dict[str, RegisteredAgent] = {}
        self._heartbeat_interval = heartbeat_interval

    def register(self, agent_card: dict) -> None:
        """Upsert an agent card. Sets last_seen to now."""
        name = agent_card["name"]
        if name in self._agents:
            self._agents[name].card = agent_card
            self._agents[name].last_seen = datetime.now(timezone.utc)
            self._agents[name].status = "active"
        else:
            self._agents[name] = RegisteredAgent(card=agent_card)

    def get_all(self) -> list[dict]:
        """Return all agent cards where status != offline."""
        return [
            a.card for a in self._agents.values() if a.status != "offline"
        ]

    def get_by_name(self, name: str) -> dict | None:
        """Return a single agent card by name."""
        entry = self._agents.get(name)
        if entry is None or entry.status == "offline":
            return None
        return entry.card

    def get_by_skill(self, skill_id: str) -> list[dict]:
        """Return agents that have a matching skill id."""
        results: list[dict] = []
        for a in self._agents.values():
            if a.status == "offline":
                continue
            for skill in a.card.get("skills", []):
                if skill.get("id") == skill_id:
                    results.append(a.card)
                    break
        return results

    def get_by_role(self, role: str) -> list[dict]:
        """Return agents with matching role (case-insensitive)."""
        role_lower = role.lower()
        results: list[dict] = []
        for a in self._agents.values():
            if a.status == "offline":
                continue
            agent_role = a.card.get("hive", {}).get("role", "")
            if agent_role.lower() == role_lower:
                results.append(a.card)
        return results

    def check_heartbeats(self) -> list[str]:
        """Mark agents as offline if last_seen > 3 * heartbeat_interval.

        Returns list of names marked offline.
        """
        now = datetime.now(timezone.utc)
        threshold = self._heartbeat_interval * 3
        marked: list[str] = []
        for name, entry in self._agents.items():
            if entry.status == "offline":
                continue
            elapsed = (now - entry.last_seen).total_seconds()
            if elapsed > threshold:
                entry.status = "offline"
                marked.append(name)
        return marked

    def deregister(self, name: str) -> bool:
        """Remove an agent. Returns True if found."""
        return self._agents.pop(name, None) is not None
