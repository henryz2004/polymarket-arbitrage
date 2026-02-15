#!/usr/bin/env python3
"""
Negrisk Hybrid Dashboard
=========================

Real-time console dashboard for neg-risk arbitrage monitoring.

Features:
- Live event discovery
- Real-time WebSocket price tracking
- Opportunity detection with detailed breakdown
- Performance statistics
- Continuous scanning

Usage:
    python negrisk_hybrid_dashboard.py
"""

import asyncio
import os
import sys
from datetime import datetime
from typing import Optional

from core.negrisk.models import NegriskConfig, NegriskOpportunity
from core.negrisk.registry import NegriskRegistry
from core.negrisk.bba_tracker import BBATracker
from core.negrisk.detector import NegriskDetector


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


class NegriskHybridDashboard:
    """Real-time console dashboard for neg-risk arbitrage."""

    def __init__(self):
        # Configuration
        self.config = NegriskConfig(
            min_net_edge=0.015,           # 1.5% minimum (relaxed for demo)
            min_outcomes=3,
            max_legs=15,
            staleness_ttl_ms=60000.0,     # 60 second staleness (realistic for prediction markets)
            taker_fee_bps=150,            # 1.5% taker fee
            gas_per_leg=0.01,
            min_liquidity_per_outcome=50.0,   # $50 min (relaxed)
            min_event_volume_24h=5000.0,      # $5k min (relaxed)
            max_position_per_event=500.0,
            skip_augmented_placeholders=True,
        )

        # Components
        self.registry: Optional[NegriskRegistry] = None
        self.tracker: Optional[BBATracker] = None
        self.detector: Optional[NegriskDetector] = None

        # State
        self._running = False
        self._scan_task: Optional[asyncio.Task] = None
        self._display_task: Optional[asyncio.Task] = None

        # Recent opportunities for display
        self._recent_opportunities: list[NegriskOpportunity] = []
        self._last_opportunity_time: Optional[datetime] = None

    async def start(self) -> None:
        """Start the dashboard."""
        self._running = True

        # Initialize components
        self.registry = NegriskRegistry(self.config)
        self.detector = NegriskDetector(self.config)

        # Start registry
        await self.registry.start()

        # Wait for initial events
        await asyncio.sleep(3)

        # Start BBA tracker with callback
        self.tracker = BBATracker(
            registry=self.registry,
            config=self.config,
            on_price_update=self._on_price_update,
        )
        await self.tracker.start()

        # Seed initial BBA data for top events by volume
        # This prevents the "all stale" problem where quiet markets never get WebSocket updates
        await self._seed_initial_bba_data()

        # Start scanning loop
        self._scan_task = asyncio.create_task(self._scan_loop())

        # Start display loop
        self._display_task = asyncio.create_task(self._display_loop())

    async def stop(self) -> None:
        """Stop the dashboard."""
        self._running = False

        if self._scan_task:
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass

        if self._display_task:
            self._display_task.cancel()
            try:
                await self._display_task
            except asyncio.CancelledError:
                pass

        if self.tracker:
            await self.tracker.stop()

        if self.registry:
            await self.registry.stop()

    def _on_price_update(self, event_id: str, token_id: str) -> None:
        """Callback when price updates."""
        # Just note that we got an update - scanning loop will detect opps
        pass

    async def _seed_initial_bba_data(self) -> None:
        """
        Seed initial BBA data from CLOB for top events.

        This solves the "all stale" problem where:
        - Registry parses BBA from Gamma API (becomes stale immediately)
        - Quiet markets never get WebSocket updates
        - All scans get rejected for stale data

        Solution: Fetch fresh CLOB prices for top events to establish baseline.
        """
        if not self.registry or not self.tracker:
            return

        # Get top 50 events by volume (most likely to have opportunities)
        all_events = self.registry.get_all_events()
        sorted_events = sorted(all_events, key=lambda e: e.volume_24h, reverse=True)
        top_events = sorted_events[:50]

        print(f"{Colors.YELLOW}Seeding BBA data for top {len(top_events)} events...{Colors.RESET}")

        # Fetch prices for each event
        for i, event in enumerate(top_events):
            try:
                await self.tracker.fetch_all_prices(event)
                if (i + 1) % 10 == 0:
                    print(f"{Colors.DIM}  Seeded {i + 1}/{len(top_events)} events...{Colors.RESET}")
            except Exception as e:
                # Don't fail startup on individual fetch errors
                pass

        print(f"{Colors.GREEN}✓ BBA seeding complete{Colors.RESET}\n")

    async def _scan_loop(self) -> None:
        """Continuously scan for opportunities."""
        while self._running:
            try:
                await asyncio.sleep(2)  # Scan every 2 seconds

                if not self.registry or not self.detector:
                    continue

                # Get tradeable events
                events = self.registry.get_tradeable_events()

                if not events:
                    continue

                # Detect opportunities
                opportunities = self.detector.detect_opportunities(events)

                # Update recent opportunities
                if opportunities:
                    self._last_opportunity_time = datetime.now()
                    # Keep last 10 opportunities
                    self._recent_opportunities.extend(opportunities)
                    self._recent_opportunities = self._recent_opportunities[-10:]

            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Don't crash the scan loop
                pass

    async def _display_loop(self) -> None:
        """Display loop - update screen continuously."""
        while self._running:
            try:
                self._render_dashboard()
                await asyncio.sleep(1)  # Update display every second
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"\n{Colors.RED}Display error: {e}{Colors.RESET}")
                await asyncio.sleep(1)

    def _render_dashboard(self) -> None:
        """Render the dashboard."""
        clear_screen()

        # Header
        move_cursor(1, 1)
        print(f"{Colors.BOLD}{Colors.CYAN}╔{'═' * 78}╗{Colors.RESET}")
        move_cursor(2, 1)
        print(f"{Colors.BOLD}{Colors.CYAN}║{Colors.RESET} ", end="")
        print(f"{Colors.BOLD}NEGRISK ARBITRAGE - LIVE DASHBOARD{Colors.RESET}", end="")
        print(f"{' ' * (78 - 36)}{Colors.BOLD}{Colors.CYAN}║{Colors.RESET}")
        move_cursor(3, 1)
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"{Colors.BOLD}{Colors.CYAN}║{Colors.RESET} {now}", end="")
        print(f"{' ' * (78 - len(now) - 2)}{Colors.BOLD}{Colors.CYAN}║{Colors.RESET}")
        move_cursor(4, 1)
        print(f"{Colors.BOLD}{Colors.CYAN}╚{'═' * 78}╗{Colors.RESET}")

        row = 6

        # Configuration
        move_cursor(row, 1)
        print(f"{Colors.BOLD}{Colors.YELLOW}CONFIG:{Colors.RESET}")
        row += 1
        move_cursor(row, 1)
        print(f"  Min Net Edge: {Colors.GREEN}{self.config.min_net_edge*100:.1f}%{Colors.RESET}  "
              f"Min Liquidity: {Colors.GREEN}${self.config.min_liquidity_per_outcome:.0f}{Colors.RESET}  "
              f"Min Volume: {Colors.GREEN}${self.config.min_event_volume_24h:,.0f}{Colors.RESET}")
        row += 2

        # Registry stats
        if self.registry:
            reg_stats = self.registry.get_stats()
            move_cursor(row, 1)
            print(f"{Colors.BOLD}{Colors.YELLOW}REGISTRY:{Colors.RESET}")
            row += 1
            move_cursor(row, 1)
            print(f"  Events Tracked: {Colors.CYAN}{reg_stats['events_tracked']}{Colors.RESET}  "
                  f"Tradeable: {Colors.GREEN}{len(self.registry.get_tradeable_events())}{Colors.RESET}  "
                  f"Tokens: {Colors.CYAN}{len(self.registry.get_all_token_ids())}{Colors.RESET}")
            row += 2

        # BBA Tracker stats
        if self.tracker:
            tracker_stats = self.tracker.get_stats()
            move_cursor(row, 1)
            print(f"{Colors.BOLD}{Colors.YELLOW}PRICE TRACKING (WebSocket):{Colors.RESET}")
            row += 1
            move_cursor(row, 1)
            ws_msgs = tracker_stats.get('ws_messages', 0)
            clob_fetches = tracker_stats.get('clob_fetches', 0)
            seq_gaps = tracker_stats.get('sequence_gaps', 0)
            tokens_tracked = tracker_stats.get('tokens_tracked', 0)

            ws_color = Colors.GREEN if ws_msgs > 0 else Colors.DIM
            print(f"  WS Messages: {ws_color}{ws_msgs:,}{Colors.RESET}  "
                  f"CLOB Fetches: {Colors.CYAN}{clob_fetches}{Colors.RESET}  "
                  f"Seq Gaps: {Colors.YELLOW if seq_gaps > 0 else Colors.GREEN}{seq_gaps}{Colors.RESET}  "
                  f"Tokens: {Colors.CYAN}{tokens_tracked}{Colors.RESET}")
            row += 2

        # Detector stats
        if self.detector:
            det_stats = self.detector.get_stats_dict()
            move_cursor(row, 1)
            print(f"{Colors.BOLD}{Colors.YELLOW}OPPORTUNITY DETECTION:{Colors.RESET}")
            row += 1
            move_cursor(row, 1)

            detected = det_stats['opportunities_detected']
            best_edge = det_stats['best_edge_seen']

            detected_color = Colors.GREEN if detected > 0 else Colors.DIM
            edge_color = Colors.GREEN if best_edge > 0.02 else (Colors.YELLOW if best_edge > 0 else Colors.DIM)

            print(f"  Detected: {detected_color}{detected}{Colors.RESET}  "
                  f"Best Edge: {edge_color}{best_edge:.4f} ({best_edge*100:.2f}%){Colors.RESET}")
            row += 1

            if det_stats['best_edge_event']:
                move_cursor(row, 1)
                event_name = det_stats['best_edge_event'][:70]
                print(f"  Best Event: {Colors.CYAN}{event_name}{Colors.RESET}")
                row += 1
            row += 1

            # Rejections
            move_cursor(row, 1)
            print(f"{Colors.BOLD}{Colors.YELLOW}REJECTIONS:{Colors.RESET}")
            row += 1
            move_cursor(row, 1)
            stale = det_stats['stale_data_rejections']
            liquidity = det_stats['liquidity_rejections']
            failures = det_stats['execution_failures']

            print(f"  Stale Data: {Colors.RED if stale > 100 else Colors.YELLOW}{stale}{Colors.RESET}  "
                  f"Low Liquidity: {Colors.YELLOW}{liquidity}{Colors.RESET}  "
                  f"Failures: {Colors.RED if failures > 0 else Colors.GREEN}{failures}{Colors.RESET}")
            row += 2

        # Recent opportunities
        move_cursor(row, 1)
        print(f"{Colors.BOLD}{Colors.YELLOW}RECENT OPPORTUNITIES:{Colors.RESET}")
        row += 1

        if self._recent_opportunities:
            move_cursor(row, 1)
            print(f"{Colors.DIM}  {'Event':<35} {'Sum':<8} {'Net%':<7} {'Legs':<5} {'Size':<7} {'Profit':<8}{Colors.RESET}")
            row += 1

            for i, opp in enumerate(reversed(self._recent_opportunities[-5:])):
                move_cursor(row, 1)
                event_name = opp.event.title[:33]
                sum_asks = f"{opp.sum_of_asks:.4f}"
                net_pct = f"{opp.net_edge*100:.2f}%"
                legs = str(opp.num_legs)
                size = f"{opp.suggested_size:.1f}"
                profit = f"${opp.expected_profit:.2f}"

                # Color code based on edge
                edge_color = Colors.GREEN if opp.net_edge > 0.03 else Colors.YELLOW

                print(f"  {Colors.WHITE}{event_name:<35}{Colors.RESET} "
                      f"{Colors.CYAN}{sum_asks:<8}{Colors.RESET} "
                      f"{edge_color}{net_pct:<7}{Colors.RESET} "
                      f"{Colors.MAGENTA}{legs:<5}{Colors.RESET} "
                      f"{Colors.CYAN}{size:<7}{Colors.RESET} "
                      f"{Colors.GREEN}{profit:<8}{Colors.RESET}")
                row += 1

            row += 1
            if self._last_opportunity_time:
                move_cursor(row, 1)
                time_ago = (datetime.now() - self._last_opportunity_time).total_seconds()
                print(f"  {Colors.DIM}Last opportunity: {time_ago:.0f}s ago{Colors.RESET}")
                row += 1
        else:
            move_cursor(row, 1)
            print(f"  {Colors.DIM}No opportunities detected yet... scanning...{Colors.RESET}")
            row += 1

        row += 1

        # Status line
        move_cursor(row, 1)
        print(f"{Colors.BOLD}{Colors.CYAN}{'─' * 80}{Colors.RESET}")
        row += 1
        move_cursor(row, 1)
        print(f"{Colors.GREEN}● RUNNING{Colors.RESET}  |  Press Ctrl+C to stop")


async def main():
    """Main entry point."""
    dashboard = NegriskHybridDashboard()

    print(f"{Colors.CYAN}Starting Negrisk Hybrid Dashboard...{Colors.RESET}")
    print(f"{Colors.DIM}Initializing components...{Colors.RESET}\n")

    try:
        await dashboard.start()

        # Wait for initial data
        print(f"{Colors.GREEN}✓ Components started{Colors.RESET}")
        print(f"{Colors.YELLOW}Waiting for initial data...{Colors.RESET}\n")
        await asyncio.sleep(5)

        # Run forever
        while True:
            await asyncio.sleep(1)

    except KeyboardInterrupt:
        print(f"\n\n{Colors.YELLOW}Shutting down...{Colors.RESET}")
    finally:
        await dashboard.stop()
        clear_screen()
        print(f"{Colors.GREEN}Dashboard stopped.{Colors.RESET}\n")


if __name__ == "__main__":
    asyncio.run(main())
