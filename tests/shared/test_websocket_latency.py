"""
Tests for WebSocket Latency
===========================

Live tests that measure latency characteristics of the WebSocket connection.
These tests connect to the real Polymarket WebSocket.

Run with: pytest tests/test_websocket_latency.py -v -s
"""

import asyncio
import json
import statistics
import pytest
import websockets
import httpx
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional


WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_API_URL = "https://gamma-api.polymarket.com"


@dataclass
class LatencyStats:
    """Container for latency statistics."""
    samples: list[float] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.samples)

    @property
    def min_ms(self) -> Optional[float]:
        return min(self.samples) if self.samples else None

    @property
    def max_ms(self) -> Optional[float]:
        return max(self.samples) if self.samples else None

    @property
    def avg_ms(self) -> Optional[float]:
        return statistics.mean(self.samples) if self.samples else None

    @property
    def median_ms(self) -> Optional[float]:
        return statistics.median(self.samples) if self.samples else None

    @property
    def p95_ms(self) -> Optional[float]:
        if len(self.samples) < 20:
            return self.max_ms
        sorted_samples = sorted(self.samples)
        idx = int(len(sorted_samples) * 0.95)
        return sorted_samples[idx]

    @property
    def p99_ms(self) -> Optional[float]:
        if len(self.samples) < 100:
            return self.max_ms
        sorted_samples = sorted(self.samples)
        idx = int(len(sorted_samples) * 0.99)
        return sorted_samples[idx]

    def add(self, latency_ms: float) -> None:
        self.samples.append(latency_ms)

    def summary(self) -> dict:
        return {
            "count": self.count,
            "min_ms": round(self.min_ms, 2) if self.min_ms else None,
            "max_ms": round(self.max_ms, 2) if self.max_ms else None,
            "avg_ms": round(self.avg_ms, 2) if self.avg_ms else None,
            "median_ms": round(self.median_ms, 2) if self.median_ms else None,
            "p95_ms": round(self.p95_ms, 2) if self.p95_ms else None,
            "p99_ms": round(self.p99_ms, 2) if self.p99_ms else None,
        }


