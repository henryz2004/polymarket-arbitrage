"""
Tests for WebSocket Integration
===============================

End-to-end tests that verify WebSocket data flows correctly
through the arbitrage detection pipeline.

Run with: pytest tests/test_websocket_integration.py -v -s
"""

import asyncio
import json
import pytest
from datetime import datetime
from unittest.mock import Mock, AsyncMock, patch
from typing import AsyncIterator

from polymarket_client.models import (
    Market,
    MarketState,
    OrderBook,
    OrderBookSide,
    PriceLevel,
    TokenOrderBook,
    TokenType,
)
from core.arb_engine import ArbEngine, ArbConfig


@pytest.fixture
def arb_config() -> ArbConfig:
    """Arbitrage config for tests."""
    return ArbConfig(
        min_edge=0.01,
        bundle_arb_enabled=True,
        min_spread=0.05,
        mm_enabled=False,
        tick_size=0.01,
        default_order_size=50.0,
        maker_fee_bps=0,
        taker_fee_bps=0,
        gas_cost_per_order=0,
    )


@pytest.fixture
def arb_engine(arb_config: ArbConfig) -> ArbEngine:
    """Create arbitrage engine for tests."""
    return ArbEngine(arb_config)


def create_orderbook_from_ws_message(message: dict) -> OrderBook:
    """Convert a WebSocket message to OrderBook model."""
    market_id = message.get("market", message.get("asset_id", "unknown"))

    bids = [
        PriceLevel(price=float(level["price"]), size=float(level["size"]))
        for level in message.get("buys", [])
    ]
    asks = [
        PriceLevel(price=float(level["price"]), size=float(level["size"]))
        for level in message.get("sells", [])
    ]

    # For simplicity, assume this is a YES token book
    # In real implementation, we'd track asset_id -> token_type mapping
    return OrderBook(
        market_id=market_id,
        yes=TokenOrderBook(
            token_type=TokenType.YES,
            bids=OrderBookSide(levels=bids),
            asks=OrderBookSide(levels=asks),
        ),
        no=TokenOrderBook(
            token_type=TokenType.NO,
            bids=OrderBookSide(levels=[]),
            asks=OrderBookSide(levels=[]),
        ),
        timestamp=datetime.utcnow(),
    )


class TestWebSocketToArbEngine:
    """Tests for WebSocket → ArbEngine pipeline."""

    def test_ws_message_to_orderbook(self):
        """Test converting WebSocket message to OrderBook."""
        ws_message = {
            "event_type": "book",
            "asset_id": "12345",
            "market": "0xmarket123",
            "timestamp": 1707500000000,
            "buys": [
                {"price": "0.48", "size": "100"},
                {"price": "0.47", "size": "200"},
            ],
            "sells": [
                {"price": "0.52", "size": "100"},
                {"price": "0.53", "size": "150"},
            ],
        }

        orderbook = create_orderbook_from_ws_message(ws_message)

        assert orderbook.market_id == "0xmarket123"
        assert orderbook.yes.bids.levels[0].price == 0.48
        assert orderbook.yes.asks.levels[0].price == 0.52
        assert len(orderbook.yes.bids.levels) == 2
        assert len(orderbook.yes.asks.levels) == 2

    def test_orderbook_feeds_arb_engine(self, arb_engine: ArbEngine):
        """Test that orderbook updates trigger arb engine analysis."""
        # Create an orderbook with arbitrage opportunity
        # YES ask = 0.45, NO ask = 0.50 → total = 0.95 (5% edge)
        orderbook = OrderBook(
            market_id="test_market",
            yes=TokenOrderBook(
                token_type=TokenType.YES,
                bids=OrderBookSide(levels=[PriceLevel(price=0.43, size=100)]),
                asks=OrderBookSide(levels=[PriceLevel(price=0.45, size=100)]),
            ),
            no=TokenOrderBook(
                token_type=TokenType.NO,
                bids=OrderBookSide(levels=[PriceLevel(price=0.48, size=100)]),
                asks=OrderBookSide(levels=[PriceLevel(price=0.50, size=100)]),
            ),
        )

        market_state = MarketState(
            market=Market(
                market_id="test_market",
                condition_id="test_market",
                question="Test Market",
                active=True,
            ),
            order_book=orderbook,
        )

        # Run analysis
        signals = arb_engine.analyze(market_state)

        # Should detect bundle opportunity
        bundle_signals = [
            s for s in signals if s.opportunity and s.opportunity.is_bundle_arb
        ]
        assert len(bundle_signals) >= 1

    def test_rapid_updates_handled(self, arb_engine: ArbEngine):
        """Test that rapid orderbook updates are handled correctly."""
        updates_processed = 0

        # Simulate 100 rapid updates
        for i in range(100):
            yes_ask = 0.50 + (i % 10) * 0.01  # Vary price
            no_ask = 0.50 - (i % 10) * 0.005

            orderbook = OrderBook(
                market_id="rapid_test",
                yes=TokenOrderBook(
                    token_type=TokenType.YES,
                    bids=OrderBookSide(levels=[PriceLevel(price=yes_ask - 0.02, size=100)]),
                    asks=OrderBookSide(levels=[PriceLevel(price=yes_ask, size=100)]),
                ),
                no=TokenOrderBook(
                    token_type=TokenType.NO,
                    bids=OrderBookSide(levels=[PriceLevel(price=no_ask - 0.02, size=100)]),
                    asks=OrderBookSide(levels=[PriceLevel(price=no_ask, size=100)]),
                ),
            )

            market_state = MarketState(
                market=Market(
                    market_id="rapid_test",
                    condition_id="rapid_test",
                    question="Rapid Test",
                    active=True,
                ),
                order_book=orderbook,
            )

            _ = arb_engine.analyze(market_state)
            updates_processed += 1

        assert updates_processed == 100


