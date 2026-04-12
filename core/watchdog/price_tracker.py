"""
Price Tracker
==============

Maintains rolling price history for watched markets.

Sources:
1. CLOB prices-history endpoint (backfill on startup)
2. BBATracker WebSocket callbacks (live updates, rate-limited)
"""

import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Optional

import httpx

from core.shared.markets.models import MarketEvent, Outcome
from core.watchdog.models import PriceSnapshot, WatchdogConfig

logger = logging.getLogger(__name__)


class WatchedMarket:
    """Rolling price history for a single outcome token."""

    def __init__(self, token_id: str, event_id: str, outcome_name: str,
                 event_title: str, event_slug: str, event_volume_24h: float,
                 max_history_hours: float = 48.0):
        self.token_id = token_id
        self.event_id = event_id
        self.outcome_name = outcome_name
        self.event_title = event_title
        self.event_slug = event_slug
        self.event_volume_24h = event_volume_24h

        # Rolling deque — at 1 sample/10s, 48h = ~17280 entries
        max_samples = int(max_history_hours * 3600 / 10) + 1000  # Some headroom
        self.history: deque[PriceSnapshot] = deque(maxlen=max_samples)

        # Separate live-only history (websocket/clob sources, excludes backfill/gamma).
        # Used by get_price_change() to avoid rebuilding a filtered list on every call.
        self.live_history: deque[PriceSnapshot] = deque(maxlen=max_samples)

        self.last_sample_at: Optional[datetime] = None

        # Gamma API probability — the "display price" shown on Polymarket's site.
        # Updated on each registry refresh. More accurate than mid-price when
        # spreads are wide (e.g. bid=0.001, ask=0.999 → mid=0.5 is misleading).
        self.gamma_price: Optional[float] = None

    @property
    def current_price(self) -> Optional[float]:
        """Get most recent mid-price."""
        if not self.history:
            return None
        return self.history[-1].mid_price

    @property
    def current_snapshot(self) -> Optional[PriceSnapshot]:
        """Get most recent snapshot."""
        if not self.history:
            return None
        return self.history[-1]