async def get_active_token_ids(limit: int = 10) -> list[str]:
    """Fetch token IDs from active, high-volume markets."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{GAMMA_API_URL}/markets",
                params={
                    "limit": 50,
                    "closed": "false",
                    "active": "true",
                },
                timeout=15.0,
            )

            if response.status_code != 200:
                return []

            markets = response.json()
            token_ids = []

            # Sort by volume to get most active markets
            markets_sorted = sorted(
                markets,
                key=lambda m: float(m.get("volume24hr", 0) or 0),
                reverse=True,
            )

            for market in markets_sorted:
                clob_ids = market.get("clobTokenIds", "")
                if clob_ids:
                    ids = clob_ids.strip("[]").split(",")
                    for tid in ids:
                        tid = tid.strip().strip('"')
                        if tid and len(token_ids) < limit:
                            token_ids.append(tid)

            return token_ids

    except Exception as e:
        print(f"Error fetching token IDs: {e}")
        return []


@pytest.fixture
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


class TestConnectionLatency:
    """Tests for connection establishment latency."""

    @pytest.mark.asyncio
    async def test_measure_connection_time(self):
        """Measure time to establish WebSocket connection."""
        connection_times = LatencyStats()

        for _ in range(5):
            start = datetime.utcnow()

            async with websockets.connect(
                WS_URL,
                ping_interval=30,
                ping_timeout=10,
            ) as ws:
                connected = datetime.utcnow()
                latency_ms = (connected - start).total_seconds() * 1000
                connection_times.add(latency_ms)

        print(f"\n📡 Connection Latency Stats:")
        print(f"   Samples: {connection_times.count}")
        print(f"   Min:     {connection_times.min_ms:.1f}ms")
        print(f"   Avg:     {connection_times.avg_ms:.1f}ms")
        print(f"   Max:     {connection_times.max_ms:.1f}ms")

        # Connection should typically be under 500ms
        assert connection_times.avg_ms < 1000, "Connection too slow"

    @pytest.mark.asyncio
    async def test_measure_subscription_to_first_message(self):
        """Measure time from subscription to first message."""
        token_ids = await get_active_token_ids(5)

        if not token_ids:
            pytest.skip("Could not fetch token IDs")

        async with websockets.connect(
            WS_URL,
            ping_interval=30,
            ping_timeout=10,
        ) as ws:
            # Send subscription
            start = datetime.utcnow()

            subscribe_msg = {
                "type": "MARKET",
                "assets_ids": token_ids,
            }
            await ws.send(json.dumps(subscribe_msg))

            # Wait for first message
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=30.0)
                received = datetime.utcnow()

                latency_ms = (received - start).total_seconds() * 1000

                print(f"\n⏱️  Subscription → First Message:")
                print(f"   Latency: {latency_ms:.1f}ms")

                # Should receive initial book snapshot quickly
                assert latency_ms < 5000, "First message took too long"

            except asyncio.TimeoutError:
                pytest.skip("No message received in 30s")


class TestMessageLatency:
    """Tests for message processing latency."""

    @pytest.mark.asyncio
    async def test_measure_message_processing_time(self):
        """Measure time to parse and process messages."""
        processing_times = LatencyStats()

        # Simulate messages
        sample_messages = [
            json.dumps({
                "event_type": "book",
                "asset_id": "123",
                "market": "0xabc",
                "timestamp": 1707500000000,
                "buys": [{"price": "0.48", "size": "100"}] * 10,
                "sells": [{"price": "0.52", "size": "100"}] * 10,
            })
            for _ in range(1000)
        ]

        for raw_msg in sample_messages:
            start = datetime.utcnow()

            # Parse message
            data = json.loads(raw_msg)

            # Extract values (simulating conversion)
            _ = data.get("event_type")
            _ = [float(b["price"]) for b in data.get("buys", [])]
            _ = [float(s["price"]) for s in data.get("sells", [])]

            end = datetime.utcnow()
            latency_ms = (end - start).total_seconds() * 1000
            processing_times.add(latency_ms)

        print(f"\n🔄 Message Processing Latency:")
        print(f"   Samples: {processing_times.count}")
        print(f"   Min:     {processing_times.min_ms:.3f}ms")
        print(f"   Avg:     {processing_times.avg_ms:.3f}ms")
        print(f"   P95:     {processing_times.p95_ms:.3f}ms")
        print(f"   Max:     {processing_times.max_ms:.3f}ms")

        # Processing should be very fast (<5ms average)
        assert processing_times.avg_ms < 5, "Message processing too slow"


class TestUpdateFrequency:
    """Tests for message update frequency."""

    @pytest.mark.asyncio
    async def test_measure_update_frequency(self):
        """Measure how often we receive updates (10 second sample)."""
        token_ids = await get_active_token_ids(10)

        if not token_ids:
            pytest.skip("Could not fetch token IDs")

        message_count = 0
        inter_message_times = LatencyStats()
        last_message_time = None

        async with websockets.connect(
            WS_URL,
            ping_interval=30,
            ping_timeout=10,
        ) as ws:
            # Subscribe
            subscribe_msg = {
                "type": "MARKET",
                "assets_ids": token_ids,
            }
            await ws.send(json.dumps(subscribe_msg))

            start_time = datetime.utcnow()
            sample_duration = 10  # seconds

            print(f"\n📊 Measuring update frequency for {sample_duration}s...")

            while True:
                elapsed = (datetime.utcnow() - start_time).total_seconds()
                if elapsed >= sample_duration:
                    break

                try:
                    # Wait for message with short timeout
                    await asyncio.wait_for(ws.recv(), timeout=1.0)
                    message_count += 1

                    now = datetime.utcnow()
                    if last_message_time:
                        inter_ms = (now - last_message_time).total_seconds() * 1000
                        inter_message_times.add(inter_ms)
                    last_message_time = now

                except asyncio.TimeoutError:
                    continue

            messages_per_second = message_count / sample_duration

            print(f"\n📈 Update Frequency Results:")
            print(f"   Duration:        {sample_duration}s")
            print(f"   Total messages:  {message_count}")
            print(f"   Messages/sec:    {messages_per_second:.1f}")

            if inter_message_times.count > 0:
                print(f"   Avg interval:    {inter_message_times.avg_ms:.1f}ms")
                print(f"   Min interval:    {inter_message_times.min_ms:.1f}ms")

            # We should receive at least some messages
            assert message_count > 0, "No messages received"


class TestCompareToHTTP:
    """Tests comparing WebSocket to HTTP polling."""

    @pytest.mark.asyncio
    async def test_compare_ws_vs_http(self):
        """Compare update speed: WebSocket vs HTTP polling."""
        token_ids = await get_active_token_ids(3)

        if not token_ids:
            pytest.skip("Could not fetch token IDs")

        # Measure HTTP polling time for one orderbook
        http_times = LatencyStats()

        async with httpx.AsyncClient() as client:
            for _ in range(5):
                start = datetime.utcnow()

                response = await client.get(
                    f"https://clob.polymarket.com/book",
                    params={"token_id": token_ids[0]},
                    timeout=10.0,
                )

                end = datetime.utcnow()
                if response.status_code == 200:
                    http_times.add((end - start).total_seconds() * 1000)

        # Measure WebSocket time to first book update
        ws_times = LatencyStats()

        for _ in range(3):
            async with websockets.connect(
                WS_URL,
                ping_interval=30,
                ping_timeout=10,
            ) as ws:
                start = datetime.utcnow()

                subscribe_msg = {
                    "type": "MARKET",
                    "assets_ids": [token_ids[0]],
                }
                await ws.send(json.dumps(subscribe_msg))

                try:
                    await asyncio.wait_for(ws.recv(), timeout=10.0)
                    end = datetime.utcnow()
                    ws_times.add((end - start).total_seconds() * 1000)
                except asyncio.TimeoutError:
                    pass

        print(f"\n🔄 HTTP vs WebSocket Comparison:")
        print(f"\n   HTTP Polling (single orderbook):")
        if http_times.count > 0:
            print(f"   - Avg latency: {http_times.avg_ms:.1f}ms")
            print(f"   - For 5000 markets: ~{http_times.avg_ms * 5000 / 1000 / 60:.1f} minutes")

        print(f"\n   WebSocket (subscription → first update):")
        if ws_times.count > 0:
            print(f"   - Avg latency: {ws_times.avg_ms:.1f}ms")
            print(f"   - Subsequent updates: instant (pushed)")

        if http_times.count > 0 and ws_times.count > 0:
            # After initial subscription, WS is effectively instant
            # vs HTTP which must poll each market
            improvement = (http_times.avg_ms * 5000) / ws_times.avg_ms
            print(f"\n   📈 Potential improvement: ~{improvement:.0f}x faster for 5000 markets")


class TestLatencyStatistics:
    """Extended latency statistics test."""

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_extended_latency_measurement(self):
        """Run extended test (30 seconds) for detailed statistics."""
        token_ids = await get_active_token_ids(20)

        if not token_ids:
            pytest.skip("Could not fetch token IDs")

        message_stats = LatencyStats()
        messages_by_type: dict[str, int] = {}

        async with websockets.connect(
            WS_URL,
            ping_interval=30,
            ping_timeout=10,
        ) as ws:
            # Subscribe
            subscribe_msg = {
                "type": "MARKET",
                "assets_ids": token_ids,
            }
            await ws.send(json.dumps(subscribe_msg))

            start_time = datetime.utcnow()
            last_msg_time = start_time
            sample_duration = 30  # seconds

            print(f"\n🔬 Extended Latency Test ({sample_duration}s)...")

            while True:
                elapsed = (datetime.utcnow() - start_time).total_seconds()
                if elapsed >= sample_duration:
                    break

                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    now = datetime.utcnow()

                    # Track inter-message time
                    inter_ms = (now - last_msg_time).total_seconds() * 1000
                    message_stats.add(inter_ms)
                    last_msg_time = now

                    # Track message types
                    try:
                        raw_data = json.loads(message)

                        # Messages come as a list
                        events = raw_data if isinstance(raw_data, list) else [raw_data]

                        for data in events:
                            event_type = data.get("event_type", "unknown") if isinstance(data, dict) else "unknown"
                            messages_by_type[event_type] = messages_by_type.get(event_type, 0) + 1
                    except json.JSONDecodeError:
                        messages_by_type["invalid"] = messages_by_type.get("invalid", 0) + 1

                except asyncio.TimeoutError:
                    continue

            print(f"\n📊 Extended Latency Report:")
            print(f"   Duration:        {sample_duration}s")
            print(f"   Markets watched: {len(token_ids)}")
            print(f"   Total messages:  {message_stats.count}")
            print(f"   Messages/sec:    {message_stats.count / sample_duration:.1f}")
            print(f"\n   Inter-message latency:")
            print(f"   - Min:    {message_stats.min_ms:.1f}ms")
            print(f"   - Avg:    {message_stats.avg_ms:.1f}ms")
            print(f"   - Median: {message_stats.median_ms:.1f}ms")
            print(f"   - P95:    {message_stats.p95_ms:.1f}ms")
            print(f"   - P99:    {message_stats.p99_ms:.1f}ms")
            print(f"   - Max:    {message_stats.max_ms:.1f}ms")
            print(f"\n   Message types received:")
            for msg_type, count in sorted(messages_by_type.items(), key=lambda x: -x[1]):
                print(f"   - {msg_type}: {count}")


# Run with: pytest tests/test_websocket_latency.py -v -s
# For extended test: pytest tests/test_websocket_latency.py -v -s -m slow
if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
