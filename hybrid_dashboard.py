#!/usr/bin/env python3
"""
Hybrid Price Feed Dashboard
============================

Real-time visualization using the hybrid data approach:
- Gamma API for bulk market discovery
- WebSocket for change detection
- CLOB /price for exact execution prices

Usage:
    python hybrid_dashboard.py [--markets N]
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.hybrid_price_feed import HybridPriceFeed, MarketPrice


# ANSI color codes
class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"


def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')


def move_cursor(row: int, col: int):
    print(f"\033[{row};{col}H", end="")


def render_header():
    move_cursor(1, 1)
    print(f"{Colors.BOLD}{Colors.CYAN}{'═' * 90}{Colors.RESET}")
    move_cursor(2, 1)
    title = "POLYMARKET HYBRID PRICE FEED - REAL EXECUTION PRICES"
    padding = (90 - len(title)) // 2
    print(f"{' ' * padding}{Colors.BOLD}{Colors.WHITE}{title}{Colors.RESET}")
    move_cursor(3, 1)
    print(f"{Colors.CYAN}{'═' * 90}{Colors.RESET}")


def render_stats(feed: HybridPriceFeed, start_time: datetime):
    stats = feed.get_stats()
    uptime = (datetime.utcnow() - start_time).total_seconds()

    move_cursor(5, 1)
    print(f"{Colors.BOLD}FEED STATUS{Colors.RESET}")
    print(f"{'─' * 44}")
    print(f"  Uptime:          {Colors.YELLOW}{uptime:.1f}s{Colors.RESET}")
    print(f"  Markets tracked: {Colors.GREEN}{stats['markets_tracked']:,}{Colors.RESET}")
    print(f"  Gamma fetches:   {Colors.BLUE}{stats['gamma_fetches']}{Colors.RESET}")
    print(f"  WS messages:     {Colors.MAGENTA}{stats['ws_messages']}{Colors.RESET}")
    print(f"  CLOB fetches:    {Colors.CYAN}{stats['clob_fetches']}{Colors.RESET}")
    print(f"  WS subscribed:   {Colors.YELLOW}{stats['ws_subscribed']}{Colors.RESET}")


def render_data_sources():
    move_cursor(5, 48)
    print(f"{Colors.BOLD}DATA SOURCES{Colors.RESET}")
    move_cursor(6, 48)
    print(f"{'─' * 42}")
    move_cursor(7, 48)
    print(f"  {Colors.BLUE}Gamma API{Colors.RESET} - Bulk prices (3min cache)")
    move_cursor(8, 48)
    print(f"  {Colors.MAGENTA}WebSocket{Colors.RESET} - Change detection (real-time)")
    move_cursor(9, 48)
    print(f"  {Colors.CYAN}CLOB API{Colors.RESET}  - Execution price (uncached)")
    move_cursor(10, 48)
    print(f"  {Colors.DIM}Prices shown are REAL execution prices{Colors.RESET}")


def render_markets(feed: HybridPriceFeed, start_row: int = 14):
    prices = feed.get_all_prices()

    # Sort by volume
    sorted_prices = sorted(
        prices.values(),
        key=lambda p: p.volume_24h,
        reverse=True
    )[:15]  # Top 15

    move_cursor(start_row, 1)
    print(f"{Colors.BOLD}TOP MARKETS BY VOLUME (Real Prices){Colors.RESET}")
    print(f"{'─' * 90}")
    print(f"  {'Market':<40} {'YES':>8} {'NO':>8} {'Sum':>8} {'Edge':>8} {'Source':>8}")
    print(f"  {'-' * 40} {'-' * 8} {'-' * 8} {'-' * 8} {'-' * 8} {'-' * 8}")

    row = start_row + 4
    for price in sorted_prices:
        move_cursor(row, 1)

        # Truncate question
        name = price.question[:38] + ".." if len(price.question) > 40 else price.question

        # Format prices in cents
        yes_str = f"{price.yes_price * 100:6.2f}c"
        no_str = f"{price.no_price * 100:6.2f}c"

        # Total (should be ~100c)
        total = price.yes_price + price.no_price
        total_str = f"{total * 100:6.2f}c"

        # Color total based on deviation from 100c
        if abs(total - 1.0) < 0.001:
            total_color = Colors.GREEN
        elif abs(total - 1.0) < 0.01:
            total_color = Colors.YELLOW
        else:
            total_color = Colors.RED

        # Edge (arbitrage opportunity)
        edge = abs(1.0 - total) * 100
        if edge >= 1.0:
            edge_str = f"{Colors.GREEN}{edge:6.2f}%{Colors.RESET}"
        elif edge >= 0.5:
            edge_str = f"{Colors.YELLOW}{edge:6.2f}%{Colors.RESET}"
        else:
            edge_str = f"{Colors.DIM}{edge:6.2f}%{Colors.RESET}"

        # Source color
        source_colors = {"gamma": Colors.BLUE, "clob": Colors.CYAN, "websocket": Colors.MAGENTA}
        source_color = source_colors.get(price.source, Colors.DIM)
        source_str = f"{source_color}{price.source:>8}{Colors.RESET}"

        print(f"  {name:<40} {Colors.GREEN}{yes_str}{Colors.RESET} {Colors.RED}{no_str}{Colors.RESET} {total_color}{total_str}{Colors.RESET} {edge_str} {source_str}")
        row += 1

    # Pad remaining rows
    while row < start_row + 19:
        move_cursor(row, 1)
        print(" " * 90)
        row += 1


def render_opportunities(feed: HybridPriceFeed, start_row: int = 34):
    opportunities = feed.get_arbitrage_opportunities(min_edge=0.005)

    move_cursor(start_row, 1)
    print(f"{Colors.BOLD}ARBITRAGE OPPORTUNITIES (Edge >= 0.5%){Colors.RESET}")
    print(f"{'─' * 90}")

    if not opportunities:
        print(f"  {Colors.DIM}No opportunities found (markets are efficient){Colors.RESET}")
    else:
        print(f"  {'Market':<45} {'YES':>8} {'NO':>8} {'Edge':>10}")
        print(f"  {'-' * 45} {'-' * 8} {'-' * 8} {'-' * 10}")

        row = start_row + 4
        for opp in opportunities[:5]:
            move_cursor(row, 1)
            name = opp.question[:43] + ".." if len(opp.question) > 45 else opp.question
            yes_str = f"{opp.yes_price * 100:6.2f}c"
            no_str = f"{opp.no_price * 100:6.2f}c"
            edge = abs(opp.arb_opportunity) * 100
            edge_str = f"{Colors.GREEN}{edge:8.3f}%{Colors.RESET}"

            print(f"  {name:<45} {yes_str} {no_str} {edge_str}")
            row += 1


def render_footer():
    move_cursor(42, 1)
    print(f"{Colors.CYAN}{'═' * 90}{Colors.RESET}")
    print(f"  {Colors.DIM}Press Ctrl+C to exit | Gamma API refresh: 30s | WS: real-time | CLOB: on-demand{Colors.RESET}")


async def run_dashboard(max_markets: int = 50):
    clear_screen()
    print("\033[?25l", end="")  # Hide cursor

    feed = HybridPriceFeed(
        gamma_refresh_interval=30.0,
        max_ws_markets=max_markets,
        min_volume_24h=100.0,  # Lower threshold to see more markets
    )

    start_time = datetime.utcnow()

    try:
        move_cursor(10, 1)
        print(f"{Colors.YELLOW}Starting Hybrid Price Feed...{Colors.RESET}")

        await feed.start()

        # Main render loop
        while True:
            render_header()
            render_stats(feed, start_time)
            render_data_sources()
            render_markets(feed)
            render_opportunities(feed)
            render_footer()

            sys.stdout.flush()
            await asyncio.sleep(0.5)

    except KeyboardInterrupt:
        pass
    except Exception as e:
        move_cursor(40, 1)
        print(f"{Colors.RED}Error: {e}{Colors.RESET}")
    finally:
        await feed.stop()
        print("\033[?25h", end="")  # Show cursor
        move_cursor(45, 1)
        stats = feed.get_stats()
        print(f"\n{Colors.YELLOW}Dashboard closed.{Colors.RESET}")
        print(f"  Markets tracked: {stats['markets_tracked']:,}")
        print(f"  Gamma fetches: {stats['gamma_fetches']}")
        print(f"  WebSocket messages: {stats['ws_messages']}")
        print(f"  CLOB fetches: {stats['clob_fetches']}")


def main():
    parser = argparse.ArgumentParser(description="Hybrid Price Feed Dashboard")
    parser.add_argument(
        "--markets", "-m",
        type=int,
        default=50,
        help="Number of markets to subscribe via WebSocket (default: 50)",
    )

    args = parser.parse_args()

    try:
        asyncio.run(run_dashboard(args.markets))
    except KeyboardInterrupt:
        print("\nExiting...")


if __name__ == "__main__":
    main()
