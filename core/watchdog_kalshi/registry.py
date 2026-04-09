"""
Kalshi Watchdog Registry
=========================

Discovers Kalshi events to watch using keyword/category/volume filters.

Uses:
- GET /events/multivariate (multi-outcome events with nested markets)
- GET /markets (all open markets for binary events)

Refreshes periodically to pick up new events.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from kalshi_client.api import KalshiClient
from kalshi_client.models import KalshiEvent, KalshiMarket
from core.watchdog_kalshi.models import KalshiWatchdogConfig

logger = logging.getLogger(__name__)


class KalshiRegistry:
    """
    Event discovery for the Kalshi watchdog.

    Discovers both multivariate (multi-outcome) and binary events
    that match the watchdog's keyword/category/volume filters.
    """

    def __init__(self, config: KalshiWatchdogConfig, client: KalshiClient):
        self.config = config
        self._client = client

        # Discovered events: event_ticker -> KalshiEvent
        self._events: dict[str, KalshiEvent] = {}

        # All watched markets: market_ticker -> KalshiMarket
        self._markets: dict[str, KalshiMarket] = {}

        # Background refresh
        self._refresh_task: Optional[asyncio.Task] = None
        self._running = False
        self._refresh_count = 0

    async def start(self) -> None:
        """Start the registry with initial fetch + background refresh."""
        if self._running:
            return
        self._running = True

        # Initial discovery
        await self._refresh()

        # Background refresh loop
        self._refresh_task = asyncio.create_task(
            self._refresh_loop(), name="kalshi_registry_refresh"
        )

    async def stop(self) -> None:
        """Stop background refresh."""
        self._running = False
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass

    async def _refresh_loop(self) -> None:
        """Periodically refresh event discovery."""
        while self._running:
            try:
                await asyncio.sleep(self.config.registry_refresh_seconds)
                await self._refresh()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Registry refresh error: {e}")
                await asyncio.sleep(10)

    async def _refresh(self) -> None:
        """Fetch events from Kalshi API and apply filters."""
        self._refresh_count += 1

        # Fetch multivariate events (multi-outcome, like Polymarket neg-risk)
        try:
            mv_events = await self._client.get_all_multivariate_events(
                with_nested_markets=True,
                max_events=2000,
            )
        except Exception as e:
            logger.warning(f"Failed to fetch multivariate events: {e}")
            mv_events = []

        # Fetch regular (binary) events if keyword-matched
        # For now, we also pull open markets and group by event_ticker
        try:
            markets, _ = await self._client.list_markets(
                status="open", limit=1000
            )
        except Exception as e:
            logger.warning(f"Failed to fetch markets: {e}")
            markets = []

        # Build event map from multivariate events
        new_events: dict[str, KalshiEvent] = {}
        for event in mv_events:
            if self._should_watch_event(event):
                new_events[event.event_ticker] = event

        # Group binary markets by event_ticker and check if they match
        binary_groups: dict[str, list[KalshiMarket]] = {}
        for market in markets:
            if market.event_ticker not in new_events:
                if market.event_ticker not in binary_groups:
                    binary_groups[market.event_ticker] = []
                binary_groups[market.event_ticker].append(market)

        for event_ticker, event_markets in binary_groups.items():
            if not event_markets:
                continue
            first = event_markets[0]
            # Build a pseudo-event from grouped markets
            pseudo_event = KalshiEvent(
                event_ticker=event_ticker,
                series_ticker=first.series_ticker,
                title=first.title,
                category=first.category,
                markets=event_markets,
            )
            if self._should_watch_event(pseudo_event):
                new_events[event_ticker] = pseudo_event

        # Update state
        self._events = new_events

        # Flatten all watched markets
        new_markets: dict[str, KalshiMarket] = {}
        for event in new_events.values():
            for market in event.markets:
                if market.is_active:
                    new_markets[market.ticker] = market
        self._markets = new_markets

        logger.info(
            f"Registry refreshed: {len(self._events)} events, "
            f"{len(self._markets)} markets"
        )

    def _should_watch_event(self, event: KalshiEvent) -> bool:
        """Check if an event matches watch criteria."""
        # Force-watch by event ticker
        if event.event_ticker in self.config.watch_event_tickers:
            return True

        # Force-watch by series ticker
        if event.series_ticker in self.config.watch_series_tickers:
            return True

        # Check category
        if self.config.watch_categories:
            if event.category.lower() in [c.lower() for c in self.config.watch_categories]:
                # Category matches — check volume
                total_volume = sum(m.volume for m in event.markets)
                if total_volume >= self.config.min_event_volume_24h:
                    return True

        # Check keyword match in event title
        title_lower = event.title.lower()
        for keyword in self.config.watch_keywords:
            if keyword.lower() in title_lower:
                # Keyword matches — check volume
                total_volume = sum(m.volume for m in event.markets)
                if total_volume >= self.config.min_event_volume_24h:
                    return True

        return False

    def get_all_events(self) -> list[KalshiEvent]:
        """Get all watched events."""
        return list(self._events.values())

    def get_all_markets(self) -> dict[str, KalshiMarket]:
        """Get all watched markets (market_ticker -> KalshiMarket)."""
        return self._markets

    def get_event(self, event_ticker: str) -> Optional[KalshiEvent]:
        """Get a specific event by ticker."""
        return self._events.get(event_ticker)

    def get_market(self, market_ticker: str) -> Optional[KalshiMarket]:
        """Get a specific market by ticker."""
        return self._markets.get(market_ticker)

    def get_event_for_market(self, market_ticker: str) -> Optional[KalshiEvent]:
        """Find which event a market belongs to."""
        market = self._markets.get(market_ticker)
        if not market:
            return None
        return self._events.get(market.event_ticker)

    def get_stats(self) -> dict:
        """Get registry statistics."""
        return {
            "events_watched": len(self._events),
            "markets_watched": len(self._markets),
            "refresh_count": self._refresh_count,
        }
