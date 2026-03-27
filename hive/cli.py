from __future__ import annotations

import click


@click.group()
def cli():
    """Hive -- Distributed Agent Society over A2A"""


@cli.command()
@click.option("--role", required=True, help="Agent role in the organization")
@click.option("--name", default=None, help="Agent name (defaults to slugified role)")
@click.option("--reports-to", default=None, help="Name of superior agent")
@click.option("--skills", default="", help="Comma-separated skill list (id:name pairs or just ids)")
@click.option("--tools", default="", help="Comma-separated tool names")
@click.option("--objectives", default="", help="Comma-separated objectives")
@click.option("--knowledge", default=None, type=click.Path(exists=True), help="Path to knowledge directory")
@click.option("--report-frequency", default="weekly", help="Reporting frequency: hourly, daily, weekly")
@click.option("--budget-daily", default=5.0, type=float, help="Daily budget in USD")
@click.option("--budget-weekly", default=25.0, type=float, help="Weekly budget in USD")
@click.option("--initiative-interval", default=30, type=int, help="Initiative loop interval in minutes")
@click.option("--registry", default=None, help="Registry URL")
@click.option("--org-memory", default=None, help="Org memory git repo URL")
@click.option("--port", default=8462, type=int, help="HTTP port")
@click.option("--host", default="127.0.0.1", help="HTTP host")
@click.option("--auth-token", default=None, help="Bearer token for authentication")
@click.option("--config", default=None, type=click.Path(exists=True), help="Path to agent-config.yaml (overrides all other flags)")
def join(role, name, reports_to, skills, tools, objectives, knowledge, report_frequency,
         budget_daily, budget_weekly, initiative_interval, registry, org_memory, port, host, auth_token, config):
    """Join the Hive network as an agent."""

    if config:
        from hive.config import load_config

        agent_config = load_config(config)
    else:
        from hive.models import AgentConfig, BudgetConfig, ReportingConfig, SkillDef

        agent_name = name or role.lower().replace(" ", "-")

        skill_list: list[SkillDef] = []
        if skills:
            for s in skills.split(","):
                s = s.strip()
                if ":" in s:
                    sid, sname = s.split(":", 1)
                    skill_list.append(SkillDef(id=sid.strip(), name=sname.strip()))
                else:
                    skill_list.append(SkillDef(id=s, name=s))

        tool_list = [t.strip() for t in tools.split(",") if t.strip()] if tools else []
        obj_list = [o.strip() for o in objectives.split(",") if o.strip()] if objectives else []

        reporting = ReportingConfig(to=reports_to, frequency=report_frequency) if reports_to else None

        agent_config = AgentConfig(
            name=agent_name,
            role=role,
            reports_to=reports_to,
            skills=skill_list,
            tools=tool_list,
            objectives=obj_list,
            knowledge_dir=knowledge,
            reporting=reporting,
            budget=BudgetConfig(daily_max_usd=budget_daily, weekly_max_usd=budget_weekly),
            initiative_interval_minutes=initiative_interval,
            registry_url=registry,
            org_memory_url=org_memory,
            host=host,
            port=port,
            auth_token=auth_token,
        )

    click.echo(f"Joining Hive as '{agent_config.name}' ({agent_config.role})")
    click.echo(f"  Reports to: {agent_config.reports_to or 'nobody (top of hierarchy)'}")
    click.echo(f"  Skills: {[s.id for s in agent_config.skills]}")
    click.echo(f"  Budget: ${agent_config.budget.daily_max_usd}/day")
    click.echo(f"  Port: {agent_config.port}")

    from hive.server import run_server

    run_server(agent_config)


@cli.command()
@click.option("--port", default=8462, type=int, help="Agent HTTP port")
@click.option("--auth-token", default=None, help="Bearer token for the agent")
@click.option("--graceful/--force", default=True, help="Graceful shutdown (finish current task)")
def leave(port, auth_token, graceful):
    """Leave the Hive network by sending a shutdown request to the running agent."""
    import httpx

    url = f"http://localhost:{port}/admin/shutdown"
    headers = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    if graceful:
        click.echo(f"Gracefully leaving the Hive network (port {port})...")
    else:
        click.echo(f"Force leaving the Hive network (port {port})...")

    try:
        resp = httpx.post(url, headers=headers, timeout=10.0)
        if resp.status_code == 200:
            click.echo("Shutdown request accepted. Agent is leaving.")
        else:
            click.echo(f"Shutdown request failed: HTTP {resp.status_code}", err=True)
            raise SystemExit(1)
    except httpx.ConnectError:
        click.echo(f"Cannot connect to agent on port {port}. Is it running?", err=True)
        raise SystemExit(1) from None
    except Exception as e:
        click.echo(f"Failed to send shutdown request: {e}", err=True)
        raise SystemExit(1) from None


@cli.command(name="registry")
@click.option("--port", default=8080, type=int, help="Registry port")
@click.option("--host", default="127.0.0.1", help="Registry host")
@click.option("--auth-token", default=None, help="Bearer token for authentication")
def start_registry(port, host, auth_token):
    """Start the Hive registry server."""
    import uvicorn

    from hive.registry.server import create_registry_app

    click.echo(f"Starting Hive registry on {host}:{port}")
    app = create_registry_app(auth_token=auth_token)
    uvicorn.run(app, host=host, port=port)


@cli.command()
@click.option("--registry", required=True, help="Registry URL")
def status(registry):
    """Show status of all agents in the network."""
    import httpx

    try:
        resp = httpx.get(f"{registry}/agents", timeout=5.0)
        agents = resp.json()
    except Exception as e:
        click.echo(f"Failed to reach registry: {e}", err=True)
        raise SystemExit(1) from None

    if not agents:
        click.echo("No agents registered.")
        return

    click.echo(f"{'NAME':<20} {'ROLE':<20} {'STATUS':<10} {'BUDGET':<15}")
    click.echo("-" * 65)
    for agent in agents:
        name = agent.get("name", "?")
        hive = agent.get("hive", {})
        role = hive.get("role", "?")
        agent_status = hive.get("status", "?")
        budget = hive.get("budget", {})
        remaining = budget.get("remaining_today_usd", "?")
        daily = budget.get("daily_max", "?")
        budget_str = f"${remaining}/${daily}" if remaining != "?" else "?"
        click.echo(f"{name:<20} {role:<20} {agent_status:<10} {budget_str:<15}")


@cli.command()
@click.option("--registry", required=True, help="Registry URL")
@click.option("--org-memory", default=None, help="Path to local org-memory repo")
@click.option("--refresh", default=5, type=int, help="Refresh interval in seconds")
def dashboard(registry, org_memory, refresh):
    """Live dashboard showing all agents and activity."""
    from hive.dashboard import HiveDashboard

    dash = HiveDashboard(registry, org_memory, refresh)
    dash.run()


if __name__ == "__main__":
    cli()
