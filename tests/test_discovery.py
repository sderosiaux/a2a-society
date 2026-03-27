from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from hive.discovery import DiscoveryClient
from tests.conftest import make_card

REGISTRY = "http://registry:9999"


# -- 1. register with mock registry succeeds ---------------------------------


@pytest.mark.asyncio
async def test_register_posts_full_card():
    card = make_card("alpha", skills=[{"id": "seo", "name": "SEO"}])
    with respx.mock:
        route = respx.post(f"{REGISTRY}/agents/register").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        client = DiscoveryClient(registry_url=REGISTRY)
        try:
            result = await client.register(card)
            assert result is True
            assert route.called
            sent = route.calls[0].request
            import json
            body = json.loads(sent.content)
            assert body["name"] == "alpha"
            assert body["skills"][0]["id"] == "seo"
        finally:
            await client.close()


# -- 2. discover_by_skill returns filtered results ----------------------------


@pytest.mark.asyncio
async def test_discover_by_skill():
    card_a = make_card("alpha", skills=[{"id": "seo", "name": "SEO"}])
    with respx.mock:
        respx.get(f"{REGISTRY}/agents/by-skill/seo").mock(
            return_value=httpx.Response(200, json=[card_a])
        )
        client = DiscoveryClient(registry_url=REGISTRY)
        try:
            results = await client.discover_by_skill("seo")
            assert len(results) == 1
            assert results[0]["name"] == "alpha"
        finally:
            await client.close()


# -- 3. discover_all updates cache; get_cached_peers returns data -------------


@pytest.mark.asyncio
async def test_discover_all_updates_cache():
    cards = [make_card("alpha"), make_card("beta")]
    with respx.mock:
        respx.get(f"{REGISTRY}/agents").mock(
            return_value=httpx.Response(200, json=cards)
        )
        client = DiscoveryClient(registry_url=REGISTRY)
        try:
            results = await client.discover_all()
            assert len(results) == 2
            cached = client.get_cached_peers()
            assert len(cached) == 2
            names = {c["name"] for c in cached}
            assert names == {"alpha", "beta"}
        finally:
            await client.close()


# -- 4. registry down → discover_all falls back to cache ---------------------


@pytest.mark.asyncio
async def test_discover_all_fallback_to_cache():
    cards = [make_card("alpha")]
    with respx.mock:
        # First call succeeds and populates cache.
        respx.get(f"{REGISTRY}/agents").mock(
            side_effect=[
                httpx.Response(200, json=cards),
                httpx.Response(503),
            ]
        )
        client = DiscoveryClient(registry_url=REGISTRY)
        try:
            await client.discover_all()  # populates cache
            # Second call — registry returns 503; should fall back.
            results = await client.discover_all()
            assert len(results) == 1
            assert results[0]["name"] == "alpha"
        finally:
            await client.close()


# -- 5. fetch_peer_cards from static peers ------------------------------------


@pytest.mark.asyncio
async def test_fetch_peer_cards():
    peer_card = make_card("peer-a")
    with respx.mock:
        respx.get("http://peer-a:8462/.well-known/agent.json").mock(
            return_value=httpx.Response(200, json=peer_card)
        )
        client = DiscoveryClient(peers=[{"url": "http://peer-a:8462"}])
        try:
            cards = await client.fetch_peer_cards()
            assert len(cards) == 1
            assert cards[0]["name"] == "peer-a"
            # Also stored in cache.
            assert len(client.get_cached_peers()) == 1
        finally:
            await client.close()


# -- 6. heartbeat fires at least twice in short interval ----------------------


@pytest.mark.asyncio
async def test_heartbeat_fires_multiple_times():
    card = make_card("alpha")
    with respx.mock:
        route = respx.post(f"{REGISTRY}/agents/register").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        client = DiscoveryClient(registry_url=REGISTRY)
        try:
            await client.start_heartbeat(card, interval=0.05)
            # Wait enough for at least 2 heartbeats.
            await asyncio.sleep(0.2)
            await client.stop_heartbeat()
            assert route.call_count >= 2
        finally:
            await client.close()


# -- 7. no registry_url → register returns False gracefully -------------------


@pytest.mark.asyncio
async def test_no_registry_returns_false():
    card = make_card("alpha")
    client = DiscoveryClient()  # no registry_url
    try:
        result = await client.register(card)
        assert result is False
    finally:
        await client.close()
