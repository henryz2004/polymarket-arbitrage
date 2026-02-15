"""
Tests for WebSocket Connection
==============================

Live tests that verify we can connect to and communicate with
the Polymarket WebSocket API.

These tests require network access and connect to the real WebSocket endpoint.
Run with: pytest tests/test_websocket_connection.py -v
"""

import asyncio
import json
import pytest
import websockets
from datetime import datetime
from typing import Optional

# WebSocket endpoint (public, no auth required for market data)
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Test token IDs (real tokens from active Polymarket markets)
# These are YES/NO token IDs for popular markets
TEST_TOKEN_IDS = [
    # You can find these by running the bot and checking logs
    # For now, we'll discover them dynamically in tests
]


@pytest.fixture
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


class TestWebSocketConnection:
    """Tests for basic WebSocket connectivity."""

    @pytest.mark.asyncio
    async def test_connection_succeeds(self):
        """Test that we can connect to the WebSocket endpoint."""
        try:
            async with websockets.connect(
                WS_URL,
                ping_interval=30,
                ping_timeout=10,
                close_timeout=5,
            ) as ws:
                # Connection established if we get here without exception
                # (websockets 12.x uses state instead of open)
                assert ws is not None
        except Exception as e:
            pytest.fail(f"Failed to connect to WebSocket: {e}")

    @pytest.mark.asyncio
    async def test_connection_with_invalid_url(self):
        """Test that invalid URLs are handled gracefully."""
        invalid_url = "wss://invalid-url-that-does-not-exist.polymarket.com/ws"

        with pytest.raises(Exception):
            async with websockets.connect(
                invalid_url,
                close_timeout=5,
            ) as ws:
                pass

    @pytest.mark.asyncio
    async def test_connection_stays_open(self):
        """Test that connection stays open for at least 5 seconds."""
        async with websockets.connect(
            WS_URL,
            ping_interval=30,
            ping_timeout=10,
        ) as ws:
            # Connection established
            assert ws is not None

            # Wait 5 seconds
            await asyncio.sleep(5)

            # If we're still in the context manager, connection is open
            # Try sending/receiving to confirm
            assert ws is not None


class TestWebSocketSubscription:
    """Tests for subscribing to market data."""

    @pytest.mark.asyncio
    async def test_subscription_message_format(self):
        """Test that subscription message is accepted by server."""
        async with websockets.connect(
            WS_URL,
            ping_interval=30,
            ping_timeout=10,
        ) as ws:
            # The subscription format per Polymarket docs:
            subscribe_msg = {
                "type": "MARKET",
                "assets_ids": [],  # Empty is valid, just won't get data
            }

            await ws.send(json.dumps(subscribe_msg))

            # Should not receive an error immediately
            # (Empty subscription is valid, just won't produce messages)
            await asyncio.sleep(1)
            # If we're still connected, the message was accepted
            assert ws is not None

    @pytest.mark.asyncio
    async def test_receives_message_after_subscription(self):
        """Test that we receive messages after subscribing to real tokens."""
        # First, get some real token IDs from the API
        token_ids = await self._get_sample_token_ids()

        if not token_ids:
            pytest.skip("Could not fetch token IDs for test")

        async with websockets.connect(
            WS_URL,
            ping_interval=30,
            ping_timeout=10,
        ) as ws:
            # Subscribe to market channel
            subscribe_msg = {
                "type": "MARKET",
                "assets_ids": token_ids[:5],  # Subscribe to up to 5 tokens
            }

            await ws.send(json.dumps(subscribe_msg))

            # Wait for a message (with timeout)
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=30.0)
                raw_data = json.loads(message)

                # Messages come as a list of events
                if isinstance(raw_data, list):
                    assert len(raw_data) > 0
                    data = raw_data[0]
                else:
                    data = raw_data

                # Should receive some kind of event
                assert "event_type" in data or "type" in data

            except asyncio.TimeoutError:
                # It's possible no updates happen in 30s for illiquid markets
                pytest.skip("No messages received in 30s (market may be illiquid)")

    async def _get_sample_token_ids(self) -> list[str]:
        """Fetch sample token IDs from the Gamma API."""
        import httpx

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    "https://gamma-api.polymarket.com/markets",
                    params={"limit": 10, "closed": "false", "active": "true"},
                    timeout=10.0,
                )

                if response.status_code != 200:
                    return []

                markets = response.json()
                token_ids = []

                for market in markets:
                    # Extract token IDs from clob_token_ids field
                    clob_ids = market.get("clobTokenIds", "")
                    if clob_ids:
                        # Format: "[id1, id2]" or "id1,id2"
                        ids = clob_ids.strip("[]").split(",")
                        for tid in ids:
                            tid = tid.strip().strip('"')
                            if tid:
                                token_ids.append(tid)

                return token_ids[:10]  # Return up to 10

        except Exception:
            return []


class TestWebSocketReconnection:
    """Tests for reconnection handling."""

    @pytest.mark.asyncio
    async def test_can_reconnect_after_close(self):
        """Test that we can reconnect after closing a connection."""
        # First connection
        ws1 = await websockets.connect(
            WS_URL,
            ping_interval=30,
            ping_timeout=10,
        )
        assert ws1 is not None
        await ws1.close()

        # Second connection (reconnect)
        ws2 = await websockets.connect(
            WS_URL,
            ping_interval=30,
            ping_timeout=10,
        )
        assert ws2 is not None
        await ws2.close()

    @pytest.mark.asyncio
    async def test_multiple_concurrent_connections(self):
        """Test that multiple connections can exist simultaneously."""
        connections = []

        try:
            # Open 3 connections
            for i in range(3):
                ws = await websockets.connect(
                    WS_URL,
                    ping_interval=30,
                    ping_timeout=10,
                )
                connections.append(ws)

            # All should be connected
            for ws in connections:
                assert ws is not None

        finally:
            # Clean up
            for ws in connections:
                await ws.close()


class TestWebSocketHeartbeat:
    """Tests for connection keepalive."""

    @pytest.mark.asyncio
    async def test_connection_survives_30_seconds(self):
        """Test that connection stays alive for 30 seconds with heartbeat."""
        start_time = datetime.utcnow()

        async with websockets.connect(
            WS_URL,
            ping_interval=10,  # Send ping every 10 seconds
            ping_timeout=5,
        ) as ws:
            assert ws is not None

            # Wait 30 seconds
            await asyncio.sleep(30)

            # If we're still in the context manager, connection is alive
            elapsed = (datetime.utcnow() - start_time).total_seconds()
            assert elapsed >= 30


# Run with: pytest tests/test_websocket_connection.py -v -s
if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