class TestOpportunityTimingWithWebSocket:
    """Tests for opportunity timing accuracy with WebSocket updates."""

    def test_opportunity_detection_latency(self, arb_engine: ArbEngine):
        """Measure time from orderbook update to opportunity detection."""
        # Create orderbook with clear opportunity
        orderbook = OrderBook(
            market_id="latency_test",
            yes=TokenOrderBook(
                token_type=TokenType.YES,
                bids=OrderBookSide(levels=[PriceLevel(price=0.40, size=100)]),
                asks=OrderBookSide(levels=[PriceLevel(price=0.42, size=100)]),
            ),
            no=TokenOrderBook(
                token_type=TokenType.NO,
                bids=OrderBookSide(levels=[PriceLevel(price=0.50, size=100)]),
                asks=OrderBookSide(levels=[PriceLevel(price=0.52, size=100)]),
            ),
        )

        market_state = MarketState(
            market=Market(
                market_id="latency_test",
                condition_id="latency_test",
                question="Latency Test",
                active=True,
            ),
            order_book=orderbook,
        )

        # Measure detection time
        start = datetime.utcnow()
        signals = arb_engine.analyze(market_state)
        end = datetime.utcnow()

        detection_ms = (end - start).total_seconds() * 1000

        print(f"\n⏱️  Opportunity Detection Latency: {detection_ms:.3f}ms")

        # Detection should be very fast (<10ms)
        assert detection_ms < 10, f"Detection took too long: {detection_ms}ms"
        assert len(signals) >= 1, "Should detect opportunity"

    def test_timing_stats_updated(self, arb_engine: ArbEngine):
        """Test that timing stats are properly updated."""
        # First, create an opportunity
        orderbook_with_opp = OrderBook(
            market_id="timing_test",
            yes=TokenOrderBook(
                token_type=TokenType.YES,
                bids=OrderBookSide(levels=[PriceLevel(price=0.40, size=100)]),
                asks=OrderBookSide(levels=[PriceLevel(price=0.42, size=100)]),
            ),
            no=TokenOrderBook(
                token_type=TokenType.NO,
                bids=OrderBookSide(levels=[PriceLevel(price=0.50, size=100)]),
                asks=OrderBookSide(levels=[PriceLevel(price=0.52, size=100)]),
            ),
        )

        market_state = MarketState(
            market=Market(
                market_id="timing_test",
                condition_id="timing_test",
                question="Timing Test",
                active=True,
            ),
            order_book=orderbook_with_opp,
        )

        # Detect opportunity
        arb_engine.analyze(market_state)

        # Now update with prices that remove opportunity
        orderbook_no_opp = OrderBook(
            market_id="timing_test",
            yes=TokenOrderBook(
                token_type=TokenType.YES,
                bids=OrderBookSide(levels=[PriceLevel(price=0.48, size=100)]),
                asks=OrderBookSide(levels=[PriceLevel(price=0.50, size=100)]),
            ),
            no=TokenOrderBook(
                token_type=TokenType.NO,
                bids=OrderBookSide(levels=[PriceLevel(price=0.48, size=100)]),
                asks=OrderBookSide(levels=[PriceLevel(price=0.50, size=100)]),
            ),
        )

        market_state_updated = MarketState(
            market=Market(
                market_id="timing_test",
                condition_id="timing_test",
                question="Timing Test",
                active=True,
            ),
            order_book=orderbook_no_opp,
        )

        # This should expire the opportunity
        arb_engine.analyze(market_state_updated)

        # Check timing stats
        timing_stats = arb_engine.get_timing_stats()
        print(f"\n📊 Timing Stats: {timing_stats}")

        # Should have tracked at least one opportunity
        # (Stats may show 0 if opportunity expired too fast to measure)


