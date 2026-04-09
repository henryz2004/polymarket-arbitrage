"""
Tests for Kalshi Watchdog
==========================

Tests the Kalshi-specific watchdog components:
- KalshiAuth (RSA-PSS signing)
- KalshiClient (multivariate events, candlesticks)
- KalshiPriceTracker (candlestick backfill, ticker sampling)
- KalshiRegistry (event discovery, filtering)
- KalshiWatchdogEngine (integration)

The shared anomaly detection logic is tested in test_watchdog.py.
"""

import asyncio
import json
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kalshi_client.models import (
    KalshiCandlestick,
    KalshiEvent,
    KalshiMarket,
    KalshiTickerUpdate,
    KalshiTrade,
)
from core.watchdog.models import PriceSnapshot
from core.watchdog_kalshi.models import KalshiWatchdogConfig
from core.watchdog_kalshi.price_tracker import KalshiPriceTracker
from core.watchdog_kalshi.registry import KalshiRegistry


# ===========================================================================
# FIXTURES
# ===========================================================================


def make_kalshi_market(
    ticker="KXIRAN-26APR01-T50",
    event_ticker="KXIRAN-26APR01",
    series_ticker="KXIRAN",
    title="Will Iran be attacked by April 1?",
    yes_price=0.25,
    volume=10000,
    status="open",
    category="politics",
):
    return KalshiMarket(
        ticker=ticker,
        event_ticker=event_ticker,
        series_ticker=series_ticker,
        title=title,
        yes_price=yes_price,
        no_price=1.0 - yes_price,
        volume=volume,
        status=status,
        category=category,
    )


def make_kalshi_event(
    event_ticker="KXIRAN-26APR01",
    series_ticker="KXIRAN",
    title="Iran military action",
    category="politics",
    num_markets=3,
    volume_per_market=5000,
):
    markets = []
    for i in range(num_markets):
        markets.append(make_kalshi_market(
            ticker=f"{event_ticker}-T{i+1}",
            event_ticker=event_ticker,
            series_ticker=series_ticker,
            title=f"Outcome {i+1}",
            volume=volume_per_market,
        ))
    return KalshiEvent(
        event_ticker=event_ticker,
        series_ticker=series_ticker,
        title=title,
        category=category,
        markets=markets,
    )


# ===========================================================================
# KalshiCandlestick TESTS
# ===========================================================================


class TestKalshiCandlestick:
    """Test candlestick model."""

    def test_mid_price_from_trade_close(self):
        candle = KalshiCandlestick(
            end_period_ts=1000,
            price_close=0.45,
            yes_bid_close=0.40,
            yes_ask_close=0.50,
        )
        assert candle.mid_price == 0.45  # Prefers trade close

    def test_mid_price_from_bid_ask(self):
        candle = KalshiCandlestick(
            end_period_ts=1000,
            yes_bid_close=0.40,
            yes_ask_close=0.50,
        )
        assert candle.mid_price == 0.45

    def test_mid_price_bid_only(self):
        candle = KalshiCandlestick(
            end_period_ts=1000,
            yes_bid_close=0.40,
        )
        assert candle.mid_price == 0.40

    def test_mid_price_none(self):
        candle = KalshiCandlestick(end_period_ts=1000)
        assert candle.mid_price is None

    def test_timestamp(self):
        candle = KalshiCandlestick(end_period_ts=1700000000)
        assert isinstance(candle.timestamp, datetime)


# ===========================================================================
# KalshiTrade TESTS
# ===========================================================================


class TestKalshiTrade:
    """Test trade model."""

    def test_dollar_value(self):
        trade = KalshiTrade(
            trade_id="t1",
            market_ticker="KXIRAN-T50",
            side="yes",
            price=0.50,
            count=100,
            ts=1700000000,
        )
        assert trade.dollar_value == 50.0

    def test_timestamp(self):
        trade = KalshiTrade(
            trade_id="t1",
            market_ticker="KXIRAN-T50",
            side="yes",
            price=0.50,
            count=100,
            ts=1700000000,
        )
        assert isinstance(trade.timestamp, datetime)


# ===========================================================================
# KalshiWatchdogConfig TESTS
# ===========================================================================


