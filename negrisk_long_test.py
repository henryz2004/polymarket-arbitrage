#!/usr/bin/env python3
"""
Negrisk Long-Running Test
==========================

Runs neg-risk arbitrage detection for extended periods with detailed logging.

Logs:
- All opportunities detected (with full details)
- Rejection statistics (stale, liquidity, etc.)
- WebSocket health metrics
- Registry refresh events
- Errors and exceptions
- Performance metrics

Usage:
    python negrisk_long_test.py [--duration HOURS] [--edge PERCENT]

    --duration: How many hours to run (default: 4)
    --edge: Minimum net edge percentage (default: 1.5)
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from core.negrisk.models import NegriskConfig, NegriskOpportunity
from core.negrisk.registry import NegriskRegistry
from core.negrisk.bba_tracker import BBATracker
from core.negrisk.detector import NegriskDetector


class DetailedFormatter(logging.Formatter):
    """Custom formatter with colors for console and detailed format."""

    COLORS = {
        'DEBUG': '\033[36m',    # Cyan
        'INFO': '\033[32m',     # Green
        'WARNING': '\033[33m',  # Yellow
        'ERROR': '\033[31m',    # Red
        'CRITICAL': '\033[35m', # Magenta
    }
    RESET = '\033[0m'

    def format(self, record):
        if hasattr(sys.stdout, 'isatty') and sys.stdout.isatty():
            # Add color for console
            color = self.COLORS.get(record.levelname, '')
            record.levelname = f"{color}{record.levelname}{self.RESET}"
        return super().format(record)


class NegriskLongTest:
    """Long-running neg-risk arbitrage test with detailed logging."""

    def __init__(self, duration_hours: float = 4.0, min_net_edge: float = 0.015,
                 min_liquidity_per_outcome: float = 50.0,
                 min_event_volume_24h: float = 5000.0):
        self.duration = timedelta(hours=duration_hours)
        self.min_net_edge = min_net_edge
        self.min_liquidity_per_outcome = min_liquidity_per_outcome
        self.min_event_volume_24h = min_event_volume_24h
        self.start_time = datetime.now()
        self.end_time = self.start_time + self.duration

        # Setup logging
        self.log_dir = Path("logs/negrisk")
        self.log_dir.mkdir(parents=True, exist_ok=True)

        timestamp = self.start_time.strftime("%Y%m%d_%H%M%S")
        edge_label = f"{min_net_edge * 100:.1f}pct"
        self.log_file = self.log_dir / f"negrisk_test_{timestamp}_{edge_label}.log"
        self.opportunities_file = self.log_dir / f"opportunities_{timestamp}_{edge_label}.jsonl"
        self.stats_file = self.log_dir / f"stats_{timestamp}_{edge_label}.jsonl"

        self._setup_logging()

        # Configuration
        self.config = NegriskConfig(
            min_net_edge=min_net_edge,
            min_outcomes=3,
            max_legs=15,
            staleness_ttl_ms=60000.0,     # 60 seconds
            fee_rate_bps=0,               # Most neg-risk markets are fee-free
            gas_per_leg=0.0,              # Polymarket covers gas on Polygon
            min_liquidity_per_outcome=self.min_liquidity_per_outcome,
            min_event_volume_24h=self.min_event_volume_24h,
            max_position_per_event=500.0,
            skip_augmented_placeholders=True,
        )

        # Components
        self.registry: Optional[NegriskRegistry] = None
        self.tracker: Optional[BBATracker] = None
        self.detector: Optional[NegriskDetector] = None

        # State tracking
        self._running = False
        self._scan_task: Optional[asyncio.Task] = None
        self._stats_task: Optional[asyncio.Task] = None

        # Metrics
        self.total_scans = 0
        self.total_opportunities = 0
        # Build dynamic edge buckets based on min_net_edge
        min_pct = self.min_net_edge * 100
        self._edge_thresholds = sorted({min_pct} | {t for t in [2.0, 3.0, 5.0] if t > min_pct})
        self.opportunities_by_edge: dict[str, int] = {}
        for i, t in enumerate(self._edge_thresholds):
            if i + 1 < len(self._edge_thresholds):
                label = f"{t:.1f}-{self._edge_thresholds[i+1]:.1f}%"
            else:
                label = f"{t:.1f}%+"
            self.opportunities_by_edge[label] = 0

        self.logger = logging.getLogger("negrisk_test")

    def _setup_logging(self):
        """Setup detailed logging to file and console."""
        # Root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)

        # Silence noisy third-party loggers (httpx logs every HTTP request)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("websockets").setLevel(logging.WARNING)
        logging.getLogger("asyncio").setLevel(logging.WARNING)

        # File handler - INFO and above (DEBUG generates ~10GB over 12 hours)
        file_handler = logging.FileHandler(self.log_file)
        file_handler.setLevel(logging.INFO)
        file_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_formatter)
        root_logger.addHandler(file_handler)

        # Console handler - INFO and above
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_formatter = DetailedFormatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%H:%M:%S'
        )
        console_handler.setFormatter(console_formatter)
        root_logger.addHandler(console_handler)

    async def start(self):
        """Start the long-running test."""
        self._running = True

        self.logger.info("=" * 80)
        self.logger.info("NEGRISK LONG-RUNNING TEST")
        self.logger.info("=" * 80)
        self.logger.info(f"Duration: {self.duration.total_seconds() / 3600:.1f} hours")
        self.logger.info(f"Min Net Edge: {self.min_net_edge * 100:.1f}%")
        self.logger.info(f"Min Liquidity/Outcome: ${self.min_liquidity_per_outcome:.0f}")
        self.logger.info(f"Min Event Volume 24h: ${self.min_event_volume_24h:.0f}")
        self.logger.info(f"Gas Per Leg: ${self.config.gas_per_leg:.2f}")
        self.logger.info(f"Start Time: {self.start_time}")
        self.logger.info(f"End Time: {self.end_time}")
        self.logger.info(f"Log File: {self.log_file}")
        self.logger.info(f"Opportunities File: {self.opportunities_file}")
        self.logger.info("=" * 80)

        # Initialize components
        self.logger.info("Initializing registry...")
        self.registry = NegriskRegistry(self.config)
        self.detector = NegriskDetector(self.config)

        await self.registry.start()
        await asyncio.sleep(3)

        reg_stats = self.registry.get_stats()
        self.logger.info(f"Registry: {reg_stats['events_tracked']} events, "
                        f"{len(self.registry.get_all_token_ids())} tokens")

        # Start BBA tracker
        self.logger.info("Starting BBA tracker...")
        self.tracker = BBATracker(
            registry=self.registry,
            config=self.config,
            on_price_update=self._on_price_update,
        )
        await self.tracker.start()
        await asyncio.sleep(3)

        # Seed initial BBA data
        self.logger.info("Seeding initial BBA data for all tradeable events...")
        await self._seed_bba_data()
        self.logger.info("BBA seeding complete")

        # Start background tasks
        self._scan_task = asyncio.create_task(self._scan_loop())
        self._stats_task = asyncio.create_task(self._stats_loop())

        self.logger.info("Test started - scanning for opportunities...")

    async def stop(self):
        """Stop the test."""
        self._running = False

        if self._scan_task:
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass

        if self._stats_task:
            self._stats_task.cancel()
            try:
                await self._stats_task
            except asyncio.CancelledError:
                pass

        if self.tracker:
            await self.tracker.stop()

        if self.registry:
            await self.registry.stop()

        self.logger.info("Test stopped")
        self._log_final_summary()

    async def _seed_bba_data(self):
        """Seed initial BBA data for all tradeable events with batching and rate limiting."""
        if not self.registry or not self.tracker:
            return

        # Get all tradeable events sorted by volume (highest first)
        all_events = self.registry.get_tradeable_events()
        sorted_events = sorted(all_events, key=lambda e: e.volume_24h, reverse=True)

        if not sorted_events:
            self.logger.warning("No tradeable events found for BBA seeding")
            return

        total_events = len(sorted_events)
        total_tokens = sum(len([o for o in e.active_outcomes if o.token_id]) for e in sorted_events)
        self.logger.info(f"Starting BBA seed for {total_events} events ({total_tokens} tokens)")

        # Process in batches of 10 events with 0.5s delay between batches
        batch_size = 10
        seeded_events = 0
        total_seeded_tokens = 0
        total_empty_tokens = 0
        total_failed_tokens = 0

        for i in range(0, total_events, batch_size):
            batch = sorted_events[i:i + batch_size]

            # Fetch prices for all events in batch concurrently
            for event in batch:
                try:
                    stats = await self.tracker.fetch_all_prices(event)
                    seeded_events += 1
                    total_seeded_tokens += stats["seeded"]
                    total_empty_tokens += stats["empty"]
                    total_failed_tokens += stats["failed"]
                except Exception as e:
                    self.logger.debug(f"BBA seed error for {event.event_id}: {e}")

            # Log progress every 50 events
            if seeded_events % 50 == 0 or seeded_events == total_events:
                self.logger.info(
                    f"Seeded {seeded_events}/{total_events} events | "
                    f"tokens: {total_seeded_tokens} with books, "
                    f"{total_empty_tokens} empty, {total_failed_tokens} failed"
                )

            # Rate limiting: 0.5s delay between batches (unless this is the last batch)
            if i + batch_size < total_events:
                await asyncio.sleep(0.5)

        # Summary: count source breakdown across all outcomes
        source_counts = {"gamma": 0, "clob": 0, "websocket": 0, "unknown": 0}
        for event in sorted_events:
            for o in event.active_outcomes:
                source_counts[o.bba.source] = source_counts.get(o.bba.source, 0) + 1

        self.logger.info(
            f"BBA source breakdown: clob={source_counts.get('clob', 0)}, "
            f"websocket={source_counts.get('websocket', 0)}, "
            f"gamma_only={source_counts.get('gamma', 0)}, "
            f"unknown={source_counts.get('unknown', 0)}"
        )
        self.logger.info(
            f"CLOB seeding: {total_seeded_tokens} tokens with books, "
            f"{total_empty_tokens} empty books (truly illiquid), "
            f"{total_failed_tokens} fetch failures"
        )

    def _on_price_update(self, event_id: str, token_id: str):
        """
        Callback for price updates — triggers immediate event-driven scanning.

        Matches NegriskEngine's approach: scan the specific event that updated
        instead of waiting for the periodic poll.
        """
        if not self.registry or not self.detector:
            return

        event = self.registry.get_event(event_id)
        if not event:
            return

        # Check both buy-side and sell-side
        buy_opp = self.detector._check_event(event)
        sell_opp = self.detector._check_event_sell_side(event)

        if buy_opp:
            self.total_opportunities += 1
            self._log_opportunity(buy_opp)
            self._categorize_opportunity(buy_opp)

        if sell_opp:
            self.total_opportunities += 1
            self._log_opportunity(sell_opp)
            self._categorize_opportunity(sell_opp)

    async def _scan_loop(self):
        """Main scanning loop."""
        while self._running:
            try:
                # Check if we've exceeded duration
                if datetime.now() >= self.end_time:
                    self.logger.info("Test duration reached - stopping...")
                    await self.stop()
                    break

                await asyncio.sleep(2)  # Scan every 2 seconds

                if not self.registry or not self.detector:
                    continue

                # Scan for opportunities
                events = self.registry.get_tradeable_events()
                self.total_scans += 1

                if not events:
                    continue

                opportunities = self.detector.detect_opportunities(events)

                # Log opportunities
                if opportunities:
                    self.total_opportunities += len(opportunities)
                    for opp in opportunities:
                        self._log_opportunity(opp)
                        self._categorize_opportunity(opp)

                # Log scan stats every 100 scans (~3 min at 2s interval)
                if self.total_scans % 100 == 0:
                    det_stats = self.detector.get_stats_dict()
                    self.logger.info(f"Scan #{self.total_scans}: {len(events)} events checked, "
                                   f"{self.total_opportunities} total opportunities found, "
                                   f"edge_rejects={det_stats['edge_too_low_rejections']}, "
                                   f"liq_rejects={det_stats['liquidity_rejections']}")

                    # Log top candidates by gross edge (sanity check)
                    candidates = self.detector.get_last_scan_candidates()
                    if candidates:
                        self.logger.info("Top candidates this scan (by gross edge):")
                        for c in candidates[:10]:
                            direction = c.get('direction', 'BUY')
                            self.logger.info(
                                f"  [{direction}] {c['title']} | legs={c['legs']} | "
                                f"sum={c['sum_prices']:.4f} | "
                                f"gross={c['gross_edge']:.4f} ({c['gross_edge']*100:.2f}%) | "
                                f"fee={c['fee']:.4f} | gas/sh={c['gas_per_share']:.6f} | "
                                f"net={c['net_edge']:.4f} ({c['net_edge']*100:.2f}%)"
                            )

            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger.error(f"Scan error: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def _stats_loop(self):
        """Periodically log statistics."""
        while self._running:
            try:
                await asyncio.sleep(300)  # Every 5 minutes
                self._log_stats_snapshot()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger.error(f"Stats error: {e}")

    def _log_opportunity(self, opp: NegriskOpportunity):
        """Log an opportunity with full details."""
        direction = opp.direction.value.upper()
        price_label = "Sum of Bids" if direction == "SELL_ALL" else "Sum of Asks"

        self.logger.info("=" * 80)
        self.logger.info(f"OPPORTUNITY DETECTED [{direction}]: {opp.opportunity_id}")
        self.logger.info(f"Event: {opp.event.title}")
        self.logger.info(f"Direction: {direction}")
        self.logger.info(f"{price_label}: {opp.sum_of_prices:.4f}")
        self.logger.info(f"Gross Edge: {opp.gross_edge:.4f} ({opp.gross_edge*100:.2f}%)")
        self.logger.info(f"Net Edge: {opp.net_edge:.4f} ({opp.net_edge*100:.2f}%)")
        self.logger.info(f"Legs: {opp.num_legs}")
        self.logger.info(f"Size: {opp.suggested_size:.2f} shares")
        self.logger.info(f"Total Cost: ${opp.total_cost:.2f}")
        self.logger.info(f"Expected Profit: ${opp.expected_profit:.2f}")
        self.logger.info(f"Event Volume 24h: ${opp.event.volume_24h:,.0f}")
        self.logger.info("-" * 80)

        # Log legs
        for i, leg in enumerate(opp.legs):
            self.logger.info(f"  Leg {i+1}: {leg['side']} {leg['outcome_name'][:50]} @ ${leg['price']:.4f}")

        self.logger.info("=" * 80)

        # Write to JSONL file
        opp_data = {
            "timestamp": datetime.now().isoformat(),
            "opportunity_id": opp.opportunity_id,
            "direction": opp.direction.value,
            "event_title": opp.event.title,
            "event_id": opp.event.event_id,
            "sum_of_prices": opp.sum_of_prices,
            "gross_edge": opp.gross_edge,
            "net_edge": opp.net_edge,
            "num_legs": opp.num_legs,
            "suggested_size": opp.suggested_size,
            "total_cost": opp.total_cost,
            "expected_profit": opp.expected_profit,
            "event_volume_24h": opp.event.volume_24h,
            "legs": opp.legs,
        }

        with open(self.opportunities_file, 'a') as f:
            f.write(json.dumps(opp_data) + '\n')

    def _categorize_opportunity(self, opp: NegriskOpportunity):
        """Categorize opportunity by edge size."""
        edge_pct = opp.net_edge * 100
        labels = list(self.opportunities_by_edge.keys())
        thresholds = self._edge_thresholds

        for i in range(len(thresholds) - 1, -1, -1):
            if edge_pct >= thresholds[i]:
                self.opportunities_by_edge[labels[i]] += 1
                return
        # Below all thresholds — put in first bucket
        self.opportunities_by_edge[labels[0]] += 1

    def _log_stats_snapshot(self):
        """Log current statistics snapshot."""
        if not self.detector or not self.tracker or not self.registry:
            return

        runtime = datetime.now() - self.start_time
        det_stats = self.detector.get_stats_dict()
        tracker_stats = self.tracker.get_stats()
        reg_stats = self.registry.get_stats()

        self.logger.info("-" * 80)
        self.logger.info(f"STATS SNAPSHOT - Runtime: {runtime}")
        self.logger.info(f"Registry: {reg_stats['events_tracked']} events")
        self.logger.info(f"WebSocket: {tracker_stats.get('ws_messages', 0)} messages, "
                        f"{tracker_stats.get('sequence_gaps', 0)} gaps, "
                        f"{tracker_stats.get('empty_books', 0)} empty books")
        self.logger.info(f"Scans: {self.total_scans}")
        self.logger.info(f"Opportunities Detected: {det_stats['opportunities_detected']}")
        self.logger.info(f"Best Edge: {det_stats['best_edge_seen']:.4f} ({det_stats['best_edge_seen']*100:.2f}%)")
        self.logger.info(f"Rejections - Stale: {det_stats['stale_data_rejections']}, "
                        f"Liquidity: {det_stats['liquidity_rejections']}, "
                        f"Edge Too Low: {det_stats['edge_too_low_rejections']}")
        self.logger.info("-" * 80)

        # Write to stats file
        stats_data = {
            "timestamp": datetime.now().isoformat(),
            "runtime_seconds": runtime.total_seconds(),
            "total_scans": self.total_scans,
            "registry": reg_stats,
            "tracker": tracker_stats,
            "detector": det_stats,
            "opportunities_by_edge": self.opportunities_by_edge,
        }

        with open(self.stats_file, 'a') as f:
            f.write(json.dumps(stats_data) + '\n')

    def _log_final_summary(self):
        """Log final test summary."""
        runtime = datetime.now() - self.start_time

        self.logger.info("=" * 80)
        self.logger.info("FINAL TEST SUMMARY")
        self.logger.info("=" * 80)
        self.logger.info(f"Runtime: {runtime}")
        self.logger.info(f"Total Scans: {self.total_scans}")
        self.logger.info(f"Total Opportunities: {self.total_opportunities}")
        self.logger.info("")
        self.logger.info("Opportunities by Edge:")
        for edge_range, count in self.opportunities_by_edge.items():
            self.logger.info(f"  {edge_range}: {count}")
        self.logger.info("")

        if self.detector:
            det_stats = self.detector.get_stats_dict()
            self.logger.info(f"Best Edge Seen: {det_stats['best_edge_seen']:.4f} "
                           f"({det_stats['best_edge_seen']*100:.2f}%)")
            if det_stats['best_edge_event']:
                self.logger.info(f"Best Event: {det_stats['best_edge_event']}")
            self.logger.info("")
            self.logger.info("Rejections:")
            self.logger.info(f"  Stale Data: {det_stats['stale_data_rejections']}")
            self.logger.info(f"  Low Liquidity: {det_stats['liquidity_rejections']}")
            self.logger.info(f"  Edge Too Low: {det_stats['edge_too_low_rejections']}")
            self.logger.info(f"  Execution Failures: {det_stats['execution_failures']}")

        self.logger.info("=" * 80)
        self.logger.info(f"Logs saved to: {self.log_file}")
        self.logger.info(f"Opportunities saved to: {self.opportunities_file}")
        self.logger.info(f"Stats saved to: {self.stats_file}")
        self.logger.info("=" * 80)


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description='Run long-term neg-risk arbitrage test')
    parser.add_argument('--duration', type=float, default=4.0,
                       help='Test duration in hours (default: 4)')
    parser.add_argument('--edge', type=float, default=1.5,
                       help='Minimum net edge percentage (default: 1.5)')
    parser.add_argument('--min-liquidity', type=float, default=50.0,
                       help='Minimum ask liquidity per outcome in $ (default: 50)')
    parser.add_argument('--min-volume', type=float, default=5000.0,
                       help='Minimum 24h event volume in $ (default: 5000)')

    args = parser.parse_args()

    test = NegriskLongTest(
        duration_hours=args.duration,
        min_net_edge=args.edge / 100.0,  # Convert percentage to decimal
        min_liquidity_per_outcome=args.min_liquidity,
        min_event_volume_24h=args.min_volume,
    )

    try:
        await test.start()

        # Wait for test to complete
        while test._running:
            await asyncio.sleep(1)

    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
        await test.stop()

    except Exception as e:
        test.logger.error(f"Fatal error: {e}", exc_info=True)
        await test.stop()


if __name__ == "__main__":
    asyncio.run(main())
