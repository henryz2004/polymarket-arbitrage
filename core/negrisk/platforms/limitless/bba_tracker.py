"""
Limitless Exchange BBA Tracker
=================================

Real-time Best Bid/Ask tracking for Limitless neg-risk outcomes.

Strategy:
- REST seeding: GET /markets/{slug}/orderbook for initial prices
- REST polling: Periodic orderbook refresh (every 2s) as primary price source
- WebSocket: Socket.IO connection for real-time updates (when available)

Follows the same lifecycle pattern as Polymarket's BBATracker:
start()/stop(), reconnection, staleness tracking.
"""

import asyncio
import logging
from datetime import datetime
from typing import Callable, Optional

from core.negrisk.models import NegriskConfig, NegriskEvent, PriceLevel
from core.negrisk.platforms.limitless.api_client import LimitlessAPIClient


logger = logging.getLogger(__name__)


class LimitlessBBATracker:
    """
    BBA tracker for Limitless Exchange.

    Satisfies BBATrackerProtocol via structural subtyping.

    Uses REST polling as the primary price source. The Limitless orderbook
    endpoint returns bids/asks with price and size (in raw token units).
    Sizes are converted from raw units (6 decimals for USDC) to dollar amounts.
    """

    # USDC on Base has 6 decimals
    USDC_DECIMALS = 6

    def __init__(
        self,
        registry,
        config: NegriskConfig,
        on_price_update: Optional[Callable[[str, str], None]] = None,
        api_client: Optional[LimitlessAPIClient] = None,
    ):
        self._registry = registry
        self._config = config
        self.on_price_update = on_price_update
        self._api_client = api_client or LimitlessAPIClient()
        self._owns_client = api_client is None

        self._running = False
        self._poll_task: Optional[asyncio.Task] = None

        # WebSocket state (for protocol compat)
        self.ws_connected: bool = False
        self.last_ws_message_at: Optional[datetime] = None

        # Stats
        self._rest_fetches: int = 0
        self._empty_books: int = 0
        self._fetch_errors: int = 0
        self._phantom_filtered: int = 0
        self._last_fetch: Optional[datetime] = None

    async def start(self) -> None:
        """Start the BBA tracker."""
        if self._running:
            return

        self._running = True

        if self._owns_client:
            await self._api_client.start()

        # Start REST polling loop
        self._poll_task = asyncio.create_task(
            self._poll_loop(),
            name="limitless_bba_poll",
        )

        # Mark as "connected" since REST is always available
        self.ws_connected = True

        logger.info("Limitless BBA Tracker started (REST polling)")

    async def stop(self) -> None:
        """Stop the BBA tracker."""
        self._running = False
        self.ws_connected = False

        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

        if self._owns_client:
            await self._api_client.stop()

        logger.info("Limitless BBA Tracker stopped")

    async def _poll_loop(self) -> None:
        """
        Poll orderbooks for all tracked outcomes periodically.

        Cycles through all events, fetching orderbooks for each sub-market.
        """
        while self._running:
            try:
                events = self._registry.get_tradeable_events()
                if events:
                    for event in events:
                        if not self._running:
                            break
                        await self._fetch_event_orderbooks(event)
                        # Small delay between events to avoid rate limiting
                        await asyncio.sleep(0.1)

                # Poll interval — faster than Polymarket registry refresh since
                # this is our primary price source (no WebSocket)
                await asyncio.sleep(2.0)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Limitless poll loop error: {e}")
                await asyncio.sleep(5)

    async def _fetch_event_orderbooks(self, event: NegriskEvent) -> None:
        """Fetch orderbooks for all outcomes in an event."""
        for outcome in event.active_outcomes:
            if not outcome.market_id or not self._running:
                continue

            try:
                data = await self._api_client.get_orderbook(outcome.market_id)
                self._rest_fetches += 1
                self._last_fetch = datetime.utcnow()
                self.last_ws_message_at = self._last_fetch

                # Filter phantom/placeholder levels (market-maker minSize stubs)
                min_size_raw = int(data.get("minSize", 0) or 0)
                raw_bids = data.get("bids", [])
                raw_asks = data.get("asks", [])
                bids = self._filter_phantom_levels(raw_bids, min_size_raw)
                asks = self._filter_phantom_levels(raw_asks, min_size_raw)

                if len(bids) < len(raw_bids) or len(asks) < len(raw_asks):
                    self._phantom_filtered += (
                        len(raw_bids) - len(bids) + len(raw_asks) - len(asks)
                    )

                best_bid = float(bids[0]["price"]) if bids else None
                best_ask = float(asks[0]["price"]) if asks else None

                # Convert sizes from raw USDC (6 decimals) to dollar amounts
                bid_size = self._raw_to_usd(bids[0]["size"]) if bids else None
                ask_size = self._raw_to_usd(asks[0]["size"]) if asks else None

                has_book = bool(bids or asks)
                if not has_book:
                    self._empty_books += 1

                # Parse full depth
                max_levels = self._config.max_book_levels
                bid_levels = [
                    PriceLevel(price=float(b["price"]), size=self._raw_to_usd(b["size"]))
                    for b in bids[:max_levels]
                ]
                ask_levels = [
                    PriceLevel(price=float(a["price"]), size=self._raw_to_usd(a["size"]))
                    for a in asks[:max_levels]
                ]

                # Update registry
                self._registry.update_outcome_bba(
                    token_id=outcome.token_id,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    bid_size=bid_size,
                    ask_size=ask_size,
                    source="clob",
                    bid_levels=bid_levels,
                    ask_levels=ask_levels,
                )

                # Trigger callback
                if self.on_price_update and has_book:
                    self.on_price_update(event.event_id, outcome.token_id)

            except Exception as e:
                self._fetch_errors += 1
                logger.debug(f"Limitless orderbook fetch error for {outcome.market_id}: {e}")

    def _raw_to_usd(self, raw_size) -> float:
        """Convert raw token size (6 decimal USDC) to dollar amount."""
        try:
            return float(raw_size) / (10 ** self.USDC_DECIMALS)
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _filter_phantom_levels(levels: list[dict], min_size_raw: int) -> list[dict]:
        """
        Filter out phantom/placeholder levels from the orderbook.

        Market makers on Limitless are required to maintain orders at minSize.
        Levels at exactly minSize are stub obligations, not real liquidity.
        We keep levels that are strictly above minSize.
        """
        if not min_size_raw:
            return levels
        return [lv for lv in levels if int(lv.get("size", 0)) > min_size_raw]

    async def fetch_all_prices(self, event: NegriskEvent) -> dict:
        """
        Fetch fresh prices from orderbook for all outcomes in an event.

        Returns:
            Dict with stats: {"seeded": N, "empty": N, "failed": N}
        """
        stats = {"seeded": 0, "empty": 0, "failed": 0}

        for outcome in event.active_outcomes:
            if not outcome.market_id:
                continue
            try:
                data = await self._api_client.get_orderbook(outcome.market_id)
                self._rest_fetches += 1

                # Filter phantom/placeholder levels
                min_size_raw = int(data.get("minSize", 0) or 0)
                bids = self._filter_phantom_levels(data.get("bids", []), min_size_raw)
                asks = self._filter_phantom_levels(data.get("asks", []), min_size_raw)

                best_bid = float(bids[0]["price"]) if bids else None
                best_ask = float(asks[0]["price"]) if asks else None
                bid_size = self._raw_to_usd(bids[0]["size"]) if bids else None
                ask_size = self._raw_to_usd(asks[0]["size"]) if asks else None

                has_book = bool(bids or asks)

                max_levels = self._config.max_book_levels
                bid_levels = [
                    PriceLevel(price=float(b["price"]), size=self._raw_to_usd(b["size"]))
                    for b in bids[:max_levels]
                ]
                ask_levels = [
                    PriceLevel(price=float(a["price"]), size=self._raw_to_usd(a["size"]))
                    for a in asks[:max_levels]
                ]

                self._registry.update_outcome_bba(
                    token_id=outcome.token_id,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    bid_size=bid_size,
                    ask_size=ask_size,
                    source="clob",
                    bid_levels=bid_levels,
                    ask_levels=ask_levels,
                )

                if has_book:
                    stats["seeded"] += 1
                else:
                    stats["empty"] += 1

            except Exception as e:
                stats["failed"] += 1
                logger.debug(f"Limitless seed error for {outcome.market_id}: {e}")

            # Rate limiting between individual fetches
            await asyncio.sleep(0.05)

        return stats

    def get_gamma_only_tokens(self) -> list[str]:
        """Get token IDs that still have only API-sourced prices."""
        gamma_tokens = []
        for event in self._registry.get_all_events():
            for outcome in event.active_outcomes:
                if outcome.token_id and outcome.bba.source == "gamma":
                    gamma_tokens.append(outcome.token_id)
        return gamma_tokens

    def get_stats(self) -> dict:
        """Get tracker statistics."""
        return {
            "platform": "limitless",
            "rest_fetches": self._rest_fetches,
            "empty_books": self._empty_books,
            "fetch_errors": self._fetch_errors,
            "phantom_filtered": self._phantom_filtered,
            "last_fetch": self._last_fetch.isoformat() if self._last_fetch else None,
            "ws_connected": self.ws_connected,
        }