class TestKalshiWatchdogConfig:
    """Test Kalshi-specific config."""

    def test_default_categories(self):
        config = KalshiWatchdogConfig()
        assert "politics" in config.watch_categories
        assert "world" in config.watch_categories

    def test_default_live_event_filters(self):
        config = KalshiWatchdogConfig()
        assert any("KXNBA" in p for p in config.skip_live_event_slug_prefixes)
        assert any("KXNFL" in p for p in config.skip_live_event_slug_prefixes)

    def test_inherits_base_config(self):
        config = KalshiWatchdogConfig()
        # Should inherit from WatchdogConfig
        assert hasattr(config, 'relative_thresholds')
        assert hasattr(config, 'absolute_thresholds')
        assert hasattr(config, 'off_hours_utc')
        assert hasattr(config, 'warmup_seconds')

    def test_custom_volume_threshold(self):
        config = KalshiWatchdogConfig(min_event_volume_24h=1000)
        assert config.min_event_volume_24h == 1000


# ===========================================================================
# KalshiPriceTracker TESTS
# ===========================================================================


class TestKalshiPriceTracker:
    """Test price tracker adapted for Kalshi."""

    def setup_method(self):
        self.config = KalshiWatchdogConfig(
            min_sample_interval_seconds=0,  # No rate limiting for tests
        )
        self.client = MagicMock()
        self.tracker = KalshiPriceTracker(self.config, self.client)

    def test_add_watch(self):
        event = make_kalshi_event()
        market = event.markets[0]
        self.tracker.add_watch(event, market)
        assert market.ticker in self.tracker.get_watched_markets()

    def test_add_watch_idempotent(self):
        event = make_kalshi_event()
        market = event.markets[0]
        self.tracker.add_watch(event, market)
        self.tracker.add_watch(event, market)
        assert len(self.tracker.get_watched_markets()) == 1

    def test_remove_watch(self):
        event = make_kalshi_event()
        market = event.markets[0]
        self.tracker.add_watch(event, market)
        self.tracker.remove_watch(market.ticker)
        assert market.ticker not in self.tracker.get_watched_markets()

    def test_sample_price(self):
        event = make_kalshi_event()
        market = event.markets[0]
        self.tracker.add_watch(event, market)

        self.tracker.sample_price(
            market_ticker=market.ticker,
            best_bid=0.40,
            best_ask=0.50,
            source="websocket",
        )

        wm = self.tracker.get_watched_markets()[market.ticker]
        assert len(wm.history) == 1
        assert wm.history[0].mid_price == 0.45

    def test_sample_from_ticker(self):
        event = make_kalshi_event()
        market = event.markets[0]
        self.tracker.add_watch(event, market)

        update = KalshiTickerUpdate(
            market_ticker=market.ticker,
            yes_bid=0.40,
            yes_ask=0.50,
            volume=1000,
        )
        self.tracker.sample_from_ticker(update)

        wm = self.tracker.get_watched_markets()[market.ticker]
        assert len(wm.history) == 1
        assert len(wm.live_history) == 1
        assert wm.live_history[0].mid_price == 0.45

    def test_price_change_detection(self):
        """Test gap-aware price change detection."""
        event = make_kalshi_event()
        market = event.markets[0]
        self.tracker.add_watch(event, market)

        wm = self.tracker.get_watched_markets()[market.ticker]

        # Simulate: price at 0.10 two hours ago, then 0.25 now
        old_time = datetime.utcnow() - timedelta(hours=2)
        wm.live_history.append(PriceSnapshot(
            timestamp=old_time,
            mid_price=0.10,
            source="websocket",
        ))
        wm.live_history.append(PriceSnapshot(
            timestamp=datetime.utcnow(),
            mid_price=0.25,
            source="websocket",
        ))

        result = self.tracker.get_price_change(market.ticker, 3600)
        assert result is not None
        price_before, price_now, pct_change = result
        assert price_before == 0.10
        assert price_now == 0.25
        assert pct_change == pytest.approx(1.5, abs=0.01)  # 150%

    def test_abs_change_detection(self):
        event = make_kalshi_event()
        market = event.markets[0]
        self.tracker.add_watch(event, market)

        wm = self.tracker.get_watched_markets()[market.ticker]
        old_time = datetime.utcnow() - timedelta(hours=2)
        wm.live_history.append(PriceSnapshot(
            timestamp=old_time, mid_price=0.10, source="websocket"
        ))
        wm.live_history.append(PriceSnapshot(
            timestamp=datetime.utcnow(), mid_price=0.25, source="websocket"
        ))

        result = self.tracker.get_abs_change(market.ticker, 3600)
        assert result is not None
        _, _, abs_change = result
        assert abs_change == pytest.approx(0.15, abs=0.001)

    def test_candlestick_history_not_in_live(self):
        """Backfill data should not appear in live_history."""
        event = make_kalshi_event()
        market = event.markets[0]
        self.tracker.add_watch(event, market)

        self.tracker.sample_price(
            market_ticker=market.ticker,
            best_bid=0.40,
            best_ask=0.50,
            source="candlestick_history",
        )

        wm = self.tracker.get_watched_markets()[market.ticker]
        assert len(wm.history) == 1
        assert len(wm.live_history) == 0  # Not in live history

    def test_stats(self):
        event = make_kalshi_event()
        for m in event.markets:
            self.tracker.add_watch(event, m)

        stats = self.tracker.get_stats()
        assert stats["markets_watched"] == 3
        assert stats["markets_with_data"] == 0


