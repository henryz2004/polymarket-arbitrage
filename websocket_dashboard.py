#!/usr/bin/env python3
"""
WebSocket Live Dashboard
========================

Real-time visualization of Polymarket WebSocket data flow.

Usage:
    python websocket_dashboard.py [--markets N]

Example:
    python websocket_dashboard.py --markets 5
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

import httpx
import websockets

# WebSocket endpoint
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_API_URL = "https://gamma-api.polymarket.com"

# ANSI color codes
class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    # Colors
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"

    # Background
    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_BLUE = "\033[44m"


@dataclass
class OrderBookSnapshot:
    """Snapshot of an order book."""
    asset_id: str
    market_name: str
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    spread: Optional[float] = None
    last_update: Optional[datetime] = None


@dataclass
class DashboardStats:
    """Dashboard statistics."""
    connected: bool = False
    connect_time: Optional[datetime] = None
    message_count: int = 0
    book_updates: int = 0
    price_changes: int = 0
    errors: int = 0
    last_message_time: Optional[datetime] = None
    messages_per_second: float = 0.0

    # Latency tracking
    latencies: list[float] = field(default_factory=list)

    @property
    def avg_latency_ms(self) -> Optional[float]:
        if not self.latencies:
            return None
        return sum(self.latencies[-100:]) / len(self.latencies[-100:])

    @property
    def uptime_seconds(self) -> float:
        if not self.connect_time:
            return 0.0
        return (datetime.utcnow() - self.connect_time).total_seconds()


def clear_screen():
    """Clear terminal screen."""
    os.system('cls' if os.name == 'nt' else 'clear')


def move_cursor(row: int, col: int):
    """Move cursor to position."""
    print(f"\033[{row};{col}H", end="")


def draw_box(row: int, col: int, width: int, height: int, title: str = ""):
    """Draw a box with optional title."""
    # Top border
    move_cursor(row, col)
    print(f"{'─' * width}")

    # Title
    if title:
        move_cursor(row, col + 2)
        print(f" {title} ")

    # Sides
    for i in range(1, height - 1):
        move_cursor(row + i, col)
        print("│")
        move_cursor(row + i, col + width - 1)
        print("│")

    # Bottom border
    move_cursor(row + height - 1, col)
    print(f"{'─' * width}")


async def get_active_markets(limit: int = 10) -> list[dict]:
    """Fetch active markets from Gamma API."""
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

            # Sort by volume
            markets_sorted = sorted(
                markets,
                key=lambda m: float(m.get("volume24hr", 0) or 0),
                reverse=True,
            )

            result = []
            for market in markets_sorted[:limit]:
                clob_ids = market.get("clobTokenIds", "")
                if clob_ids:
                    ids = clob_ids.strip("[]").split(",")
                    for tid in ids:
                        tid = tid.strip().strip('"')
                        if tid:
                            result.append({
                                "token_id": tid,
                                "question": market.get("question", "Unknown")[:50],
                                "volume": float(market.get("volume24hr", 0) or 0),
                            })
                            break  # Just take first token per market

                if len(result) >= limit:
                    break

            return result

    except Exception as e:
        print(f"Error fetching markets: {e}")
        return []


def render_header(stats: DashboardStats):
    """Render dashboard header."""
    move_cursor(1, 1)
    print(f"{Colors.BOLD}{Colors.CYAN}{'═' * 80}{Colors.RESET}")

    move_cursor(2, 1)
    title = "POLYMARKET WEBSOCKET LIVE DASHBOARD"
    padding = (80 - len(title)) // 2
    print(f"{' ' * padding}{Colors.BOLD}{Colors.WHITE}{title}{Colors.RESET}")

    move_cursor(3, 1)
    print(f"{Colors.CYAN}{'═' * 80}{Colors.RESET}")


def render_connection_status(stats: DashboardStats):
    """Render connection status panel."""
    move_cursor(5, 1)
    print(f"{Colors.BOLD}CONNECTION STATUS{Colors.RESET}")
    print(f"{'─' * 40}")

    if stats.connected:
        status = f"{Colors.GREEN}● CONNECTED{Colors.RESET}"
    else:
        status = f"{Colors.RED}○ DISCONNECTED{Colors.RESET}"

    print(f"  Status:    {status}")
    print(f"  Uptime:    {Colors.YELLOW}{stats.uptime_seconds:.1f}s{Colors.RESET}")
    print(f"  Endpoint:  {Colors.DIM}ws-subscriptions-clob.polymarket.com{Colors.RESET}")


def render_statistics(stats: DashboardStats):
    """Render message statistics panel."""
    move_cursor(5, 42)
    print(f"{Colors.BOLD}MESSAGE STATISTICS{Colors.RESET}")
    move_cursor(6, 42)
    print(f"{'─' * 38}")

    move_cursor(7, 42)
    print(f"  Messages:     {Colors.GREEN}{stats.message_count:,}{Colors.RESET}")
    move_cursor(8, 42)
    print(f"  Book Updates: {Colors.BLUE}{stats.book_updates:,}{Colors.RESET}")
    move_cursor(9, 42)
    print(f"  Price Changes:{Colors.MAGENTA}{stats.price_changes:,}{Colors.RESET}")
    move_cursor(10, 42)
    print(f"  Msgs/sec:     {Colors.YELLOW}{stats.messages_per_second:.1f}{Colors.RESET}")

    if stats.avg_latency_ms:
        move_cursor(11, 42)
        print(f"  Avg Latency:  {Colors.CYAN}{stats.avg_latency_ms:.1f}ms{Colors.RESET}")


def render_order_books(order_books: dict[str, OrderBookSnapshot], start_row: int = 13):
    """Render order book snapshots."""
    move_cursor(start_row, 1)
    print(f"{Colors.BOLD}LIVE ORDER BOOKS{Colors.RESET}")
    print(f"{'─' * 80}")

    # Header
    print(f"  {'Market':<35} {'Bid':>8} {'Ask':>8} {'Spread':>8} {'Age':>6}")
    print(f"  {'-' * 35} {'-' * 8} {'-' * 8} {'-' * 8} {'-' * 6}")

    row = start_row + 4
    for asset_id, book in list(order_books.items())[:8]:
        move_cursor(row, 1)

        # Truncate market name
        name = book.market_name[:33] + ".." if len(book.market_name) > 35 else book.market_name

        # Format prices in cents (more useful for prediction markets)
        bid_str = f"{book.best_bid*100:6.1f}c" if book.best_bid else "    --"
        ask_str = f"{book.best_ask*100:6.1f}c" if book.best_ask else "    --"

        # Spread with color
        if book.spread is not None:
            if book.spread < 0.02:
                spread_color = Colors.GREEN
            elif book.spread < 0.05:
                spread_color = Colors.YELLOW
            else:
                spread_color = Colors.RED
            spread_str = f"{spread_color}{book.spread:.1%}{Colors.RESET}"
        else:
            spread_str = "  --"

        # Time since update
        if book.last_update:
            age = (datetime.utcnow() - book.last_update).total_seconds()
            if age < 1:
                age_str = f"{Colors.GREEN}<1s{Colors.RESET}"
            elif age < 5:
                age_str = f"{Colors.YELLOW}{age:.0f}s{Colors.RESET}"
            else:
                age_str = f"{Colors.RED}{age:.0f}s{Colors.RESET}"
        else:
            age_str = "  --"

        print(f"  {name:<35} {Colors.GREEN}{bid_str:>8}{Colors.RESET} {Colors.RED}{ask_str:>8}{Colors.RESET} {spread_str:>16} {age_str:>10}")
        row += 1

    # Pad remaining rows
    while row < start_row + 12:
        move_cursor(row, 1)
        print(" " * 80)
        row += 1


def render_live_feed(messages: list[str], start_row: int = 26):
    """Render live message feed."""
    move_cursor(start_row, 1)
    print(f"{Colors.BOLD}LIVE MESSAGE FEED{Colors.RESET}")
    print(f"{'─' * 80}")

    row = start_row + 2
    for msg in messages[-8:]:  # Show last 8 messages
        move_cursor(row, 1)
        # Truncate message
        if len(msg) > 78:
            msg = msg[:75] + "..."
        print(f"  {msg:<78}")
        row += 1

    # Pad remaining rows
    while row < start_row + 10:
        move_cursor(row, 1)
        print(" " * 80)
        row += 1


def render_footer():
    """Render dashboard footer."""
    move_cursor(37, 1)
    print(f"{Colors.CYAN}{'═' * 80}{Colors.RESET}")
    print(f"  {Colors.DIM}Press Ctrl+C to exit{Colors.RESET}")


async def run_dashboard(num_markets: int = 5):
    """Run the live dashboard."""
    clear_screen()

    # Hide cursor
    print("\033[?25l", end="")

    stats = DashboardStats()
    order_books: dict[str, OrderBookSnapshot] = {}
    live_messages: list[str] = []
    market_names: dict[str, str] = {}

    try:
        # Fetch markets
        move_cursor(10, 1)
        print(f"{Colors.YELLOW}Fetching active markets...{Colors.RESET}")

        markets = await get_active_markets(num_markets)

        if not markets:
            print(f"{Colors.RED}Failed to fetch markets. Exiting.{Colors.RESET}")
            return

        token_ids = [m["token_id"] for m in markets]
        for m in markets:
            market_names[m["token_id"]] = m["question"]
            order_books[m["token_id"]] = OrderBookSnapshot(
                asset_id=m["token_id"],
                market_name=m["question"],
            )

        clear_screen()

        # Connect to WebSocket
        move_cursor(10, 1)
        print(f"{Colors.YELLOW}Connecting to WebSocket...{Colors.RESET}")

        async with websockets.connect(
            WS_URL,
            ping_interval=30,
            ping_timeout=10,
        ) as ws:
            stats.connected = True
            stats.connect_time = datetime.utcnow()

            # Subscribe
            subscribe_msg = {
                "type": "MARKET",
                "assets_ids": token_ids,
            }
            await ws.send(json.dumps(subscribe_msg))

            live_messages.append(f"{Colors.GREEN}[SENT]{Colors.RESET} Subscribed to {len(token_ids)} markets")

            # Message processing loop
            last_stats_time = datetime.utcnow()
            message_count_at_last_check = 0

            while True:
                try:
                    # Receive with timeout for UI updates
                    message = await asyncio.wait_for(ws.recv(), timeout=0.5)
                    recv_time = datetime.utcnow()

                    stats.message_count += 1
                    stats.last_message_time = recv_time

                    # Parse message
                    try:
                        raw_data = json.loads(message)
                        events = raw_data if isinstance(raw_data, list) else [raw_data]

                        for data in events:
                            if not isinstance(data, dict):
                                continue

                            asset_id = data.get("asset_id", "")

                            # Detect message type by content (no event_type field in WS)
                            has_bids = "bids" in data
                            has_asks = "asks" in data
                            has_changes = "changes" in data

                            if has_bids or has_asks:
                                # This is a book snapshot
                                stats.book_updates += 1

                                bids = data.get("bids", [])
                                asks = data.get("asks", [])

                                best_bid = float(bids[0]["price"]) if bids else None
                                best_ask = float(asks[0]["price"]) if asks else None
                                bid_size = float(bids[0]["size"]) if bids else None
                                ask_size = float(asks[0]["size"]) if asks else None

                                spread = None
                                if best_bid and best_ask:
                                    spread = best_ask - best_bid

                                # Update order book (create if not exists)
                                order_books[asset_id] = OrderBookSnapshot(
                                    asset_id=asset_id,
                                    market_name=market_names.get(asset_id, asset_id[:20]),
                                    best_bid=best_bid,
                                    best_ask=best_ask,
                                    bid_size=bid_size,
                                    ask_size=ask_size,
                                    spread=spread,
                                    last_update=recv_time,
                                )

                                # Add to live feed - show cents for better readability
                                bid_cents = f"{best_bid*100:.1f}c" if best_bid else "--"
                                ask_cents = f"{best_ask*100:.1f}c" if best_ask else "--"
                                live_messages.append(
                                    f"{Colors.BLUE}[BOOK]{Colors.RESET} {asset_id[:8]}.. {Colors.GREEN}Bid:{bid_cents}{Colors.RESET} {Colors.RED}Ask:{ask_cents}{Colors.RESET}"
                                )

                            elif has_changes:
                                # This is an incremental update
                                stats.price_changes += 1
                                changes = data.get("changes", [])

                                for change in changes:
                                    side = change.get("side", "?")
                                    price = change.get("price", "?")
                                    size = change.get("size", "?")

                                    # Update order book if we have it
                                    if asset_id in order_books:
                                        book = order_books[asset_id]
                                        book.last_update = recv_time
                                        try:
                                            price_val = float(price)
                                            if side == "BUY":
                                                book.best_bid = price_val
                                            elif side == "SELL":
                                                book.best_ask = price_val
                                            if book.best_bid and book.best_ask:
                                                book.spread = book.best_ask - book.best_bid
                                        except (ValueError, TypeError):
                                            pass

                                    try:
                                        price_cents = f"{float(price)*100:.1f}c"
                                    except:
                                        price_cents = str(price)

                                    side_color = Colors.GREEN if side == "BUY" else Colors.RED
                                    live_messages.append(
                                        f"{Colors.MAGENTA}[CHG]{Colors.RESET} {asset_id[:8]}.. {side_color}{side}{Colors.RESET} @ {price_cents} sz:{size}"
                                    )

                            else:
                                # Unknown message type
                                keys = list(data.keys())[:3]
                                live_messages.append(
                                    f"{Colors.DIM}[???]{Colors.RESET} keys: {keys}"
                                )

                        # Track latency (time from message timestamp to receive)
                        if events and isinstance(events[0], dict):
                            ts = events[0].get("timestamp")
                            if ts:
                                try:
                                    msg_time = datetime.utcfromtimestamp(int(ts) / 1000)
                                    latency = (recv_time - msg_time).total_seconds() * 1000
                                    if 0 < latency < 10000:  # Sanity check
                                        stats.latencies.append(latency)
                                except (ValueError, TypeError):
                                    pass

                    except json.JSONDecodeError:
                        stats.errors += 1
                        live_messages.append(f"{Colors.RED}[ERROR]{Colors.RESET} Invalid JSON")

                except asyncio.TimeoutError:
                    pass  # No message received, just update UI

                # Calculate messages per second
                now = datetime.utcnow()
                elapsed = (now - last_stats_time).total_seconds()
                if elapsed >= 1.0:
                    stats.messages_per_second = (stats.message_count - message_count_at_last_check) / elapsed
                    message_count_at_last_check = stats.message_count
                    last_stats_time = now

                # Keep message list bounded
                if len(live_messages) > 100:
                    live_messages = live_messages[-50:]

                # Render dashboard
                render_header(stats)
                render_connection_status(stats)
                render_statistics(stats)
                render_order_books(order_books)
                render_live_feed(live_messages)
                render_footer()

                # Flush output
                sys.stdout.flush()

    except KeyboardInterrupt:
        pass
    except Exception as e:
        move_cursor(38, 1)
        print(f"{Colors.RED}Error: {e}{Colors.RESET}")
    finally:
        # Show cursor
        print("\033[?25h", end="")
        move_cursor(40, 1)
        print(f"\n{Colors.YELLOW}Dashboard closed.{Colors.RESET}")
        print(f"  Total messages: {stats.message_count:,}")
        print(f"  Uptime: {stats.uptime_seconds:.1f}s")
        if stats.avg_latency_ms:
            print(f"  Avg latency: {stats.avg_latency_ms:.1f}ms")


def main():
    parser = argparse.ArgumentParser(description="WebSocket Live Dashboard")
    parser.add_argument(
        "--markets", "-m",
        type=int,
        default=5,
        help="Number of markets to monitor (default: 5)",
    )

    args = parser.parse_args()

    try:
        asyncio.run(run_dashboard(args.markets))
    except KeyboardInterrupt:
        print("\nExiting...")


if __name__ == "__main__":
    main()
