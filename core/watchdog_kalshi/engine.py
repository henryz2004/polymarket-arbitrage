"""
Kalshi Watchdog Engine
=======================

Main orchestrator for suspicious activity detection on Kalshi markets.

Coordinates:
1. KalshiRegistry — discovers events via Kalshi REST API
2. KalshiWebSocket — real-time price/trade streaming
3. KalshiPriceTracker — rolling price history with candlestick backfill
4. AnomalyDetector — spike detection + suspicion scoring (shared with Polymarket)
5. NewsChecker — Google News RSS headlines (shared)
6. AlertDispatcher — console + JSONL output (shared)

Architecture mirrors the Polymarket WatchdogEngine but swaps data sources.
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from kalshi_client.api import KalshiClient
from kalshi_client.auth import KalshiAuth
from kalshi_client.models import KalshiTickerUpdate, KalshiTrade
from kalshi_client.websocket import KalshiWebSocket

from core.watchdog.alert_dispatcher import (
    AlertDispatcher,
    ConsoleChannel,
    DiscordWebhookChannel,
    FileChannel,
)
from core.watchdog.anomaly_detector import AnomalyDetector
from core.watchdog.news_checker import NewsChecker

from core.watchdog_kalshi.models import KalshiWatchdogConfig
from core.watchdog_kalshi.price_tracker import KalshiPriceTracker
from core.watchdog_kalshi.registry import KalshiRegistry

logger = logging.getLogger(__name__)


class KalshiWatchdogEngine:
    """
    Orchestrates suspicious activity detection across Kalshi markets.

    Lifecycle:
    1. start() — registry discovery -> keyword/volume filtering -> add watches ->
       backfill candlestick history -> start WebSocket -> start scan loop
    2. _scan_loop() — periodic check of all watched markets
    3. _on_ticker_update() — event-driven immediate check on significant moves
    4. stop() — clean shutdown
    """

    def __init__(self, config: KalshiWatchdogConfig, auth: Optional[KalshiAuth] = None):
        self.config = config

        # Kalshi API client (shared between registry and price tracker)
        self._auth = auth
        self._client = KalshiClient(auth=auth, timeout=30.0, max_retries=3)

        # Core components
        self.registry = KalshiRegistry(config, self._client)
        self.price_tracker = KalshiPriceTracker(config, self._client)
        self.detector = AnomalyDetector(config)
        self.news_checker = NewsChecker(config)
        channels = [
            ConsoleChannel(),
            FileChannel(log_dir=Path("logs/watchdog_kalshi")),
        ]
        discord_channel = DiscordWebhookChannel.from_env()
        if discord_channel:
            channels.append(discord_channel)
        self.dispatcher = AlertDispatcher(channels)

        # WebSocket (requires auth)
        self._ws: Optional[KalshiWebSocket] = None

        # Background tasks
        self._scan_task: Optional[asyncio.Task] = None
        self._running = False

        # Warmup: don't fire alerts until live data has settled
        self._started_at: Optional[datetime] = None

        # Price change tracking for immediate checks
        self._last_known_price: dict[str, float] = {}  # market_ticker -> last mid

        # Stats
        self._total_scans = 0
        self._total_alerts = 0

    async def start(self) -> None:
        """Start the Kalshi watchdog engine."""
        if self._running:
            return

        self._running = True

        # Initialize API client
        await self._client.__aenter__()

        # Start sub-components
        await self.dispatcher.start()
        await self.news_checker.start()
        await self.price_tracker.start()

        # Start registry and wait for initial fetch
        await self.registry.start()
        await asyncio.sleep(1)

        # Add watches for discovered markets
        watched_count = self._discover_and_watch()
        logger.info(f"Watching {watched_count} markets across watched events")

        if watched_count == 0:
            logger.warning("No events matched watch criteria — will retry on refresh")

        # Start WebSocket if auth is available
        if self._auth:
            self._ws = KalshiWebSocket(
                auth=self._auth,
                on_ticker=self._on_ticker_update,
                on_trade=self._on_trade,
                on_connect=self._on_ws_connect,
                on_disconnect=self._on_ws_disconnect,
                demo=self.config.kalshi_demo,
            )
            await self._ws.start()
            # Subscribe to all watched markets
            tickers = list(self.price_tracker.get_watched_markets().keys())
            if tickers:
                self._ws.subscribe(tickers)
        else:
            logger.warning(
                "No Kalshi auth provided — running without WebSocket. "
                "Price data will come from candlestick polling only."
            )

        self._started_at = datetime.utcnow()

        # Backfill price history concurrently (non-blocking)
        logger.info("Backfilling price history from Kalshi candlesticks...")
        self._backfill_task = asyncio.create_task(
            self._run_backfill(),
            name="kalshi_watchdog_backfill"
        )

        # Start scan loop immediately
        self._scan_task = asyncio.create_task(
            self._scan_loop(),
            name="kalshi_watchdog_scan"
        )

        logger.info("Kalshi watchdog engine started (backfill running in background)")

    async def stop(self) -> None:
        """Stop the Kalshi watchdog engine."""
        self._running = False

        for task in [self._scan_task, getattr(self, '_backfill_task', None)]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self._ws:
            await self._ws.stop()

        await self.registry.stop()
        await self.price_tracker.stop()
        await self.news_checker.stop()
        await self.dispatcher.stop()
        await self._client.__aexit__(None, None, None)

        logger.info("Kalshi watchdog engine stopped")

    async def _run_backfill(self) -> None:
        """Background backfill task."""
        try:
            backfill_stats = await self.price_tracker.backfill_all()
            logger.info(
                f"Backfill complete: {backfill_stats['backfilled']}/{backfill_stats['total']} "
                f"markets with history, {backfill_stats['empty']} empty, "
                f"{backfill_stats['failed']} failed"
            )
        except asyncio.CancelledError:
            logger.info("Backfill cancelled")
            raise
        except Exception as e:
            logger.error(f"Backfill failed: {e}")

    def _discover_and_watch(self) -> int:
        """
        Discover events from registry and add watches.

        Returns number of markets in the watch list.
        """
        all_events = self.registry.get_all_events()

        active_tickers: set[str] = set()
        for event in all_events:
            for market in event.markets:
                if market.is_active:
                    active_tickers.add(market.ticker)
                    self.price_tracker.add_watch(event, market)

        # Clean up stale watches
        current_watches = set(self.price_tracker.get_watched_markets().keys())
        stale = current_watches - active_tickers
        for ticker in stale:
            self.price_tracker.remove_watch(ticker)
        if stale:
            logger.debug(f"Removed {len(stale)} stale watches")

        # Refresh event volumes on existing watches
        for event in all_events:
            total_vol = sum(m.volume for m in event.markets)
            for market in event.markets:
                wm = self.price_tracker.get_watched_markets().get(market.ticker)
                if wm:
                    wm.event_volume_24h = total_vol

        return len(active_tickers)

    def _on_ticker_update(self, update: KalshiTickerUpdate) -> None:
        """
        Callback from WebSocket on ticker updates.

        Samples price into tracker and triggers immediate anomaly check
        on significant moves (>2c since last sample).
        """
        market_ticker = update.market_ticker

        # Only track markets we're watching
        if market_ticker not in self.price_tracker.get_watched_markets():
            return

        # Sample into price tracker
        self.price_tracker.sample_from_ticker(update)

        # Check for significant immediate move (>2c)
        mid_price = None
        if update.yes_bid is not None and update.yes_ask is not None:
            mid_price = (update.yes_bid + update.yes_ask) / 2
        elif update.yes_ask is not None:
            mid_price = update.yes_ask
        elif update.yes_bid is not None:
            mid_price = update.yes_bid

        if mid_price is not None:
            last_price = self._last_known_price.get(market_ticker)
            self._last_known_price[market_ticker] = mid_price

            if last_price is not None and abs(mid_price - last_price) > 0.02:
                if self._past_warmup():
                    alert = self.detector.check_market(
                        market_ticker, self.price_tracker
                    )
                    if alert:
                        asyncio.create_task(
                            self._process_alert(alert),
                            name=f"alert_{market_ticker[:12]}"
                        )

    def _on_trade(self, trade: KalshiTrade) -> None:
        """
        Callback from WebSocket on trade executions.

        Could be extended for whale detection / unusual volume alerting.
        """
        # For now, just log large trades
        if trade.dollar_value > 1000:
            logger.debug(
                f"Large trade: {trade.market_ticker} "
                f"{trade.side.upper()} {trade.count:.0f} @ ${trade.price:.2f} "
                f"(${trade.dollar_value:.0f})"
            )

    def _on_ws_connect(self) -> None:
        """WebSocket connected callback."""
        logger.info("Kalshi WebSocket connected")
        # Re-subscribe to all watched markets
        tickers = list(self.price_tracker.get_watched_markets().keys())
        if self._ws and tickers:
            self._ws.subscribe(tickers)

    def _on_ws_disconnect(self) -> None:
        """WebSocket disconnected callback."""
        logger.warning("Kalshi WebSocket disconnected")

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

                # Re-discover events periodically
                if self._total_scans % 10 == 0:
                    new_count = self._discover_and_watch()
                    # Subscribe new markets to WebSocket
                    if self._ws:
                        tickers = list(
                            self.price_tracker.get_watched_markets().keys()
                        )
                        self._ws.subscribe(tickers)

                # Skip anomaly checks during warmup
                if not self._past_warmup():
                    await asyncio.sleep(self.config.price_poll_interval_seconds)
                    continue

                # If no WebSocket, poll prices via REST
                if not self._ws or not self._ws.connected:
                    await self._poll_prices()

                # Check all markets for anomalies
                alerts = self.detector.check_all_markets(self.price_tracker)

                for alert in alerts:
                    await self._process_alert(alert)

                await asyncio.sleep(self.config.price_poll_interval_seconds)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Scan loop error: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def _poll_prices(self) -> None:
        """
        Poll prices via REST when WebSocket is not available.

        Fetches orderbooks for watched markets in batches.
        """
        tickers = list(self.price_tracker.get_watched_markets().keys())
        if not tickers:
            return

        batch_size = 20
        for i in range(0, len(tickers), batch_size):
            batch = tickers[i:i + batch_size]
            tasks = [self._client.get_orderbook(t) for t in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for ticker, result in zip(batch, results):
                if isinstance(result, Exception) or result is None:
                    continue
                self.price_tracker.sample_price(
                    market_ticker=ticker,
                    best_bid=result.best_bid_yes,
                    best_ask=result.best_ask_yes,
                    source="rest_poll",
                )

            await asyncio.sleep(0.3)

    async def _process_alert(self, alert) -> None:
        """Enrich alert with news headlines and dispatch."""
        if self.config.news_check_enabled:
            try:
                from datetime import timedelta
                move_started_at = alert.detected_at - timedelta(seconds=alert.window_seconds)

                headlines = await self.news_checker.fetch_headlines(
                    alert.event_title,
                    move_started_at=move_started_at,
                )
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
        ws_stats = self._ws.get_stats() if self._ws else {}

        return {
            "running": self._running,
            "platform": "kalshi",
            "total_scans": self._total_scans,
            "total_alerts": self._total_alerts,
            "price_tracker": tracker_stats,
            "anomaly_detector": detector_stats,
            "registry": registry_stats,
            "websocket": ws_stats,
        }