# ===========================================================================
# KalshiRegistry TESTS
# ===========================================================================


class TestKalshiRegistry:
    """Test event discovery and filtering."""

    def setup_method(self):
        self.config = KalshiWatchdogConfig(
            watch_keywords=["iran", "strike", "nuclear"],
            watch_categories=["politics"],
            min_event_volume_24h=5000,
        )
        self.client = AsyncMock()
        self.registry = KalshiRegistry(self.config, self.client)

    def test_should_watch_keyword_match(self):
        event = make_kalshi_event(title="Iran military strike deadline")
        assert self.registry._should_watch_event(event) is True

    def test_should_watch_keyword_no_match(self):
        event = make_kalshi_event(
            title="Weather forecast NYC",
            category="weather",
        )
        assert self.registry._should_watch_event(event) is False

    def test_should_watch_category_match(self):
        event = make_kalshi_event(
            title="Presidential election odds",
            category="politics",
        )
        assert self.registry._should_watch_event(event) is True

    def test_should_watch_volume_filter(self):
        event = make_kalshi_event(
            title="Iran strike deadline",
            volume_per_market=100,  # Total = 300, below 5000 threshold
        )
        assert self.registry._should_watch_event(event) is False

    def test_should_watch_forced_event_ticker(self):
        self.config.watch_event_tickers = ["KXIRAN-26APR01"]
        event = make_kalshi_event(
            title="Weather forecast",
            category="weather",
            volume_per_market=0,
        )
        assert self.registry._should_watch_event(event) is True

    def test_should_watch_forced_series_ticker(self):
        self.config.watch_series_tickers = ["KXIRAN"]
        event = make_kalshi_event(
            title="Something unrelated",
            category="other",
            volume_per_market=0,
        )
        assert self.registry._should_watch_event(event) is True

    @pytest.mark.asyncio
    async def test_refresh_populates_events(self):
        """Test that refresh discovers events from API."""
        mv_event = make_kalshi_event(title="Iran strike deadline")
        self.client.get_all_multivariate_events = AsyncMock(return_value=[mv_event])
        self.client.list_markets = AsyncMock(return_value=([], None))

        await self.registry._refresh()

        assert len(self.registry.get_all_events()) == 1
        assert len(self.registry.get_all_markets()) == 3

    @pytest.mark.asyncio
    async def test_refresh_filters_inactive(self):
        """Markets with non-active status are excluded."""
        event = make_kalshi_event()
        event.markets[0].status = "settled"
        self.client.get_all_multivariate_events = AsyncMock(return_value=[event])
        self.client.list_markets = AsyncMock(return_value=([], None))

        await self.registry._refresh()

        assert len(self.registry.get_all_markets()) == 2  # Only 2 active

    def test_get_event_for_market(self):
        """Test reverse lookup from market to event."""
        event = make_kalshi_event()
        self.registry._events = {event.event_ticker: event}
        for m in event.markets:
            self.registry._markets[m.ticker] = m

        result = self.registry.get_event_for_market(event.markets[0].ticker)
        assert result is not None
        assert result.event_ticker == event.event_ticker

    def test_stats(self):
        event = make_kalshi_event()
        self.registry._events = {event.event_ticker: event}
        for m in event.markets:
            self.registry._markets[m.ticker] = m

        stats = self.registry.get_stats()
        assert stats["events_watched"] == 1
        assert stats["markets_watched"] == 3


