"""
Watchdog Engine
================

Main orchestrator for suspicious activity detection.

Coordinates:
1. NegriskRegistry — discovers events from Gamma API
2. BBATracker — real-time WebSocket prices
3. PriceTracker — rolling price history
4. AnomalyDetector — spike detection + suspicion scoring
5. NewsChecker — Google News RSS headlines
6. AlertDispatcher — console + file output
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from core.negrisk.bba_tracker import BBATracker
from core.negrisk.models import NegriskConfig, NegriskEvent
from core.negrisk.registry import NegriskRegistry
from core.watchdog.alert_dispatcher import AlertDispatcher, ConsoleChannel, FileChannel
from core.watchdog.anomaly_detector import AnomalyDetector
from core.watchdog.models import WatchdogConfig
from core.watchdog.news_checker import NewsChecker
from core.watchdog.price_tracker import PriceTracker

logger = logging.getLogger(__name__)


class WatchdogEngine:
    """
    Orchestrates suspicious activity detection across Polymarket events.

    Lifecycle:
    1. start() — registry discovery -> keyword/slug filtering -> add watches ->
       backfill history -> start BBA tracker -> start scan loop
    2. _scan_loop() — periodic check of all watched markets
    3. _on_price_update() — event-driven immediate check on significant moves
    4. stop() — clean shutdown
    """

    def __init__(self, config: WatchdogConfig):
        self.config = config

        # Build a NegriskConfig for the registry + tracker
        self._negrisk_config = NegriskConfig(
            min_outcomes=config.min_outcomes,
            min_event_volume_24h=config.min_event_volume_24h,
            registry_refresh_seconds=config.registry_refresh_seconds,
            bba_ws_reconnect_delay=config.bba_ws_reconnect_delay,
            staleness_ttl_ms=config.staleness_ttl_ms,
            # Relaxed settings — we're watching, not trading
            min_liquidity_per_outcome=0.0,
            min_net_edge=0.0,
            # Watchdog mode: discover ALL events (not just neg-risk) so we
            # can monitor non-neg-risk multi-outcome markets like Iran ceasefire
            watchdog_mode=True,
        )

        # Core components — own instances
        self.registry = NegriskRegistry(self._negrisk_config)
        self.tracker: Optional[BBATracker] = None
        self.price_tracker = PriceTracker(config)
        self.detector = AnomalyDetector(config)
        self.news_checker = NewsChecker(config)
        self.dispatcher = AlertDispatcher([ConsoleChannel(), FileChannel()])

        # Background tasks
        self._scan_task: Optional[asyncio.Task] = None
        self._running = False

        # Warmup: don't fire alerts until live data has settled
        self._started_at: Optional[datetime] = None

        # Price change tracking for immediate checks
        self._last_known_price: dict[str, float] = {}  # token_id -> last mid-price

        # Stats
        self._total_scans = 0
        self._total_alerts = 0

    async def start(self) -> None:
        """Start the watchdog engine."""
        if self._running:
            return

        self._running = True

        # Start sub-components
        await self.dispatcher.start()
        await self.news_checker.start()
        await self.price_tracker.start()

        # Start registry and wait for initial fetch
        await self.registry.start()
        await asyncio.sleep(2)

        # Filter events and add watches
        watched_count = self._discover_and_watch()
        logger.info(f"Watching {watched_count} outcome tokens across watched events")

        if watched_count == 0:
            logger.warning("No events matched watch criteria — will retry on registry refresh")

        # Start BBA tracker for live prices FIRST — WebSocket data starts flowing
        # immediately while backfill runs in the background.
        # Use token_filter to only subscribe to watched tokens (not all registry
        # tokens). In watchdog_mode the registry discovers ALL events, which can
        # be 10K+ tokens — subscribing to all causes WS keepalive timeouts.
        self.tracker = BBATracker(
            registry=self.registry,
            config=self._negrisk_config,
            on_price_update=self._on_price_update,
            token_filter=lambda: list(self.price_tracker.get_watched_markets().keys()),
        )
        await self.tracker.start()
        self._started_at = datetime.utcnow()

        # Backfill price history concurrently (non-blocking)
        logger.info("Backfilling price history from CLOB...")
        self._backfill_task = asyncio.create_task(
            self._run_backfill(),
            name="watchdog_backfill"
        )

        # Start scan loop immediately — WS data is already flowing
        self._scan_task = asyncio.create_task(
            self._scan_loop(),
            name="watchdog_scan"
        )

        logger.info("Watchdog engine started (backfill running in background)")

    async def stop(self) -> None:
        """Stop the watchdog engine."""
        self._running = False

        for task in [self._scan_task, getattr(self, '_backfill_task', None)]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self.tracker:
            await self.tracker.stop()

        await self.registry.stop()
        await self.price_tracker.stop()
        await self.news_checker.stop()
        await self.dispatcher.stop()

        logger.info("Watchdog engine stopped")

    async def _run_backfill(self) -> None:
        """Background backfill task."""
        try:
            backfill_stats = await self.price_tracker.backfill_all()
            logger.info(
                f"Backfill complete: {backfill_stats['backfilled']}/{backfill_stats['total']} "
                f"tokens with history, {backfill_stats['empty']} empty, "
                f"{backfill_stats['failed']} failed"
            )
        except asyncio.CancelledError:
            logger.info("Backfill cancelled")
            raise
        except Exception as e:
            logger.error(f"Backfill failed: {e}")

    def _discover_and_watch(self) -> int:
        """
        Discover events from registry and add watches based on keyword/slug/volume filters.

        Also cleans up watched markets that are no longer in the registry (resolved
        or removed events), and refreshes stale event_volume_24h on existing watches.

        Returns number of outcome tokens currently in the watch list.
        """
        all_events = self.registry.get_all_events()

        # Build set of all active token_ids from registry events we should watch
        active_token_ids: set[str] = set()
        for event in all_events:
            if self._should_watch(event):
                for outcome in event.active_outcomes:
                    if outcome.token_id:
                        active_token_ids.add(outcome.token_id)
                        self.price_tracker.add_watch(event, outcome)

        # Clean up watched markets that are no longer in the registry
        current_watches = set(self.price_tracker.get_watched_markets().keys())
        stale_tokens = current_watches - active_token_ids
        for token_id in stale_tokens:
            self.price_tracker.remove_watch(token_id)
        if stale_tokens:
            logger.debug(f"Removed {len(stale_tokens)} stale watches (resolved/removed events)")

        # Refresh event_volume_24h on existing watches from fresh registry data
        for event in all_events:
            for outcome in event.active_outcomes:
                if outcome.token_id:
                    market = self.price_tracker.get_watched_markets().get(outcome.token_id)
                    if market:
                        market.event_volume_24h = event.volume_24h

        return len(active_token_ids)

    def _should_watch(self, event: NegriskEvent) -> bool:
        """Check if an event matches watch criteria."""
        # Force-watch by slug
        if self.config.watch_slugs:
            if event.slug in self.config.watch_slugs:
                return True

        # Check volume threshold
        if event.volume_24h < self.config.min_event_volume_24h:
            return False

        # Check keyword match in title
        title_lower = event.title.lower()
        for keyword in self.config.watch_keywords:
            if keyword.lower() in title_lower:
                return True

        return False

    def _on_price_update(self, event_id: str, token_id: str) -> None:
        """
        Callback from BBA tracker on price updates.

        Samples the price into the tracker, and triggers an immediate
        anomaly check if the price moved significantly (>2c since last sample).
        """
        # Look up the current BBA from registry
        result = self.registry.get_event_by_token(token_id)
        if not result:
            return

        event, outcome = result
        bba = outcome.bba

        # Don't sample gamma-sourced BBA — these are imprecise Gamma API
        # probabilities that get reset on every registry refresh (~60s).
        # Sampling them creates phantom price baselines that cause false
        # spike alerts when real WebSocket prices arrive.
        if bba.source == "gamma":
            return

        # Only track tokens we're watching
        if token_id not in self.price_tracker.get_watched_markets():
            return

        # Sample into price tracker (rate-limited internally)
        self.price_tracker.sample_price(
            token_id=token_id,
            best_bid=bba.best_bid,
            best_ask=bba.best_ask,
            bid_size=bba.bid_size,
            ask_size=bba.ask_size,
            source=bba.source,
        )

        # Check for significant immediate move (>2c since last known price)
        mid_price = bba.mid_price
        if mid_price is not None:
            last_price = self._last_known_price.get(token_id)
            self._last_known_price[token_id] = mid_price

            if last_price is not None and abs(mid_price - last_price) > 0.02:
                if self._past_warmup():
                    alert = self.detector.check_market(token_id, self.price_tracker)
                    if alert:
                        asyncio.create_task(
                            self._process_alert(alert),
                            name=f"alert_{token_id[:8]}"
                        )

    def _past_warmup(self) -> bool:
        """Check if we're past the warmup period."""
        if self._started_at is None:
            return False
        elapsed = (datetime.utcnow() - self._started_at).total_seconds()
        return elapsed >= self.config.warmup_seconds

    async def _scan_loop(self) -> None:
        """Periodic scan of all watched markets."""
        while self._running:
            try:
                self._total_scans += 1

                # Re-discover events periodically (registry may have refreshed)
                if self._total_scans % 10 == 0:
                    self._discover_and_watch()

                # Skip anomaly checks during warmup — just collect data
                if not self._past_warmup():
                    await asyncio.sleep(self.config.price_poll_interval_seconds)
                    continue

                # Check all markets
                alerts = self.detector.check_all_markets(self.price_tracker)

                for alert in alerts:
                    await self._process_alert(alert)

                await asyncio.sleep(self.config.price_poll_interval_seconds)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Scan loop error: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def _process_alert(self, alert) -> None:
        """Enrich alert with news headlines and dispatch."""
        # Fetch news if enabled
        if self.config.news_check_enabled:
            try:
                headlines = await self.news_checker.fetch_headlines(alert.event_title)
                alert.news_headlines = headlines
                alert.news_driven = len(headlines) > 0
            except Exception as e:
                logger.debug(f"News fetch failed for alert: {e}")

        self._total_alerts += 1
        await self.dispatcher.dispatch(alert)

    def get_stats(self) -> dict:
        """Get comprehensive watchdog statistics."""
        tracker_stats = self.price_tracker.get_stats()
        detector_stats = self.detector.get_stats()
        registry_stats = self.registry.get_stats()
        ws_stats = self.tracker.get_stats() if self.tracker else {}

        return {
            "running": self._running,
            "total_scans": self._total_scans,
            "total_alerts": self._total_alerts,
            "price_tracker": tracker_stats,
            "anomaly_detector": detector_stats,
            "registry": registry_stats,
            "websocket": ws_stats,
        }
