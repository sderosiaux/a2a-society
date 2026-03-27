from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class DiscoveryClient:
    """Client for registry interaction + local peer cache."""

    def __init__(
        self,
        registry_url: str | None = None,
        peers: list[dict[str, Any]] | None = None,
    ) -> None:
        self._registry_url = registry_url
        self._cache: dict[str, dict[str, Any]] = {}  # name -> agent card
        self._static_peers = peers or []
        self._http = httpx.AsyncClient(timeout=10.0)
        self._heartbeat_task: asyncio.Task[None] | None = None

    # -- registry interactions ------------------------------------------------

    async def register(self, agent_card: dict[str, Any]) -> bool:
        """POST agent card to registry. Returns True on success."""
        if not self._registry_url:
            return False
        try:
            resp = await self._http.post(
                f"{self._registry_url}/agents/register", json=agent_card
            )
            resp.raise_for_status()
            return True
        except (httpx.HTTPError, Exception) as exc:
            logger.warning("Registry register failed: %s", exc)
            return False

    async def discover_all(self) -> list[dict[str, Any]]:
        """GET /agents from registry, update local cache.

        Falls back to cache on failure.
        """
        if not self._registry_url:
            return list(self._cache.values())
        try:
            resp = await self._http.get(f"{self._registry_url}/agents")
            resp.raise_for_status()
            agents: list[dict[str, Any]] = resp.json()
            self._cache = {a["name"]: a for a in agents}
            return agents
        except (httpx.HTTPError, Exception) as exc:
            logger.warning("Registry discover_all failed, using cache: %s", exc)
            return list(self._cache.values())

    async def discover_by_skill(self, skill_id: str) -> list[dict[str, Any]]:
        """GET /agents/by-skill/{skill_id} from registry."""
        if not self._registry_url:
            return []
        try:
            resp = await self._http.get(
                f"{self._registry_url}/agents/by-skill/{skill_id}"
            )
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, Exception) as exc:
            logger.warning("Registry discover_by_skill failed: %s", exc)
            return []

    async def discover_by_role(self, role: str) -> list[dict[str, Any]]:
        """GET /agents/by-role/{role} from registry."""
        if not self._registry_url:
            return []
        try:
            resp = await self._http.get(
                f"{self._registry_url}/agents/by-role/{role}"
            )
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, Exception) as exc:
            logger.warning("Registry discover_by_role failed: %s", exc)
            return []

    # -- heartbeat ------------------------------------------------------------

    async def start_heartbeat(
        self, agent_card: dict[str, Any], interval: int = 60
    ) -> None:
        """Start background task that POSTs register every *interval* seconds."""
        if self._heartbeat_task is not None:
            return
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(agent_card, interval)
        )

    async def _heartbeat_loop(
        self, agent_card: dict[str, Any], interval: int
    ) -> None:
        while True:
            await self.register(agent_card)
            await asyncio.sleep(interval)

    async def stop_heartbeat(self) -> None:
        """Cancel the heartbeat background task."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

    # -- deregister -----------------------------------------------------------

    async def deregister(self, name: str) -> None:
        """Stop heartbeat. (Registry deregister is implicit via TTL expiry.)"""
        await self.stop_heartbeat()
        self._cache.pop(name, None)

    # -- local cache & static peers -------------------------------------------

    def get_cached_peers(self) -> list[dict[str, Any]]:
        """Return locally cached agent cards."""
        return list(self._cache.values())

    async def fetch_peer_cards(self) -> list[dict[str, Any]]:
        """Fetch agent cards from static peer URLs (/.well-known/agent.json).

        Used as fallback when no registry is configured.
        """
        cards: list[dict[str, Any]] = []
        for peer in self._static_peers:
            url = peer.get("url", "")
            if not url:
                continue
            try:
                resp = await self._http.get(f"{url}/.well-known/agent.json")
                resp.raise_for_status()
                card = resp.json()
                cards.append(card)
                self._cache[card.get("name", url)] = card
            except (httpx.HTTPError, Exception) as exc:
                logger.warning("Failed to fetch peer card from %s: %s", url, exc)
        return cards

    # -- lifecycle ------------------------------------------------------------

    async def close(self) -> None:
        """Cleanup: stop heartbeat, close http client."""
        await self.stop_heartbeat()
        await self._http.aclose()
