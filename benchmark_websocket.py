#!/usr/bin/env python3
"""
WebSocket Latency Benchmark
===========================

Standalone script to measure and report WebSocket latency characteristics.

Usage:
    python benchmark_websocket.py
    python benchmark_websocket.py --markets 20 --duration 60
    python benchmark_websocket.py --verbose

This connects to the real Polymarket WebSocket and measures:
- Connection establishment time
- Time to first message
- Message frequency
- Inter-message latency statistics
- Comparison to HTTP polling
"""

import argparse
import asyncio
import json
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import httpx
import websockets


WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_API_URL = "https://gamma-api.polymarket.com"
CLOB_API_URL = "https://clob.polymarket.com"


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
    def stdev_ms(self) -> Optional[float]:
        return statistics.stdev(self.samples) if len(self.samples) >= 2 else None

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


async def get_active_token_ids(limit: int = 20) -> list[str]:
    """Fetch token IDs from active, high-volume markets."""
    print("Fetching active market token IDs...")

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{GAMMA_API_URL}/markets",
                params={
                    "limit": 100,
                    "closed": "false",
                    "active": "true",
                },
                timeout=15.0,
            )

            if response.status_code != 200:
                print(f"Error: Failed to fetch markets (status {response.status_code})")
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

            print(f"Found {len(token_ids)} token IDs from {len(markets_sorted)} markets")
            return token_ids

    except Exception as e:
        print(f"Error fetching token IDs: {e}")
        return []


