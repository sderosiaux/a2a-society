from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class InitiativeLoop:
    """Periodic loop that wakes the agent to evaluate objectives and act proactively."""

    def __init__(
        self,
        config,           # AgentConfig
        claude_fn,        # async callable: (prompt, system_prompt) -> (text, cost, session_id)
        org_memory=None,  # OrgMemory | None
        client=None,      # A2AClient | None
        discovery=None,   # DiscoveryClient | None
        budget=None,      # BudgetManager | None
        queue=None,       # TaskQueue | None
    ):
        self._config = config
        self._claude_fn = claude_fn
        self._org_memory = org_memory
        self._client = client
        self._discovery = discovery
        self._budget = budget
        self._queue = queue
        self._interval = config.initiative_interval_minutes * 60  # seconds
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the initiative loop as a background asyncio task."""
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._interval)
            await self.tick()

    async def tick(self) -> dict | None:
        """Single initiative evaluation. Can be called directly for testing.

        Returns the decision dict from Claude, or None if skipped.
        """
        # 1. Skip if budget is warning or vacation
        if self._budget and self._budget.status.value in ("warning", "vacation"):
            logger.info("Initiative loop skipped: budget %s", self._budget.status.value)
            return None

        # 2. Gather context
        context_parts = []
        context_parts.append(
            "Your objectives:\n" + "\n".join(f"- {o}" for o in self._config.objectives)
        )

        if self._org_memory:
            try:
                self._org_memory.pull()
                events = self._org_memory.list_events(agent=self._config.name)
                recent = events[-10:]  # last 10 events
                if recent:
                    context_parts.append(
                        "Recent activity:\n"
                        + "\n".join(
                            f"- [{e.get('timestamp', '')}] {e.get('event', '')} — {e.get('summary', '')}"
                            for e in recent
                        )
                    )
            except Exception as e:
                logger.warning("Failed to read org-memory: %s", e)

        # 3. Ask Claude to evaluate
        eval_prompt = (
            "\n\n".join(context_parts)
            + """

Evaluate your progress on your objectives. Decide ONE action:
- "nothing" — nothing to do right now
- "self_task" — create a task for yourself (specify: description)
- "delegate" — send a task to a peer (specify: skill_needed, message)
- "report" — send a status report to your superior

Respond with JSON only:
{"decision": "nothing|self_task|delegate|report", "description": "...", "skill_needed": "...", "message": "..."}
"""
        )

        from hive.prompt_builder import build_system_prompt

        system_prompt = build_system_prompt(self._config)

        try:
            text, cost, _ = await self._claude_fn(eval_prompt, system_prompt)
            if self._budget:
                self._budget.record_cost(cost)
        except Exception as e:
            logger.error("Initiative loop Claude call failed: %s", e)
            return None

        # 4. Parse decision
        try:
            decision = json.loads(text.strip())
        except json.JSONDecodeError:
            # Try to extract JSON from response
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                decision = json.loads(match.group())
            else:
                logger.warning("Could not parse initiative decision: %s", text[:200])
                return None

        # 5. Execute decision
        action = decision.get("decision", "nothing")

        if action == "nothing":
            logger.info("Initiative: nothing to do")

        elif action == "self_task" and self._queue:
            description = decision.get("description", "Self-assigned task")
            await self._queue.enqueue(
                task_id=f"initiative-{datetime.now(timezone.utc).strftime('%H%M%S')}",
                message_text=description,
                metadata={"from_agent": self._config.name, "priority": "normal"},
            )
            logger.info("Initiative: self-assigned task: %s", description)

        elif action == "delegate" and self._client and self._discovery:
            skill = decision.get("skill_needed", "")
            msg = decision.get("message", "")
            if skill:
                peers = await self._discovery.discover_by_skill(skill)
                if peers:
                    peer = peers[0]
                    try:
                        await self._client.send_task(
                            peer_url=peer["url"],
                            message_text=msg,
                            from_agent=self._config.name,
                        )
                        logger.info("Initiative: delegated to %s: %s", peer["name"], msg)
                    except Exception as e:
                        logger.warning("Initiative delegation failed: %s", e)

        elif action == "report":
            logger.info("Initiative: reporting triggered (handled by reporting module)")
            # Actual report generation handled by Task 7.2 reporting.py

        return decision
