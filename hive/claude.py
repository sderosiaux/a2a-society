from __future__ import annotations

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query


async def invoke_claude(
    prompt: str,
    system_prompt: str,
    allowed_tools: list[str] | None = None,
    max_budget_usd: float | None = None,
    cwd: str | None = None,
    session_id: str | None = None,
) -> tuple[str, float, str | None]:
    """Call Claude Code SDK.

    Returns: (response_text, cost_usd, session_id)
    """
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        permission_mode="bypassPermissions",
        max_turns=25,
    )

    if allowed_tools:
        options.allowed_tools = allowed_tools

    if max_budget_usd is not None:
        options.max_budget_usd = max_budget_usd

    if cwd is not None:
        options.cwd = cwd

    if session_id is not None:
        options.continue_conversation = True
        options.resume = session_id

    result_text = ""
    cost_usd = 0.0
    result_session_id: str | None = None

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            result_text = message.result or ""
            cost_usd = message.total_cost_usd or 0.0
            result_session_id = message.session_id

    return result_text, cost_usd, result_session_id
