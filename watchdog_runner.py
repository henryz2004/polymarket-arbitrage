#!/usr/bin/env python3
"""
Suspicious Activity Watchdog Runner
=====================================

Monitors Polymarket events for suspicious price movements without
corresponding public news catalysts. Inspired by the Iran strike market
insider-trading pattern: price jumped 2.5x (7c->25.5c) 21 hours before
any public news, during off-hours (2:15 AM PST).

Usage:
    python watchdog_runner.py [--duration HOURS] [--keywords KEYWORDS]
                               [--watch-slugs SLUGS] [--min-volume USD]

Examples:
    # Run for 24 hours with defaults (geopolitical keywords)
    python watchdog_runner.py

    # Run for 1 hour, lower volume threshold
    python watchdog_runner.py --duration 1 --min-volume 5000

    # Watch specific slugs
    python watchdog_runner.py --watch-slugs "us-strikes-iran,china-invades-taiwan"

    # Custom keywords
    python watchdog_runner.py --keywords "iran,strike,nuclear,sanctions"
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from core.watchdog.engine import WatchdogEngine
from core.watchdog.models import WatchdogConfig


class DetailedFormatter(logging.Formatter):
    """Custom formatter with colors for console output."""

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
            color = self.COLORS.get(record.levelname, '')
            record.levelname = f"{color}{record.levelname}{self.RESET}"
        return super().format(record)


class WatchdogRunner:
    """Long-running watchdog with logging and periodic stats."""

    def __init__(self, config: WatchdogConfig, duration_hours: float = 24.0):
        self.config = config
        self.duration = timedelta(hours=duration_hours)
        self.start_time = datetime.now()
        self.end_time = self.start_time + self.duration

        # Logging setup
        self.log_dir = Path("logs/watchdog")
        self.log_dir.mkdir(parents=True, exist_ok=True)

        timestamp = self.start_time.strftime("%Y%m%d_%H%M%S")
        self.log_file = self.log_dir / f"watchdog_{timestamp}.log"
        self.stats_file = self.log_dir / f"watchdog_stats_{timestamp}.jsonl"

        self._setup_logging()
        self.logger = logging.getLogger("watchdog")

        # Engine
        self.engine: Optional[WatchdogEngine] = None
        self._running = False
        self._scan_task: Optional[asyncio.Task] = None
        self._stats_task: Optional[asyncio.Task] = None

    def _setup_logging(self):
        """Setup file and console logging."""
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)

        # Silence noisy loggers
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("websockets").setLevel(logging.WARNING)
        logging.getLogger("asyncio").setLevel(logging.WARNING)

        # File handler
        file_handler = logging.FileHandler(self.log_file)
        file_handler.setLevel(logging.INFO)
        file_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_formatter)
        root_logger.addHandler(file_handler)

        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_formatter = DetailedFormatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%H:%M:%S'
        )
        console_handler.setFormatter(console_formatter)
        root_logger.addHandler(console_handler)

    async def start(self):
        """Start the watchdog."""
        self._running = True

        self.logger.info("=" * 80)
        self.logger.info("SUSPICIOUS ACTIVITY WATCHDOG")
        self.logger.info("=" * 80)
        self.logger.info(f"Duration: {self.duration.total_seconds() / 3600:.1f} hours")
        self.logger.info(f"Keywords: {', '.join(self.config.watch_keywords[:10])}"
                        f"{'...' if len(self.config.watch_keywords) > 10 else ''}")
        if self.config.watch_slugs:
            self.logger.info(f"Watch Slugs: {', '.join(self.config.watch_slugs)}")
        self.logger.info(f"Min Volume: ${self.config.min_event_volume_24h:,.0f}")
        self.logger.info(f"Relative Thresholds: {self.config.relative_thresholds}")
        self.logger.info(f"Absolute Thresholds: {self.config.absolute_thresholds}")
        self.logger.info(f"Off-Hours (UTC): {self.config.off_hours_utc[0]}:00 - "
                        f"{self.config.off_hours_utc[1]}:00")
        self.logger.info(f"Alert Cooldown: {self.config.alert_cooldown_seconds}s")
        self.logger.info(f"News Check: {'enabled' if self.config.news_check_enabled else 'disabled'}")
        self.logger.info(f"Start Time: {self.start_time}")
        self.logger.info(f"End Time: {self.end_time}")
        self.logger.info(f"Log File: {self.log_file}")
        self.logger.info("=" * 80)

        # Start engine
        self.engine = WatchdogEngine(self.config)
        await self.engine.start()

        # Log initial state
        stats = self.engine.get_stats()
        self.logger.info(
            f"Watchdog active: {stats['price_tracker']['markets_watched']} markets watched, "
            f"{stats['registry']['events_tracked']} events tracked"
        )

        # Start duration check and stats loop
        self._scan_task = asyncio.create_task(self._duration_loop())
        self._stats_task = asyncio.create_task(self._stats_loop())

    async def stop(self):
        """Stop the watchdog."""
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

        if self.engine:
            await self.engine.stop()

        self._log_final_summary()

    async def _duration_loop(self):
        """Check if duration has been exceeded."""
        while self._running:
            try:
                if datetime.now() >= self.end_time:
                    self.logger.info("Duration reached — stopping watchdog...")
                    await self.stop()
                    break
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise

    async def _stats_loop(self):
        """Periodically log stats."""
        while self._running:
            try:
                await asyncio.sleep(300)  # Every 5 minutes
                self._log_stats_snapshot()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger.error(f"Stats loop error: {e}")

    def _log_stats_snapshot(self):
        """Log current statistics."""
        if not self.engine:
            return

        runtime = datetime.now() - self.start_time
        stats = self.engine.get_stats()

        self.logger.info("-" * 60)
        self.logger.info(f"STATS — Runtime: {runtime}")
        self.logger.info(f"  Markets watched: {stats['price_tracker']['markets_watched']}")
        self.logger.info(f"  Markets with data: {stats['price_tracker']['markets_with_data']}")
        self.logger.info(f"  Total snapshots: {stats['price_tracker']['total_snapshots']}")
        self.logger.info(f"  Scans: {stats['total_scans']}")
        self.logger.info(f"  Alerts fired: {stats['total_alerts']}")
        self.logger.info(f"  Highest score: {stats['anomaly_detector']['highest_score']}")
        ws = stats.get('websocket', {})
        self.logger.info(f"  WS messages: {ws.get('ws_messages', 0)}")
        self.logger.info("-" * 60)

        # Write to JSONL
        stats_data = {
            "timestamp": datetime.now().isoformat(),
            "runtime_seconds": runtime.total_seconds(),
            **stats,
        }
        try:
            with open(self.stats_file, 'a') as f:
                f.write(json.dumps(stats_data, default=str) + '\n')
        except Exception as e:
            self.logger.debug(f"Stats write error: {e}")

    def _log_final_summary(self):
        """Log final summary."""
        runtime = datetime.now() - self.start_time

        self.logger.info("=" * 80)
        self.logger.info("WATCHDOG FINAL SUMMARY")
        self.logger.info("=" * 80)
        self.logger.info(f"Runtime: {runtime}")

        if self.engine:
            stats = self.engine.get_stats()
            self.logger.info(f"Total Scans: {stats['total_scans']}")
            self.logger.info(f"Total Alerts: {stats['total_alerts']}")
            self.logger.info(f"Highest Suspicion Score: {stats['anomaly_detector']['highest_score']}")
            self.logger.info(f"Markets Watched: {stats['price_tracker']['markets_watched']}")
            self.logger.info(f"Total Price Snapshots: {stats['price_tracker']['total_snapshots']}")

        self.logger.info(f"Log File: {self.log_file}")
        self.logger.info(f"Stats File: {self.stats_file}")
        self.logger.info(f"Alerts: logs/watchdog/alerts_*.jsonl")
        self.logger.info("=" * 80)


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Suspicious Activity Watchdog for Polymarket',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python watchdog_runner.py --duration 24
  python watchdog_runner.py --duration 1 --min-volume 5000
  python watchdog_runner.py --keywords "iran,strike,nuclear"
  python watchdog_runner.py --watch-slugs "us-strikes-iran"
        """
    )
    parser.add_argument('--duration', type=float, default=24.0,
                       help='Duration in hours (default: 24)')
    parser.add_argument('--keywords', type=str, default=None,
                       help='Comma-separated keywords to watch (overrides defaults)')
    parser.add_argument('--watch-slugs', type=str, default=None,
                       help='Comma-separated event slugs to force-watch')
    parser.add_argument('--min-volume', type=float, default=10000.0,
                       help='Minimum 24h event volume in $ (default: 10000)')
    parser.add_argument('--poll-interval', type=float, default=60.0,
                       help='Scan interval in seconds (default: 60)')
    parser.add_argument('--cooldown', type=float, default=300.0,
                       help='Alert cooldown in seconds (default: 300)')
    parser.add_argument('--no-news', action='store_true', default=False,
                       help='Disable Google News headline fetching')

    args = parser.parse_args()

    # Build config
    config = WatchdogConfig(
        min_event_volume_24h=args.min_volume,
        price_poll_interval_seconds=args.poll_interval,
        alert_cooldown_seconds=args.cooldown,
        news_check_enabled=not args.no_news,
    )

    if args.keywords:
        config.watch_keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]

    if args.watch_slugs:
        config.watch_slugs = [s.strip() for s in args.watch_slugs.split(",") if s.strip()]

    runner = WatchdogRunner(config=config, duration_hours=args.duration)

    try:
        await runner.start()

        # Wait for completion
        while runner._running:
            await asyncio.sleep(1)

    except KeyboardInterrupt:
        print("\n\nWatchdog interrupted by user")
        await runner.stop()

    except Exception as e:
        runner.logger.error(f"Fatal error: {e}", exc_info=True)
        await runner.stop()


if __name__ == "__main__":
    asyncio.run(main())