class PriceTracker:
    """
    Tracks rolling price history for watched markets.

    Startup: backfills from CLOB prices-history endpoint.
    Live: samples from BBATracker callbacks, rate-limited per token.
    """

    CLOB_URL = "https://clob.polymarket.com"

    def __init__(self, config: WatchdogConfig):
        self.config = config
        self._markets: dict[str, WatchedMarket] = {}  # token_id -> WatchedMarket
        self._http_client: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        """Initialize HTTP client."""
        self._http_client = httpx.AsyncClient(timeout=30.0)

    async def stop(self) -> None:
        """Cleanup HTTP client."""
        if self._http_client:
            await self._http_client.aclose()

    def add_watch(self, event: MarketEvent, outcome: Outcome) -> None:
        """Add an outcome to the watch list."""
        if outcome.token_id in self._markets:
            return

        market = WatchedMarket(
            token_id=outcome.token_id,
            event_id=event.event_id,
            outcome_name=outcome.name,
            event_title=event.title,
            event_slug=event.slug if hasattr(event, 'slug') else "",
            event_volume_24h=event.volume_24h,
            max_history_hours=self.config.price_history_window_hours,
        )
        # Seed gamma price from the initial BBA if source is "gamma"
        if outcome.bba.source == "gamma" and outcome.bba.best_ask is not None:
            market.gamma_price = outcome.bba.best_ask
        self._markets[outcome.token_id] = market

    def remove_watch(self, token_id: str) -> None:
        """Remove an outcome from the watch list."""
        self._markets.pop(token_id, None)

    def get_watched_markets(self) -> dict[str, WatchedMarket]:
        """Get all watched markets."""
        return self._markets

    def sample_price(self, token_id: str, best_bid: Optional[float],
                     best_ask: Optional[float], bid_size: Optional[float] = None,
                     ask_size: Optional[float] = None, source: str = "websocket") -> None:
        """
        Sample a price update into the rolling history.

        Rate-limited to 1 sample per min_sample_interval_seconds per token.
        """
        market = self._markets.get(token_id)
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
        # Also append to live_history for fast anomaly detection lookups.
        # Only live sources (websocket, clob) — not clob_history or gamma.
        if source not in ("clob_history", "gamma"):
            market.live_history.append(snapshot)
        market.last_sample_at = now

    def get_price_change(self, token_id: str, window_seconds: int) -> Optional[tuple[float, float, float]]:
        """
        Get price change over a time window using only live-sourced snapshots.

        Uses the pre-filtered live_history deque (populated at sample time)
        instead of rebuilding a filtered list on every call. This avoids
        O(N) list comprehensions per threshold per market per scan cycle.

        Gap-aware: when there's a data gap (e.g. 12 hours of no quotes),
        finds the last known price BEFORE the window as baseline, rather
        than comparing the current price against itself (which would yield
        0% change and miss the spike entirely).

        Returns:
            (price_before, price_now, pct_change) or None if insufficient data.
            pct_change is a fraction (e.g. 1.79 for 179%).
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

        # Find the best baseline: the most recent snapshot AT or BEFORE the
        # cutoff. This correctly handles data gaps — if a token has no trades
        # for hours and then a sudden buy moves the price 11c, we compare
        # against the last known price before the gap, not the current price.
        price_before = None
        for i in range(len(live) - 2, -1, -1):
            snap = live[i]
            if snap.mid_price is not None and snap.timestamp <= cutoff:
                price_before = snap.mid_price
                break

        # Fallback: if no snapshot at/before cutoff (all data is within the
        # window), use the oldest available snapshot (excluding the latest).
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

    async def backfill_history(self, token_id: str) -> int:
        """
        Backfill price history from CLOB prices-history endpoint.

        Returns:
            Number of data points loaded.
        """
        market = self._markets.get(token_id)
        if not market or not self._http_client:
            return 0

        try:
            resp = await self._http_client.get(
                f"{self.CLOB_URL}/prices-history",
                params={
                    "market": token_id,
                    "interval": "max",
                    "fidelity": 1,
                },
            )

            if resp.status_code != 200:
                logger.debug(f"prices-history returned {resp.status_code} for {token_id[:12]}")
                return 0

            data = resp.json()
            history = data.get("history", [])

            if not history:
                return 0

            # Parse history points
            cutoff = datetime.utcnow() - timedelta(hours=self.config.price_history_window_hours)
            count = 0

            for point in history:
                try:
                    ts = point.get("t")
                    price = point.get("p")

                    if ts is None or price is None:
                        continue

                    # Parse timestamp — CLOB returns epoch seconds
                    if isinstance(ts, (int, float)):
                        timestamp = datetime.utcfromtimestamp(ts)
                    else:
                        continue

                    if timestamp < cutoff:
                        continue

                    snapshot = PriceSnapshot(
                        timestamp=timestamp,
                        mid_price=float(price),
                        source="clob_history",
                    )
                    market.history.append(snapshot)
                    count += 1
                except (ValueError, TypeError):
                    continue

            # Sort by timestamp after backfill
            sorted_history = sorted(market.history, key=lambda s: s.timestamp)
            market.history.clear()
            market.history.extend(sorted_history)

            logger.debug(f"Backfilled {count} price points for {token_id[:12]}")
            return count

        except Exception as e:
            logger.debug(f"Backfill error for {token_id[:12]}: {e}")
            return 0

    async def backfill_all(self) -> dict:
        """
        Backfill all watched markets using concurrent batches.

        Follows the NegriskEngine._seed_bba_data pattern: fires off
        concurrent requests within each batch, then pauses between batches.

        Returns:
            Stats dict: {"total": N, "backfilled": N, "empty": N, "failed": N}
        """
        import asyncio

        stats = {"total": 0, "backfilled": 0, "empty": 0, "failed": 0}
        token_ids = list(self._markets.keys())
        stats["total"] = len(token_ids)

        if not token_ids:
            return stats

        # Concurrent batches of 20 tokens, 0.5s pause between batches
        batch_size = 20
        for i in range(0, len(token_ids), batch_size):
            batch = token_ids[i:i + batch_size]

            # Fire all requests in this batch concurrently
            tasks = [self.backfill_history(tid) for tid in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    stats["failed"] += 1
                elif isinstance(result, int) and result > 0:
                    stats["backfilled"] += 1
                else:
                    stats["empty"] += 1

            # Rate limit between batches
            if i + batch_size < len(token_ids):
                await asyncio.sleep(0.3)

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
