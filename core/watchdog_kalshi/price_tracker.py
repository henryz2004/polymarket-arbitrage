"""
Kalshi Price Tracker
=====================

Maintains rolling price history for watched Kalshi markets.

Sources:
1. Candlestick API (backfill on startup) — much richer than Polymarket's prices-history
2. WebSocket ticker updates (live, rate-limited per market)

Key difference from Polymarket tracker: uses market_ticker as the primary key
instead of token_id. All internal interfaces are identical so the shared
AnomalyDetector works unchanged.
"""

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Optional

from kalshi_client.api import KalshiClient
from kalshi_client.models import KalshiEvent, KalshiMarket, KalshiTickerUpdate
from core.watchdog.models import PriceSnapshot, WatchdogConfig
from core.watchdog.price_tracker import WatchedMarket

logger = logging.getLogger(__name__)


class KalshiPriceTracker:
    """
    Tracks rolling price history for watched Kalshi markets.

    Uses the same WatchedMarket class as the Polymarket tracker, with
    market_ticker as the key (stored in WatchedMarket.token_id).

    Startup: backfills from Kalshi candlestick API (1-hour candles, 48h window).
    Live: samples from WebSocket ticker callbacks, rate-limited per market.
    """

    def __init__(self, config: WatchdogConfig, client: KalshiClient):
        self.config = config
        self._client = client
        self._markets: dict[str, WatchedMarket] = {}  # market_ticker -> WatchedMarket

    async def start(self) -> None:
        """Initialize (client already initialized externally)."""
        pass

    async def stop(self) -> None:
        """Cleanup."""
        pass

    def add_watch(self, event: KalshiEvent, market: KalshiMarket) -> None:
        """Add a Kalshi market to the watch list."""
        if market.ticker in self._markets:
            return

        # Calculate total event volume
        total_volume = sum(m.volume for m in event.markets)

        self._markets[market.ticker] = WatchedMarket(
            token_id=market.ticker,  # Use market_ticker as the watchdog key
            event_id=market.event_ticker,
            outcome_name=market.title or market.subtitle,
            event_title=event.title,
            event_slug=market.series_ticker,  # For live-event filtering
            event_volume_24h=total_volume,
            max_history_hours=self.config.price_history_window_hours,
        )

    def remove_watch(self, market_ticker: str) -> None:
        """Remove a market from the watch list."""
        self._markets.pop(market_ticker, None)

    def get_watched_markets(self) -> dict[str, WatchedMarket]:
        """Get all watched markets."""
        return self._markets

    def sample_price(self, market_ticker: str, best_bid: Optional[float],
                     best_ask: Optional[float], bid_size: Optional[float] = None,
                     ask_size: Optional[float] = None, source: str = "websocket") -> None:
        """
        Sample a price update into the rolling history.

        Rate-limited to 1 sample per min_sample_interval_seconds per market.
        """
        market = self._markets.get(market_ticker)
        if not market:
            return

        now = datetime.utcnow()

        # Rate-limit sampling
        if market.last_sample_at:
            elapsed = (now - market.last_sample_at).total_seconds()
            if elapsed < self.config.min_sample_interval_seconds:
                return

        # Calculate mid-price
        mid_price = None
        if best_bid is not None and best_ask is not None:
            mid_price = (best_bid + best_ask) / 2
        elif best_ask is not None:
            mid_price = best_ask
        elif best_bid is not None:
            mid_price = best_bid

        if mid_price is None:
            return

        snapshot = PriceSnapshot(
            timestamp=now,
            mid_price=mid_price,
            best_bid=best_bid,
            best_ask=best_ask,
            bid_size=bid_size,
            ask_size=ask_size,
            source=source,
        )

        market.history.append(snapshot)
        if source not in ("candlestick_history",):
            market.live_history.append(snapshot)
        market.last_sample_at = now

    def sample_from_ticker(self, update: KalshiTickerUpdate) -> None:
        """
        Sample a price from a WebSocket ticker update.

        Convenience method that extracts bid/ask from the ticker format.
        """
        self.sample_price(
            market_ticker=update.market_ticker,
            best_bid=update.yes_bid,
            best_ask=update.yes_ask,
            bid_size=update.yes_bid_size if update.yes_bid_size > 0 else None,
            ask_size=update.yes_ask_size if update.yes_ask_size > 0 else None,
            source="websocket",
        )

    def get_price_change(self, token_id: str, window_seconds: int) -> Optional[tuple[float, float, float]]:
        """
        Get price change over a time window using only live-sourced snapshots.

        Gap-aware: finds last known price BEFORE window as baseline.

        Returns:
            (price_before, price_now, pct_change) or None if insufficient data.
        """
        market = self._markets.get(token_id)
        if not market or len(market.live_history) < 2:
            return None

        live = market.live_history
        now = live[-1].timestamp
        price_now = live[-1].mid_price
        if price_now is None:
            return None

        cutoff = now - timedelta(seconds=window_seconds)

        # Find best baseline: most recent snapshot AT or BEFORE cutoff
        price_before = None
        for i in range(len(live) - 2, -1, -1):
            snap = live[i]
            if snap.mid_price is not None and snap.timestamp <= cutoff:
                price_before = snap.mid_price
                break

        # Fallback: oldest available snapshot
        if price_before is None:
            for i in range(len(live) - 1):
                if live[i].mid_price is not None:
                    price_before = live[i].mid_price
                    break

        if price_before is None or price_before == 0:
            return None

        pct_change = (price_now - price_before) / price_before
        return (price_before, price_now, pct_change)

    def get_abs_change(self, token_id: str, window_seconds: int) -> Optional[tuple[float, float, float]]:
        """
        Get absolute price change over a time window.

        Returns:
            (price_before, price_now, abs_change) or None if insufficient data.
        """
        result = self.get_price_change(token_id, window_seconds)
        if result is None:
            return None
        price_before, price_now, _ = result
        abs_change = abs(price_now - price_before)
        return (price_before, price_now, abs_change)

    async def backfill_history(self, market_ticker: str) -> int:
        """
        Backfill price history from Kalshi candlestick API.

        Uses 1-hour candles over the configured history window (default 48h).

        Returns:
            Number of data points loaded.
        """
        market = self._markets.get(market_ticker)
        if not market:
            return 0

        try:
            now = datetime.utcnow()
            start = now - timedelta(hours=self.config.price_history_window_hours)
            end_ts = int(now.timestamp())
            start_ts = int(start.timestamp())

            candles = await self._client.get_market_candlesticks(
                ticker=market_ticker,
                start_ts=start_ts,
                end_ts=end_ts,
                period_interval=60,  # 1-hour candles
            )

            if not candles:
                return 0

            count = 0
            for candle in candles:
                mid = candle.mid_price
                if mid is None:
                    continue

                snapshot = PriceSnapshot(
                    timestamp=candle.timestamp,
                    mid_price=mid,
                    best_bid=candle.yes_bid_close,
                    best_ask=candle.yes_ask_close,
                    source="candlestick_history",
                )
                market.history.append(snapshot)
                count += 1

            # Sort by timestamp after backfill
            sorted_history = sorted(market.history, key=lambda s: s.timestamp)
            market.history.clear()
            market.history.extend(sorted_history)

            logger.debug(f"Backfilled {count} candles for {market_ticker}")
            return count

        except Exception as e:
            logger.debug(f"Backfill error for {market_ticker}: {e}")
            return 0

    async def backfill_all(self) -> dict:
        """
        Backfill all watched markets using concurrent batches.

        Returns:
            Stats dict: {"total": N, "backfilled": N, "empty": N, "failed": N}
        """
        stats = {"total": 0, "backfilled": 0, "empty": 0, "failed": 0}
        tickers = list(self._markets.keys())
        stats["total"] = len(tickers)

        if not tickers:
            return stats

        # Concurrent batches of 10 (Kalshi rate limits are stricter than Polymarket)
        batch_size = 10
        for i in range(0, len(tickers), batch_size):
            batch = tickers[i:i + batch_size]

            tasks = [self.backfill_history(t) for t in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    stats["failed"] += 1
                elif isinstance(result, int) and result > 0:
                    stats["backfilled"] += 1
                else:
                    stats["empty"] += 1

            # Rate limit between batches (20 reads/sec at basic tier)
            if i + batch_size < len(tickers):
                await asyncio.sleep(0.5)

        return stats

    def get_stats(self) -> dict:
        """Get tracker statistics."""
        total_snapshots = sum(len(m.history) for m in self._markets.values())
        markets_with_data = sum(1 for m in self._markets.values() if len(m.history) > 0)

        return {
            "markets_watched": len(self._markets),
            "markets_with_data": markets_with_data,
            "total_snapshots": total_snapshots,
        }
