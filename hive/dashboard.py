from __future__ import annotations

import time

import httpx
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table


class HiveDashboard:
    """Live terminal dashboard showing agent status and activity."""

    def __init__(
        self,
        registry_url: str,
        org_memory_path: str | None = None,
        refresh_interval: int = 5,
    ):
        self._registry_url = registry_url
        self._org_memory_path = org_memory_path
        self._refresh_interval = refresh_interval
        self._console = Console()

    def fetch_agents(self) -> list[dict]:
        """Fetch agent cards from registry."""
        try:
            resp = httpx.get(f"{self._registry_url}/agents", timeout=5.0)
            return resp.json()
        except Exception:
            return []

    def build_agent_table(self, agents: list[dict]) -> Table:
        """Build a rich Table of agent status."""
        table = Table(title="Agents", expand=True)
        table.add_column("Name", style="cyan")
        table.add_column("Role", style="white")
        table.add_column("Status", style="bold")
        table.add_column("Budget", justify="right")
        table.add_column("Queue", justify="right")

        for agent in agents:
            hive = agent.get("hive", {})
            name = agent.get("name", "?")
            role = hive.get("role", "?")
            status = hive.get("status", "?")
            budget = hive.get("budget", {})
            remaining = budget.get("remaining_today_usd", 0)
            daily = budget.get("daily_max", 0)

            if status == "active":
                status_str = "[green]active[/green]"
            elif status == "warning":
                status_str = "[yellow]warning[/yellow]"
            elif status == "vacation":
                status_str = "[red]vacation[/red]"
            else:
                status_str = f"[dim]{status}[/dim]"

            budget_str = f"${remaining:.1f}/${daily:.1f}"
            queue_str = str(agent.get("queue_depth", "?"))

            table.add_row(name, role, status_str, budget_str, queue_str)

        return table

    def read_recent_events(self, limit: int = 10) -> list[dict]:
        """Read recent events from org-memory if available."""
        if not self._org_memory_path:
            return []

        from pathlib import Path

        import yaml

        events_dir = Path(self._org_memory_path) / "events"
        if not events_dir.exists():
            return []

        all_events: list[dict] = []
        for date_dir in sorted(events_dir.iterdir(), reverse=True)[:3]:
            if date_dir.is_dir():
                for event_file in sorted(date_dir.iterdir(), reverse=True):
                    if event_file.suffix in (".yaml", ".yml"):
                        try:
                            data = yaml.safe_load(event_file.read_text())
                            if data:
                                all_events.append(data)
                        except Exception:
                            pass

        return sorted(all_events, key=lambda e: e.get("timestamp", ""), reverse=True)[:limit]

    def build_activity_panel(self, events: list[dict]) -> Panel:
        """Build a panel showing recent activity."""
        if not events:
            content = "[dim]No recent activity[/dim]"
        else:
            lines: list[str] = []
            for e in events:
                ts = str(e.get("timestamp", ""))[-8:]
                agent = e.get("agent", "?")
                event_type = e.get("event", "?")
                summary = e.get("summary", "")[:60]
                from_agent = e.get("from", "")

                if from_agent:
                    lines.append(f"[dim]{ts}[/dim]  {agent} <- {from_agent}  {summary}")
                else:
                    lines.append(f"[dim]{ts}[/dim]  {agent}  {event_type}: {summary}")

            content = "\n".join(lines)

        return Panel(content, title="Recent Activity", expand=True)

    def build_footer(self, agents: list[dict]) -> str:
        """Build footer with totals."""
        total_spend = sum(
            a.get("hive", {}).get("budget", {}).get("daily_max", 0)
            - a.get("hive", {}).get("budget", {}).get("remaining_today_usd", 0)
            for a in agents
        )
        active = sum(1 for a in agents if a.get("hive", {}).get("status") == "active")
        vacation = sum(1 for a in agents if a.get("hive", {}).get("status") == "vacation")
        return f"  Org spend today: ${total_spend:.2f}  |  Active: {active}  |  On vacation: {vacation}"

    def render(self) -> Layout:
        """Build the full dashboard layout."""
        agents = self.fetch_agents()
        events = self.read_recent_events()

        layout = Layout()
        layout.split_column(
            Layout(self.build_agent_table(agents), name="agents", ratio=2),
            Layout(self.build_activity_panel(events), name="activity", ratio=1),
            Layout(
                Panel(self.build_footer(agents), style="bold"),
                name="footer",
                size=3,
            ),
        )
        return layout

    def run(self) -> None:
        """Run the live dashboard with auto-refresh."""
        self._console.print("[bold]Hive Dashboard[/bold] -- press Ctrl+C to exit\n")
        try:
            with Live(self.render(), console=self._console, refresh_per_second=0.2) as live:
                while True:
                    time.sleep(self._refresh_interval)
                    live.update(self.render())
        except KeyboardInterrupt:
            self._console.print("\n[dim]Dashboard stopped.[/dim]")