async def measure_connection_latency(iterations: int = 5) -> LatencyStats:
    """Measure WebSocket connection establishment time."""
    stats = LatencyStats()

    for i in range(iterations):
        start = datetime.utcnow()

        async with websockets.connect(
            WS_URL,
            ping_interval=30,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            connected = datetime.utcnow()
            latency_ms = (connected - start).total_seconds() * 1000
            stats.add(latency_ms)

    return stats


async def measure_http_latency(token_id: str, iterations: int = 5) -> LatencyStats:
    """Measure HTTP polling latency for a single orderbook."""
    stats = LatencyStats()

    async with httpx.AsyncClient() as client:
        for i in range(iterations):
            start = datetime.utcnow()

            try:
                response = await client.get(
                    f"{CLOB_API_URL}/book",
                    params={"token_id": token_id},
                    timeout=10.0,
                )

                end = datetime.utcnow()
                if response.status_code == 200:
                    stats.add((end - start).total_seconds() * 1000)
            except Exception:
                pass

    return stats


async def run_benchmark(
    num_markets: int = 10,
    duration_seconds: int = 30,
    verbose: bool = False,
) -> dict:
    """Run the complete WebSocket benchmark."""

    results = {
        "connection": {},
        "first_message": {},
        "message_stats": {},
        "http_comparison": {},
    }

    print()
    print("=" * 60)
    print("         POLYMARKET WEBSOCKET LATENCY BENCHMARK")
    print("=" * 60)
    print()

    # Get token IDs
    token_ids = await get_active_token_ids(num_markets)
    if not token_ids:
        print("ERROR: Could not fetch any token IDs")
        return results

    print(f"Will monitor {len(token_ids)} tokens for {duration_seconds} seconds")
    print()

    # 1. Measure connection latency
    print("1. Measuring connection latency...")
    connection_stats = await measure_connection_latency(5)
    results["connection"] = {
        "avg_ms": round(connection_stats.avg_ms, 1) if connection_stats.avg_ms else None,
        "min_ms": round(connection_stats.min_ms, 1) if connection_stats.min_ms else None,
        "max_ms": round(connection_stats.max_ms, 1) if connection_stats.max_ms else None,
    }
    print(f"   Average: {connection_stats.avg_ms:.1f}ms")

    # 2. Measure subscription to first message
    print("\n2. Measuring subscription → first message latency...")

    async with websockets.connect(
        WS_URL,
        ping_interval=30,
        ping_timeout=10,
    ) as ws:
        start = datetime.utcnow()

        subscribe_msg = {
            "type": "MARKET",
            "assets_ids": token_ids,
        }
        await ws.send(json.dumps(subscribe_msg))

        try:
            message = await asyncio.wait_for(ws.recv(), timeout=30.0)
            first_msg_time = datetime.utcnow()
            first_msg_latency = (first_msg_time - start).total_seconds() * 1000

            results["first_message"] = {
                "latency_ms": round(first_msg_latency, 1),
            }
            print(f"   First message: {first_msg_latency:.1f}ms")

        except asyncio.TimeoutError:
            print("   ERROR: No message received in 30 seconds")
            return results

    # 3. Run extended measurement
    print(f"\n3. Running {duration_seconds}s measurement...")

    inter_message_stats = LatencyStats()
    messages_by_type: dict[str, int] = {}
    total_messages = 0

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

        progress_interval = max(1, duration_seconds // 10)
        last_progress = 0

        while True:
            elapsed = (datetime.utcnow() - start_time).total_seconds()

            if elapsed >= duration_seconds:
                break

            # Progress indicator
            if int(elapsed) // progress_interval > last_progress:
                last_progress = int(elapsed) // progress_interval
                print(f"   Progress: {int(elapsed)}/{duration_seconds}s ({total_messages} messages)")

            try:
                message = await asyncio.wait_for(ws.recv(), timeout=2.0)
                now = datetime.utcnow()

                # Track inter-message time
                inter_ms = (now - last_msg_time).total_seconds() * 1000
                inter_message_stats.add(inter_ms)
                last_msg_time = now

                # Track message types
                try:
                    raw_data = json.loads(message)

                    # WebSocket returns a list of events
                    events = raw_data if isinstance(raw_data, list) else [raw_data]

                    for data in events:
                        total_messages += 1
                        event_type = data.get("event_type", "unknown") if isinstance(data, dict) else "unknown"
                        messages_by_type[event_type] = messages_by_type.get(event_type, 0) + 1

                        if verbose and isinstance(data, dict):
                            asset_id = data.get('asset_id', '')
                            print(f"      [{event_type}] {asset_id[:16] if asset_id else 'N/A'}...")

                except json.JSONDecodeError:
                    messages_by_type["invalid"] = messages_by_type.get("invalid", 0) + 1

            except asyncio.TimeoutError:
                continue

    results["message_stats"] = {
        "total_messages": total_messages,
        "messages_per_second": round(total_messages / duration_seconds, 1),
        "inter_message": {
            "min_ms": round(inter_message_stats.min_ms, 1) if inter_message_stats.min_ms else None,
            "avg_ms": round(inter_message_stats.avg_ms, 1) if inter_message_stats.avg_ms else None,
            "median_ms": round(inter_message_stats.median_ms, 1) if inter_message_stats.median_ms else None,
            "p95_ms": round(inter_message_stats.p95_ms, 1) if inter_message_stats.p95_ms else None,
            "p99_ms": round(inter_message_stats.p99_ms, 1) if inter_message_stats.p99_ms else None,
            "max_ms": round(inter_message_stats.max_ms, 1) if inter_message_stats.max_ms else None,
        },
        "by_type": messages_by_type,
    }

    # 4. HTTP comparison
    print("\n4. Measuring HTTP polling latency (for comparison)...")

    if token_ids:
        http_stats = await measure_http_latency(token_ids[0], 5)
        results["http_comparison"] = {
            "single_orderbook_ms": round(http_stats.avg_ms, 1) if http_stats.avg_ms else None,
            "estimated_5000_markets_minutes": round(http_stats.avg_ms * 5000 / 1000 / 60, 1) if http_stats.avg_ms else None,
        }
        print(f"   Single orderbook: {http_stats.avg_ms:.1f}ms")

    # Print final report
    print()
    print("=" * 60)
    print("                      RESULTS")
    print("=" * 60)
    print()

    print("┌────────────────────────────────────────────────────────┐")
    print("│              WebSocket Latency Report                  │")
    print("├────────────────────────────────────────────────────────┤")
    print(f"│ Markets subscribed:     {len(token_ids):<30}│")
    print(f"│ Test duration:          {duration_seconds} seconds{' ' * (23 - len(str(duration_seconds)))}│")
    print(f"│ Total messages:         {total_messages:<30}│")
    print(f"│ Messages/second:        {results['message_stats']['messages_per_second']:<30}│")
    print("├────────────────────────────────────────────────────────┤")
    print(f"│ Connection latency:     {results['connection'].get('avg_ms', 'N/A')} ms{' ' * (25 - len(str(results['connection'].get('avg_ms', 'N/A'))))}│")
    print(f"│ First book snapshot:    {results['first_message'].get('latency_ms', 'N/A')} ms{' ' * (25 - len(str(results['first_message'].get('latency_ms', 'N/A'))))}│")
    print("├────────────────────────────────────────────────────────┤")
    print("│ Inter-message latency:                                 │")

    im = results['message_stats']['inter_message']
    print(f"│   Min:                  {im.get('min_ms', 'N/A')} ms{' ' * (25 - len(str(im.get('min_ms', 'N/A'))))}│")
    print(f"│   Avg:                  {im.get('avg_ms', 'N/A')} ms{' ' * (25 - len(str(im.get('avg_ms', 'N/A'))))}│")
    print(f"│   Median:               {im.get('median_ms', 'N/A')} ms{' ' * (25 - len(str(im.get('median_ms', 'N/A'))))}│")
    print(f"│   P95:                  {im.get('p95_ms', 'N/A')} ms{' ' * (25 - len(str(im.get('p95_ms', 'N/A'))))}│")
    print(f"│   P99:                  {im.get('p99_ms', 'N/A')} ms{' ' * (25 - len(str(im.get('p99_ms', 'N/A'))))}│")
    print(f"│   Max:                  {im.get('max_ms', 'N/A')} ms{' ' * (25 - len(str(im.get('max_ms', 'N/A'))))}│")
    print("├────────────────────────────────────────────────────────┤")
    print("│ Message types received:                                │")

    for msg_type, count in sorted(messages_by_type.items(), key=lambda x: -x[1])[:5]:
        line = f"│   {msg_type}: {count}"
        print(f"{line}{' ' * (57 - len(line))}│")

    print("├────────────────────────────────────────────────────────┤")
    print("│ HTTP Polling Comparison:                               │")

    http_single = results['http_comparison'].get('single_orderbook_ms', 'N/A')
    http_5000 = results['http_comparison'].get('estimated_5000_markets_minutes', 'N/A')

    print(f"│   Single orderbook:     {http_single} ms{' ' * (25 - len(str(http_single)))}│")
    print(f"│   Est. 5000 markets:    {http_5000} minutes{' ' * (20 - len(str(http_5000)))}│")
    print("├────────────────────────────────────────────────────────┤")

    # Calculate improvement factor
    if total_messages > 0 and http_single and http_single != 'N/A':
        ws_time_for_5000 = duration_seconds / total_messages * 5000  # seconds
        http_time_for_5000 = http_5000 * 60 if http_5000 != 'N/A' else 0  # seconds
        if ws_time_for_5000 > 0:
            improvement = http_time_for_5000 / ws_time_for_5000
            print(f"│ IMPROVEMENT: ~{improvement:.0f}x faster with WebSocket{' ' * (18 - len(f'{improvement:.0f}'))}│")

    print("└────────────────────────────────────────────────────────┘")
    print()

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark Polymarket WebSocket latency",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python benchmark_websocket.py                    # Default: 10 markets, 30 seconds
  python benchmark_websocket.py --markets 50       # Monitor 50 tokens
  python benchmark_websocket.py --duration 120     # Run for 2 minutes
  python benchmark_websocket.py -v                 # Verbose output
        """,
    )

    parser.add_argument(
        "--markets", "-m",
        type=int,
        default=10,
        help="Number of market tokens to subscribe to (default: 10)",
    )

    parser.add_argument(
        "--duration", "-d",
        type=int,
        default=30,
        help="Duration of the benchmark in seconds (default: 30)",
    )

    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print each message received",
    )

    args = parser.parse_args()

    try:
        asyncio.run(run_benchmark(
            num_markets=args.markets,
            duration_seconds=args.duration,
            verbose=args.verbose,
        ))
    except KeyboardInterrupt:
        print("\n\nBenchmark interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\nError: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