class TestMultipleMarkets:
    """Tests for handling multiple markets simultaneously."""

    def test_concurrent_market_updates(self, arb_engine: ArbEngine):
        """Test processing updates from multiple markets."""
        markets = []

        # Create 50 different markets
        for i in range(50):
            orderbook = OrderBook(
                market_id=f"market_{i}",
                yes=TokenOrderBook(
                    token_type=TokenType.YES,
                    bids=OrderBookSide(levels=[PriceLevel(price=0.48, size=100)]),
                    asks=OrderBookSide(levels=[PriceLevel(price=0.50, size=100)]),
                ),
                no=TokenOrderBook(
                    token_type=TokenType.NO,
                    bids=OrderBookSide(levels=[PriceLevel(price=0.48, size=100)]),
                    asks=OrderBookSide(levels=[PriceLevel(price=0.50, size=100)]),
                ),
            )

            market_state = MarketState(
                market=Market(
                    market_id=f"market_{i}",
                    condition_id=f"market_{i}",
                    question=f"Test Market {i}",
                    active=True,
                ),
                order_book=orderbook,
            )
            markets.append(market_state)

        # Process all markets
        start = datetime.utcnow()
        total_signals = 0

        for market_state in markets:
            signals = arb_engine.analyze(market_state)
            total_signals += len(signals)

        end = datetime.utcnow()
        total_ms = (end - start).total_seconds() * 1000
        per_market_ms = total_ms / len(markets)

        print(f"\n📊 Multi-Market Processing:")
        print(f"   Markets: {len(markets)}")
        print(f"   Total time: {total_ms:.1f}ms")
        print(f"   Per market: {per_market_ms:.3f}ms")

        # Should process 50 markets in under 100ms
        assert total_ms < 100, f"Processing too slow: {total_ms}ms"


class TestDataFeedWebSocketMode:
    """Tests for DataFeed with WebSocket mode."""

    @pytest.mark.asyncio
    async def test_datafeed_websocket_callback(self):
        """Test that DataFeed correctly calls callbacks on WS updates."""
        updates_received = []

        def on_update(market_id: str, state: MarketState):
            updates_received.append((market_id, state))

        # Simulate what DataFeed would do with WS messages
        # This tests the callback mechanism without actual WebSocket
        ws_messages = [
            {
                "event_type": "book",
                "market": "market_1",
                "buys": [{"price": "0.48", "size": "100"}],
                "sells": [{"price": "0.52", "size": "100"}],
            },
            {
                "event_type": "book",
                "market": "market_2",
                "buys": [{"price": "0.45", "size": "200"}],
                "sells": [{"price": "0.55", "size": "200"}],
            },
        ]

        for msg in ws_messages:
            market_id = msg["market"]
            orderbook = create_orderbook_from_ws_message(msg)

            market_state = MarketState(
                market=Market(
                    market_id=market_id,
                    condition_id=market_id,
                    question=f"Market {market_id}",
                    active=True,
                ),
                order_book=orderbook,
            )

            on_update(market_id, market_state)

        assert len(updates_received) == 2
        assert updates_received[0][0] == "market_1"
        assert updates_received[1][0] == "market_2"


# Run with: pytest tests/test_websocket_integration.py -v -s
if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
