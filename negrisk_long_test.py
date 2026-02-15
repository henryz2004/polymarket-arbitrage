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

    def __init__(self, duration_hours: float = 4.0, min_net_edge: float = 0.015):
        self.duration = timedelta(hours=duration_hours)
        self.min_net_edge = min_net_edge
        self.start_time = datetime.now()
        self.end_time = self.start_time + self.duration

        # Setup logging
        self.log_dir = Path("logs/negrisk")
        self.log_dir.mkdir(parents=True, exist_ok=True)

        timestamp = self.start_time.strftime("%Y%m%d_%H%M%S")
        self.log_file = self.log_dir / f"negrisk_test_{timestamp}.log"
        self.opportunities_file = self.log_dir / f"opportunities_{timestamp}.jsonl"
        self.stats_file = self.log_dir / f"stats_{timestamp}.jsonl"

        self._setup_logging()

        # Configuration
        self.config = NegriskConfig(
            min_net_edge=min_net_edge,
            min_outcomes=3,
            max_legs=15,
            staleness_ttl_ms=60000.0,     # 60 seconds
            taker_fee_bps=150,
            gas_per_leg=0.05,
            min_liquidity_per_outcome=50.0,
            min_event_volume_24h=5000.0,
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
        self.opportunities_by_edge: dict[str, int] = {
            "1.5-2.0%": 0,
            "2.0-3.0%": 0,
            "3.0-5.0%": 0,
            "5.0%+": 0,
        }

        self.logger = logging.getLogger("negrisk_test")

    def _setup_logging(self):
        """Setup detailed logging to file and console."""
        # Root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)

        # File handler - everything
        file_handler = logging.FileHandler(self.log_file)
        file_handler.setLevel(logging.DEBUG)
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
        self.logger.info("Seeding initial BBA data for top 50 events...")
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
        """Seed initial BBA data for top events."""
        if not self.registry or not self.tracker:
            return

        all_events = self.registry.get_all_events()
        sorted_events = sorted(all_events, key=lambda e: e.volume_24h, reverse=True)
        top_events = sorted_events[:50]

        for event in top_events:
            try:
                await self.tracker.fetch_all_prices(event)
            except Exception as e:
                self.logger.debug(f"BBA seed error for {event.event_id}: {e}")

    def _on_price_update(self, event_id: str, token_id: str):
        """Callback for price updates."""
        # Log at debug level
        self.logger.debug(f"Price update: event={event_id[:8]}, token={token_id[:8]}")

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
                    self.logger.debug("No tradeable events")
                    continue

                opportunities = self.detector.detect_opportunities(events)

                # Log opportunities
                if opportunities:
                    self.total_opportunities += len(opportunities)
                    for opp in opportunities:
                        self._log_opportunity(opp)
                        self._categorize_opportunity(opp)

                # Log scan stats every 10 scans
                if self.total_scans % 10 == 0:
                    self.logger.debug(f"Scan #{self.total_scans}: {len(events)} events checked, "
                                    f"{len(opportunities)} opportunities found")

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
        self.logger.info("=" * 80)
        self.logger.info(f"OPPORTUNITY DETECTED: {opp.opportunity_id}")
        self.logger.info(f"Event: {opp.event.title}")
        self.logger.info(f"Sum of Asks: {opp.sum_of_asks:.4f}")
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
            self.logger.info(f"  Leg {i+1}: {leg['outcome_name'][:50]} @ ${leg['price']:.4f}")

        self.logger.info("=" * 80)

        # Write to JSONL file
        opp_data = {
            "timestamp": datetime.now().isoformat(),
            "opportunity_id": opp.opportunity_id,
            "event_title": opp.event.title,
            "event_id": opp.event.event_id,
            "sum_of_asks": opp.sum_of_asks,
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

        if edge_pct < 2.0:
            self.opportunities_by_edge["1.5-2.0%"] += 1
        elif edge_pct < 3.0:
            self.opportunities_by_edge["2.0-3.0%"] += 1
        elif edge_pct < 5.0:
            self.opportunities_by_edge["3.0-5.0%"] += 1
        else:
            self.opportunities_by_edge["5.0%+"] += 1

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
                        f"{tracker_stats.get('sequence_gaps', 0)} gaps")
        self.logger.info(f"Scans: {self.total_scans}")
        self.logger.info(f"Opportunities Detected: {det_stats['opportunities_detected']}")
        self.logger.info(f"Best Edge: {det_stats['best_edge_seen']:.4f} ({det_stats['best_edge_seen']*100:.2f}%)")
        self.logger.info(f"Rejections - Stale: {det_stats['stale_data_rejections']}, "
                        f"Liquidity: {det_stats['liquidity_rejections']}")
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

    args = parser.parse_args()

    test = NegriskLongTest(
        duration_hours=args.duration,
        min_net_edge=args.edge / 100.0,  # Convert percentage to decimal
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
