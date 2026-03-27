from __future__ import annotations

from hive.models import AgentConfig


def build_system_prompt(
    config: AgentConfig, known_peers: list[str] | None = None
) -> str:
    """Build a role-based system prompt from agent configuration."""
    lines: list[str] = []

    lines.append(f"You are the {config.role} of this organization.")

    if config.reports_to:
        lines.append(f"You report to {config.reports_to}.")
    else:
        lines.append("You are the top of the hierarchy.")

    if known_peers:
        lines.append(f"Your direct reports: {', '.join(known_peers)}")

    if config.objectives:
        lines.append("")
        lines.append("Your objectives:")
        for obj in config.objectives:
            lines.append(f"- {obj}")

    lines.append("")
    lines.append("When you receive a task:")
    lines.append("1. Evaluate if you can do it alone with your tools")
    lines.append(
        "2. If you need a skill you don't have, identify which skill is needed"
    )
    lines.append(
        "3. If the decision is above your scope, escalate to your superior"
    )
    lines.append("4. When you delegate, specify clearly what you need")
    lines.append(
        "5. When all subtasks complete, synthesize and respond"
    )

    # Delegation instructions
    lines.append("")
    lines.append(
        "When you need to delegate work to a specialist, include this JSON block"
        " in your response:"
    )
    lines.append(
        '{"delegate": {"skill": "<skill_id>", "message": "<what you need them to do>"}}'
    )
    lines.append("Only include this block when you genuinely need another agent's help.")

    if config.tools:
        lines.append("")
        lines.append(f"Your tools: {', '.join(config.tools)}")

    if config.tools_exclusive:
        lines.append(
            f"Tools you do NOT have (delegate to specialists):"
            f" {', '.join(config.tools_exclusive)}"
        )

    return "\n".join(lines)