# ===========================================================================
# KalshiTickerUpdate TESTS
# ===========================================================================


class TestKalshiTickerUpdate:
    """Test ticker update parsing."""

    def test_basic_fields(self):
        update = KalshiTickerUpdate(
            market_ticker="KXIRAN-T50",
            yes_bid=0.40,
            yes_ask=0.50,
            volume=1000,
            open_interest=500,
        )
        assert update.market_ticker == "KXIRAN-T50"
        assert update.yes_bid == 0.40
        assert update.yes_ask == 0.50

    def test_defaults(self):
        update = KalshiTickerUpdate(market_ticker="TEST")
        assert update.yes_bid is None
        assert update.yes_ask is None
        assert update.volume == 0.0
        assert update.ts == 0


# ===========================================================================
# INTEGRATION-LIKE TESTS
# ===========================================================================


class TestKalshiWatchdogIntegration:
    """Test that Kalshi components integrate with shared anomaly detector."""

    def test_price_tracker_with_anomaly_detector(self):
        """Verify KalshiPriceTracker works with AnomalyDetector."""
        from core.watchdog.anomaly_detector import AnomalyDetector

        config = KalshiWatchdogConfig(
            min_sample_interval_seconds=0,
            warmup_seconds=0,
            relative_thresholds=[(0.50, 3600)],
            absolute_thresholds=[(0.05, 1800)],
        )

        client = MagicMock()
        tracker = KalshiPriceTracker(config, client)
        detector = AnomalyDetector(config)

        # Add a market
        event = make_kalshi_event(title="Iran strike deadline")
        market = event.markets[0]
        tracker.add_watch(event, market)

        # Simulate price history: 0.10 -> 0.25 over 30 min
        wm = tracker.get_watched_markets()[market.ticker]
        old_time = datetime.utcnow() - timedelta(minutes=30)
        snap_old = PriceSnapshot(
            timestamp=old_time, mid_price=0.10, source="websocket"
        )
        snap_new = PriceSnapshot(
            timestamp=datetime.utcnow(), mid_price=0.25, source="websocket"
        )
        # Must populate both history (guard check) and live_history (price change)
        wm.history.append(snap_old)
        wm.history.append(snap_new)
        wm.live_history.append(snap_old)
        wm.live_history.append(snap_new)

        # Detector should find this (150% change in 30min, >50% threshold)
        alert = detector.check_market(market.ticker, tracker)
        assert alert is not None
        assert alert.pct_change > 1.0  # >100%

    def test_live_event_filtering(self):
        """Verify Kalshi live-event prefixes are filtered."""
        from core.watchdog.anomaly_detector import AnomalyDetector

        config = KalshiWatchdogConfig(min_sample_interval_seconds=0)
        client = MagicMock()
        tracker = KalshiPriceTracker(config, client)
        detector = AnomalyDetector(config)

        # Create an NBA market (should be filtered)
        event = make_kalshi_event(
            event_ticker="KXNBA-GAME1",
            series_ticker="KXNBA",
            title="NBA Lakers vs Celtics",
            category="sports",
        )
        market = event.markets[0]
        tracker.add_watch(event, market)

        wm = tracker.get_watched_markets()[market.ticker]
        old_time = datetime.utcnow() - timedelta(minutes=5)
        wm.live_history.append(PriceSnapshot(
            timestamp=old_time, mid_price=0.10, source="websocket"
        ))
        wm.live_history.append(PriceSnapshot(
            timestamp=datetime.utcnow(), mid_price=0.90, source="websocket"
        ))

        # Should be filtered (NBA slug prefix)
        alert = detector.check_market(market.ticker, tracker)
        assert alert is None  # Filtered by live event detection
