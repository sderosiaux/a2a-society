from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class ReportGenerator:
    """Generates periodic status reports and sends them to the superior."""

    def __init__(
        self,
        config,           # AgentConfig
        claude_fn,        # async callable: (prompt, system_prompt) -> (text, cost, session_id)
        org_memory=None,  # OrgMemory | None
        client=None,      # A2AClient | None
        discovery=None,   # DiscoveryClient | None
        state_file: str = ".hive_report_state.json",
    ):
        self._config = config
        self._claude_fn = claude_fn
        self._org_memory = org_memory
        self._client = client
        self._discovery = discovery
        self._state_file = Path(state_file)
        self._last_report: datetime | None = self._load_last_report()

    def _load_last_report(self) -> datetime | None:
        """Load last report timestamp from state file."""
        if self._state_file.exists():
            try:
                data = json.loads(self._state_file.read_text())
                return datetime.fromisoformat(data["last_report"])
            except (json.JSONDecodeError, KeyError, ValueError):
                return None
        return None

    def _save_last_report(self, ts: datetime) -> None:
        """Persist last report timestamp."""
        self._state_file.write_text(json.dumps({"last_report": ts.isoformat()}))

    def should_report(self) -> bool:
        """Check if reporting interval has elapsed since last report.

        Frequency from config.reporting.frequency:
        - "hourly"  -> 1h
        - "daily"   -> 24h
        - "weekly"  -> 7d
        Returns False if no reporting config.
        """
        if not self._config.reporting:
            return False

        if self._last_report is None:
            return True

        now = datetime.now(timezone.utc)
        freq = self._config.reporting.frequency
        if freq == "hourly":
            delta = 3600
        elif freq == "daily":
            delta = 86400
        elif freq == "weekly":
            delta = 604800
        else:
            delta = 86400  # default daily

        return (now - self._last_report).total_seconds() >= delta

    async def generate_and_send(self) -> dict | None:
        """Generate a status report and send it to the superior.

        1. Read own recent events from org-memory
        2. Ask Claude to synthesize a status report
        3. Commit report to artifacts/{domain}/reports/YYYY-MM-DD-status.md
        4. Send to reports_to via A2A with artifact_ref
        5. Update last_report timestamp

        Returns artifact_ref dict on success, None on failure.
        """
        if not self._config.reporting:
            return None

        # 1. Gather own activity
        context = f"Generate a status report for your superior ({self._config.reporting.to}).\n\n"

        if self._org_memory:
            self._org_memory.pull()
            events = self._org_memory.list_events(agent=self._config.name)
            recent = events[-20:]
            if recent:
                context += "Recent activity:\n"
                for e in recent:
                    context += f"- [{e.get('timestamp', '')}] {e.get('event', '')} — {e.get('summary', '')}\n"

        context += "\nWrite a concise status report covering: what was accomplished, current priorities, blockers (if any), next steps."

        from hive.prompt_builder import build_system_prompt

        system_prompt = build_system_prompt(self._config)

        try:
            report_text, cost, _ = await self._claude_fn(context, system_prompt)
        except Exception as e:
            logger.error("Report generation failed: %s", e)
            return None

        # 3. Commit to org-memory
        artifact_ref = None
        domain = self._config.role.lower().split()[0] if self._config.role else self._config.name
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        filename = f"reports/{date_str}-status.md"

        if self._org_memory:
            artifact_ref = self._org_memory.write_artifact(domain, filename, report_text)

        # 4. Send to superior
        if self._client and self._discovery and self._config.reporting.to:
            try:
                peers = await self._discovery.discover_by_role(self._config.reporting.to)
                # Also try by name
                if not peers:
                    peers = [
                        p
                        for p in (await self._discovery.discover_all())
                        if p.get("name") == self._config.reporting.to
                    ]

                if peers:
                    summary = report_text[:200] + "..." if len(report_text) > 200 else report_text
                    await self._client.send_task(
                        peer_url=peers[0]["url"],
                        message_text=f"Status report from {self._config.name}:\n\n{summary}",
                        from_agent=self._config.name,
                        artifact_ref=artifact_ref,
                    )
            except Exception as e:
                logger.warning("Failed to send report to superior: %s", e)

        # 5. Update timestamp
        now = datetime.now(timezone.utc)
        self._last_report = now
        self._save_last_report(now)

        return artifact_ref
