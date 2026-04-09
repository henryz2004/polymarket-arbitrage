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

from core.negrisk.alerter import NegriskAlerter
from core.negrisk.models import NegriskConfig, NegriskOpportunity
from core.negrisk.recorder import BBARecorder
from core.negrisk.registry import NegriskRegistry
from core.negrisk.bba_tracker import BBATracker
from core.negrisk.detector import NegriskDetector
from core.negrisk.fee_models import LimitlessFeeModel, PolymarketFeeModel


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
                 min_event_volume_24h: float = 5000.0,
                 max_horizon_days: float = 90,
                 staleness_ttl_seconds: float = 60.0,
                 max_gamma_only_legs: int = 0,
                 platform: str = "polymarket",
                 execute: bool = False,
                 dry_run: bool = True,
                 max_trade_usd: float = 50.0,
                 record: bool = False,
                 ws_only: bool = False):
        self.duration = timedelta(hours=duration_hours)
        self.min_net_edge = min_net_edge
        self.min_liquidity_per_outcome = min_liquidity_per_outcome
        self.min_event_volume_24h = min_event_volume_24h
        self.max_horizon_days = max_horizon_days
        self.staleness_ttl_seconds = staleness_ttl_seconds
        self.max_gamma_only_legs = max_gamma_only_legs
        self.platform = platform
        self.execute = execute
        self.dry_run_mode = dry_run
        self.max_trade_usd = max_trade_usd
        self.record = record
        self.ws_only = ws_only
        self._limitless_executor = None
        self._polymarket_executor = None
        self._recorder: Optional[BBARecorder] = None
        self._alerter = NegriskAlerter(enable_sound=True)
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
            min_outcomes=2,                   # Include 2-outcome events for binary bundle arb
            max_legs=15,
            staleness_ttl_ms=self.staleness_ttl_seconds * 1000.0,
            fee_rate_bps=0,               # Most neg-risk markets are fee-free
            gas_per_leg=0.0,              # Polymarket covers gas on Polygon
            min_liquidity_per_outcome=self.min_liquidity_per_outcome,
            min_event_volume_24h=self.min_event_volume_24h,
            max_position_per_event=500.0,
            max_horizon_days=self.max_horizon_days,
            max_gamma_only_legs=self.max_gamma_only_legs,
            skip_augmented_placeholders=True,
            registry_refresh_seconds=300.0,  # 5 min — event list barely changes, avoids 429s
            reseed_interval_seconds=120.0,  # Re-seed gamma-only tokens every 2 min for fresher coverage
            binary_bundle_enabled=True,      # YES+NO bundle arb on 2-outcome events
            ws_only_mode=self.ws_only,
        )

        # Components
        self.registry: Optional[NegriskRegistry] = None
        self.tracker: Optional[BBATracker] = None
        self.detector: Optional[NegriskDetector] = None

        # State tracking
        self._running = False
        self._scan_task: Optional[asyncio.Task] = None
        self._stats_task: Optional[asyncio.Task] = None

        # Dedup: suppress repeated alerts for the same event within cooldown
        self._last_alert_time: dict[str, datetime] = {}
        self._alert_cooldown = timedelta(seconds=30)
        self._dedup_suppressed = 0

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
        self.logger.info(f"NEGRISK LONG-RUNNING TEST [{self.platform.upper()}]")
        self.logger.info("=" * 80)
        self.logger.info(f"Platform: {self.platform}")
        self.logger.info(f"Mode: {'EXECUTE' if self.execute else 'SCAN-ONLY'}"
                        f"{' (DRY_RUN)' if self.execute and self.dry_run_mode else ''}")
        self.logger.info(f"Duration: {self.duration.total_seconds() / 3600:.1f} hours")
        self.logger.info(f"Min Net Edge: {self.min_net_edge * 100:.1f}%")
        self.logger.info(f"Min Liquidity/Outcome: ${self.min_liquidity_per_outcome:.0f}")
        self.logger.info(f"Min Event Volume 24h: ${self.min_event_volume_24h:.0f}")
        self.logger.info(f"Max Horizon: {self.max_horizon_days:.0f} days" if self.max_horizon_days > 0 else "Max Horizon: unlimited")
        self.logger.info(f"Staleness TTL: {self.staleness_ttl_seconds:.0f}s")
        self.logger.info(f"Max Gamma-Only Legs: {self.max_gamma_only_legs}")
        self.logger.info(f"Gas Per Leg: ${self.config.gas_per_leg:.2f}")
        self.logger.info(f"Start Time: {self.start_time}")
        self.logger.info(f"End Time: {self.end_time}")
        self.logger.info(f"Log File: {self.log_file}")
        self.logger.info(f"Opportunities File: {self.opportunities_file}")
        self.logger.info("=" * 80)

        # Initialize components (platform-specific)
        self.logger.info(f"Initializing registry for platform: {self.platform}...")

        if self.platform == "limitless":
            from core.negrisk.platforms.limitless.api_client import LimitlessAPIClient
            from core.negrisk.platforms.limitless.registry import LimitlessRegistry
            from core.negrisk.platforms.limitless.bba_tracker import LimitlessBBATracker

            api_client = LimitlessAPIClient()
            await api_client.start()
            self.registry = LimitlessRegistry(self.config, api_client=api_client)
            fee_model = LimitlessFeeModel()
            self.detector = NegriskDetector(self.config, fee_model=fee_model)
            self._limitless_api_client = api_client  # Keep reference for cleanup

            # Initialize executor if --execute was passed
            if self.execute:
                import os
                from core.negrisk.platforms.limitless.executor import LimitlessExecutor

                self._limitless_executor = LimitlessExecutor(
                    api_client=api_client,
                    api_key=os.environ.get("LIMITLESS_API_KEY"),
                    private_key=os.environ.get("LIMITLESS_PRIVATE_KEY"),
                    dry_run=self.dry_run_mode,
                    max_trade_usd=self.max_trade_usd,
                )
                await self._limitless_executor.initialize()
                self.logger.info(
                    f"Limitless executor initialized "
                    f"({'DRY_RUN' if self.dry_run_mode else 'LIVE'})"
                )

                # Pre-flight checklist
                wallet = self._limitless_executor._wallet_address or "N/A (dry-run)"
                balance_str = "N/A (dry-run)"
                if not self.dry_run_mode and self._limitless_executor._wallet_address:
                    try:
                        bal = await self._limitless_executor._check_balance()
                        balance_str = f"${bal:.2f}"
                    except Exception:
                        balance_str = "ERROR (check RPC)"
                self.logger.info("=" * 50)
                self.logger.info("=== LIMITLESS LIVE EXECUTION CHECKLIST ===")
                self.logger.info(f"  1. Wallet: {wallet}")
                self.logger.info(f"  2. USDC balance: {balance_str}")
                self.logger.info(f"  3. Mode: {'DRY_RUN' if self.dry_run_mode else 'LIVE'}")
                self.logger.info(f"  4. Kill switch: touch KILL_SWITCH to halt")
                self.logger.info(f"  5. Max trade size: ${self.max_trade_usd:.2f}")
                self.logger.info("=" * 50)
        else:
            self.registry = NegriskRegistry(self.config)
            self.detector = NegriskDetector(self.config)

            # Initialize Polymarket executor if --execute was passed
            if self.execute:
                import os
                from core.negrisk.platforms.polymarket.executor import PolymarketExecutor

                self._polymarket_executor = PolymarketExecutor(
                    private_key=os.environ.get("POLYMARKET_PRIVATE_KEY"),
                    funder=os.environ.get("POLYMARKET_FUNDER"),
                    dry_run=self.dry_run_mode,
                    max_trade_usd=self.max_trade_usd,
                )
                await self._polymarket_executor.initialize()
                self.logger.info(
                    f"Polymarket executor initialized "
                    f"({'DRY_RUN' if self.dry_run_mode else 'LIVE'})"
                )

                # Pre-flight checklist
                wallet = os.environ.get("POLYMARKET_FUNDER", "N/A (dry-run)")
                balance_str = "N/A (dry-run)"
                if not self.dry_run_mode and self._polymarket_executor.funder:
                    try:
                        bal = await self._polymarket_executor._check_balance()
                        balance_str = f"${bal:.2f}"
                    except Exception:
                        balance_str = "ERROR (check RPC)"
                self.logger.info("=" * 50)
                self.logger.info("=== POLYMARKET EXECUTION CHECKLIST ===")
                self.logger.info(f"  1. Wallet: {wallet}")
                self.logger.info(f"  2. USDC.e balance: {balance_str}")
                self.logger.info(f"  3. Mode: {'DRY_RUN' if self.dry_run_mode else 'LIVE'}")
                self.logger.info(f"  4. Kill switch: touch KILL_SWITCH to halt")
                self.logger.info(f"  5. Max trade size: ${self.max_trade_usd:.2f}")
                self.logger.info("=" * 50)

        await self.registry.start()
        await asyncio.sleep(3)

        reg_stats = self.registry.get_stats()
        self.logger.info(f"Registry: {reg_stats['events_tracked']} events, "
                        f"{len(self.registry.get_all_token_ids())} tokens")

        # Start BBA recorder (if enabled)
        if self.record:
            self._recorder = BBARecorder(
                output_dir=f"logs/negrisk/recordings",
                snapshot_interval_seconds=300.0,
            )
            self._recorder.start()
            self._recorder.attach_registry(self.registry)
            self.logger.info(f"BBA Recorder enabled: {self._recorder._file_path}")

        # Start BBA tracker
        self.logger.info("Starting BBA tracker...")
        if self.platform == "limitless":
            from core.negrisk.platforms.limitless.bba_tracker import LimitlessBBATracker
            self.tracker = LimitlessBBATracker(
                registry=self.registry,
                config=self.config,
                on_price_update=self._on_price_update,
                api_client=self._limitless_api_client,
            )
        else:
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

        # Start recorder async tasks (flush + snapshot loops)
        if self._recorder:
            await self._recorder.start_async_tasks()

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

        # Stop recorder
        if self._recorder:
            await self._recorder.stop_async_tasks()
            self._recorder.stop()
            self.logger.info(f"BBA Recorder: {self._recorder.get_stats()}")

        # Cleanup platform-specific resources
        if hasattr(self, '_limitless_api_client') and self._limitless_api_client:
            await self._limitless_api_client.stop()

        # Cleanup alerter
        await self._alerter.close()

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

    def _is_dedup_suppressed(self, event_id: str, direction: str) -> bool:
        """Check if this event+direction was already alerted within cooldown."""
        key = f"{event_id}:{direction}"
        now = datetime.now()
        last = self._last_alert_time.get(key)
        if last and (now - last) < self._alert_cooldown:
            self._dedup_suppressed += 1
            return True
        self._last_alert_time[key] = now
        return False

    def _on_price_update(self, event_id: str, token_id: str):
        """
        Callback for price updates — triggers immediate event-driven scanning.

        Matches NegriskEngine's approach: pre-filter then scan the specific
        event that updated instead of waiting for the periodic poll.
        """
        if not self.registry or not self.detector:
            return

        # Pre-filter: skip events that are far from any opportunity
        # This prevents 94K+ WS messages from each triggering full detection
        # (which was the main source of stale rejection inflation)
        prefilter_threshold = max(self.min_net_edge * 3, 0.02)
        if not self.registry.is_near_opportunity(event_id, threshold=prefilter_threshold):
            return

        event = self.registry.get_event(event_id)
        if not event:
            return

        # Check both buy-side and sell-side
        buy_opp = self.detector._check_event(event)
        sell_opp = self.detector._check_event_sell_side(event)

        if buy_opp and not self._is_dedup_suppressed(event_id, "buy"):
            self.total_opportunities += 1
            self._log_opportunity(buy_opp)
            self._categorize_opportunity(buy_opp)
            if self._recorder:
                self._recorder.record_opportunity(buy_opp)
            if self._active_executor:
                asyncio.create_task(self._execute_opportunity(buy_opp))

        if sell_opp and not self._is_dedup_suppressed(event_id, "sell"):
            self.total_opportunities += 1
            self._log_opportunity(sell_opp)
            self._categorize_opportunity(sell_opp)
            if self._recorder:
                self._recorder.record_opportunity(sell_opp)
            if self._active_executor:
                asyncio.create_task(self._execute_opportunity(sell_opp))

    @property
    def _active_executor(self):
        """Get the active executor for the current platform."""
        return self._limitless_executor or self._polymarket_executor

    async def _execute_opportunity(self, opp: NegriskOpportunity):
        """Execute an opportunity via the platform-specific executor."""
        executor = self._active_executor
        if not executor:
            return
        try:
            result = await executor.execute_opportunity(opp)
            if result.success:
                self.logger.info(
                    f"EXECUTION SUCCESS: {opp.opportunity_id} ({result.reason}) "
                    f"cost=${result.total_cost:.2f} time={result.execution_time_ms:.0f}ms"
                )
            else:
                self.logger.warning(f"EXECUTION FAILED: {opp.opportunity_id} ({result.reason})")
        except Exception as e:
            self.logger.error(f"Execution error: {e}")

    async def _scan_loop(self):
        """Main scanning loop."""
        while self._running:
            try:
                # Check if we've exceeded duration
                if datetime.now() >= self.end_time:
                    self.logger.info("Test duration reached - stopping...")
                    await self.stop()
                    break

                await asyncio.sleep(30)  # Fallback only — event-driven callback handles real-time detection

                if not self.registry or not self.detector:
                    continue

                # Pre-filter: only scan events near opportunity threshold
                # This matches NegriskEngine's approach and prevents stale
                # rejection inflation from checking 300+ events every 2s
                prefilter_threshold = max(self.min_net_edge * 3, 0.02)
                events = self.registry.get_near_opportunity_events(threshold=prefilter_threshold)
                self.total_scans += 1

                if not events:
                    continue

                opportunities = self.detector.detect_opportunities(events)

                # Log and optionally execute opportunities (with dedup)
                if opportunities:
                    for opp in opportunities:
                        direction = "buy" if "buy" in opp.direction.value.lower() else "sell"
                        if self._is_dedup_suppressed(opp.event.event_id, direction):
                            continue
                        self.total_opportunities += 1
                        self._log_opportunity(opp)
                        self._categorize_opportunity(opp)
                        if self._recorder:
                            self._recorder.record_opportunity(opp)
                        if self._active_executor:
                            await self._execute_opportunity(opp)

                # Log scan stats every 5 fallback scans (~2.5 min at 30s interval)
                if self.total_scans % 5 == 0:
                    det_stats = self.detector.get_stats_dict()
                    total_events = len(self.registry.get_event_ids()) if self.registry else 0
                    self.logger.info(f"Scan #{self.total_scans}: {len(events)}/{total_events} events checked (pre-filtered), "
                                   f"{self.total_opportunities} total opportunities found, "
                                   f"edge_rejects={det_stats['edge_too_low_rejections']}, "
                                   f"stale_rejects={det_stats['stale_data_rejections']}, "
                                   f"liq_rejects={det_stats['liquidity_rejections']}")

                    # Log top candidates by closeness to opportunity
                    candidates = self.detector.get_last_scan_candidates()
                    if candidates:
                        self.logger.info("Top candidates (closest to opportunity):")
                        for c in candidates[:10]:
                            direction = c.get('direction', 'BUY')
                            self.logger.info(
                                f"  [{direction}] {c['title']} | legs={c['legs']} | "
                                f"sum={c['sum_prices']:.4f} | "
                                f"gross={c['gross_edge']:.4f} ({c['gross_edge']*100:.2f}%) | "
                                f"fee={c['fee']:.4f} | gas/sh={c['gas_per_share']:.6f} | "
                                f"net={c['net_edge']:.4f} ({c['net_edge']*100:.2f}%)"
                            )

                    # Log near-miss candidates (rejected at coverage/staleness)
                    near_misses = self.detector.get_last_scan_near_misses()
                    if near_misses:
                        self.logger.info("Near-miss candidates (coverage/staleness rejected):")
                        for nm in near_misses[:5]:
                            self.logger.info(
                                f"  [{nm['direction']}] {nm['title']} | "
                                f"legs={nm['covered']}/{nm['legs']} (missing {nm['missing']}) | "
                                f"partial_sum={nm['partial_sum']:.4f} | "
                                f"est_sum={nm['estimated_sum']:.4f} | "
                                f"est_edge={nm['gross_edge']:.4f} ({nm['gross_edge']*100:.2f}%) | "
                                f"reason={nm['rejection']}"
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

    def _fire_alert(self, opp: NegriskOpportunity):
        """Fire alerts for a detected opportunity (webhook + sound)."""
        asyncio.create_task(self._alerter.send_opportunity_alert(opp))

    def _log_opportunity(self, opp: NegriskOpportunity):
        """Log an opportunity with full details and trigger Mac audio alert."""
        direction = opp.direction.value.upper()
        price_label = "Sum of Bids" if direction == "SELL_ALL" else "Sum of Asks"

        # Build market URL
        if opp.event.platform == "limitless":
            market_url = f"https://limitless.exchange/markets/{opp.event.slug}"
        else:
            market_url = f"https://polymarket.com/event/{opp.event.slug}"

        # Trigger alerts (webhook + sound)
        self._fire_alert(opp)

        self.logger.info("=" * 80)
        self.logger.info(f"OPPORTUNITY DETECTED [{direction}]: {opp.opportunity_id}")
        self.logger.info(f"Event: {opp.event.title}")
        self.logger.info(f"URL: {market_url}")
        self.logger.info(f"Direction: {direction}")
        self.logger.info(f"{price_label}: {opp.sum_of_prices:.4f}")
        self.logger.info(f"Gross Edge: {opp.gross_edge:.4f} ({opp.gross_edge*100:.2f}%)")
        self.logger.info(f"Net Edge: {opp.net_edge:.4f} ({opp.net_edge*100:.2f}%)")
        self.logger.info(f"Legs: {opp.num_legs}")
        self.logger.info(f"Size: {opp.suggested_size:.2f} shares")
        self.logger.info(f"Total Cost: ${opp.total_cost:.2f}")
        self.logger.info(f"Expected Profit: ${opp.expected_profit:.2f}")
        self.logger.info(f"Event Volume 24h: ${opp.event.volume_24h:,.0f}")
        self.logger.info(f"Fee Rate: {opp.event.fee_rate_bps:.0f} bps")
        self.logger.info("-" * 80)

        # Log legs with price breakdown
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
            "event_slug": opp.event.slug,
            "platform": opp.event.platform,
            "market_url": market_url,
            "sum_of_prices": opp.sum_of_prices,
            "gross_edge": opp.gross_edge,
            "net_edge": opp.net_edge,
            "fee_rate_bps": opp.event.fee_rate_bps,
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
        if self._recorder:
            stats_data["recorder"] = self._recorder.get_stats()

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
        self.logger.info(f"Dedup Suppressed: {self._dedup_suppressed}")
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
            self.logger.info(f"  Incomplete Coverage: {det_stats.get('incomplete_coverage_rejections', 0)}")
            self.logger.info(f"  Low Liquidity: {det_stats['liquidity_rejections']}")
            self.logger.info(f"  Edge Too Low: {det_stats['edge_too_low_rejections']}")
            self.logger.info(f"  Execution Failures: {det_stats['execution_failures']}")

        if self._active_executor:
            exec_stats = self._active_executor.get_stats()
            self.logger.info("")
            self.logger.info(f"Executor Stats ({exec_stats.get('platform', self.platform)}):")
            self.logger.info(f"  Opportunities received: {exec_stats['opportunities_received']}")
            self.logger.info(f"  Dry-run simulations: {exec_stats['dry_run_simulations']}")
            self.logger.info(f"  Executions attempted: {exec_stats['executions_attempted']}")
            self.logger.info(f"  Executions succeeded: {exec_stats['executions_succeeded']}")
            self.logger.info(f"  Executions failed: {exec_stats['executions_failed']}")
            self.logger.info(f"  Slippage rejections: {exec_stats['slippage_rejections']}")
            self.logger.info(f"  Total volume: ${exec_stats['total_volume_usd']:.2f}")

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
    parser.add_argument('--max-horizon', type=float, default=90,
                       help='Max days until event resolution, 0=no limit (default: 90)')
    parser.add_argument('--staleness', type=float, default=60.0,
                       help='Staleness TTL in seconds — outcomes older than this are rejected (default: 60)')
    parser.add_argument('--gamma-legs', type=int, default=0,
                       help='Max gamma-only legs allowed per event (default: 0 = strict)')
    parser.add_argument('--platform', type=str, default='polymarket',
                       choices=['polymarket', 'limitless', 'all'],
                       help='Platform to scan (default: polymarket)')
    parser.add_argument('--execute', action='store_true', default=False,
                       help='Enable order execution (default: scan-only)')
    parser.add_argument('--dry-run', action='store_true', default=False,
                       help='With --execute: simulate orders without placing them')
    parser.add_argument('--setup-approvals', action='store_true', default=False,
                       help='Run token approval setup before trading (requires funded wallet)')
    parser.add_argument('--max-size', type=float, default=50.0,
                       help='Max USD per trade (default: 50)')
    parser.add_argument('--record', action='store_true', default=False,
                       help='Record BBA data for offline backtesting')
    parser.add_argument('--ws-only', action='store_true', default=False,
                       help='Skip CLOB re-validation before execution, trust WebSocket data for lower latency')

    args = parser.parse_args()

    # Platform-specific defaults
    if args.platform == "limitless":
        # Higher min edge due to 3% taker fee, lower liquidity thresholds
        if args.edge == 1.5:  # User didn't override
            args.edge = 2.0
        if args.min_liquidity == 50.0:
            args.min_liquidity = 20.0
        if args.min_volume == 5000.0:
            args.min_volume = 100.0

    # Run token approval setup if requested
    if args.setup_approvals:
        if args.platform not in ("limitless",):
            print("--setup-approvals is only supported for --platform limitless")
            print("For Polymarket, approve contracts manually (see executor.py docstring)")
            sys.exit(1)

        import os
        api_key = os.environ.get("LIMITLESS_API_KEY")
        private_key = os.environ.get("LIMITLESS_PRIVATE_KEY")
        if not api_key or not private_key:
            print("LIMITLESS_API_KEY and LIMITLESS_PRIVATE_KEY env vars required for --setup-approvals")
            sys.exit(1)

        from core.negrisk.platforms.limitless.approvals import check_and_approve
        logging.basicConfig(level=logging.INFO)
        result = await check_and_approve(
            private_key=private_key,
            api_key=api_key,
        )
        print(f"Approval setup complete: {result}")
        return

    if args.platform == "all":
        # Run both platforms concurrently
        tests = []
        for plat, edge_default, liq_default, vol_default in [
            ("polymarket", args.edge, args.min_liquidity, args.min_volume),
            ("limitless", max(args.edge, 2.0), min(args.min_liquidity, 20.0), min(args.min_volume, 100.0)),
        ]:
            tests.append(NegriskLongTest(
                duration_hours=args.duration,
                min_net_edge=edge_default / 100.0,
                min_liquidity_per_outcome=liq_default,
                min_event_volume_24h=vol_default,
                max_horizon_days=args.max_horizon,
                platform=plat,
                record=args.record,
            ))

        try:
            await asyncio.gather(*(t.start() for t in tests))
            while any(t._running for t in tests):
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            print("\n\nTest interrupted by user")
            await asyncio.gather(*(t.stop() for t in tests))
        except Exception as e:
            logging.getLogger("negrisk_test").error(f"Fatal error: {e}", exc_info=True)
            await asyncio.gather(*(t.stop() for t in tests))
        return

    test = NegriskLongTest(
        duration_hours=args.duration,
        min_net_edge=args.edge / 100.0,  # Convert percentage to decimal
        min_liquidity_per_outcome=args.min_liquidity,
        min_event_volume_24h=args.min_volume,
        max_horizon_days=args.max_horizon,
        staleness_ttl_seconds=args.staleness,
        max_gamma_only_legs=args.gamma_legs,
        platform=args.platform,
        execute=args.execute,
        dry_run=args.dry_run,
        max_trade_usd=args.max_size,
        record=args.record,
        ws_only=args.ws_only,
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
