"""
Tests for WebSocket Message Parsing
===================================

Unit tests for parsing WebSocket messages from Polymarket.
These tests don't require network access - they test message parsing logic.

Run with: pytest tests/test_websocket_messages.py -v
"""

import pytest
import json
from datetime import datetime
from typing import Optional

from polymarket_client.models import (
    OrderBook,
    OrderBookSide,
    PriceLevel,
    TokenOrderBook,
    TokenType,
)


# Sample WebSocket messages based on actual Polymarket WebSocket responses
# NOTE: Messages come wrapped in a list, e.g. [{"event_type": "book", ...}]
# These samples show the unwrapped individual message format

SAMPLE_BOOK_MESSAGE = {
    "event_type": "book",
    "asset_id": "12345678901234567890",
    "market": "0xabc123",
    "timestamp": "1707500000000",  # String, not int
    "hash": "0xdef456",
    # NOTE: Uses "bids"/"asks" not "buys"/"sells"
    "bids": [
        {"price": "0.48", "size": "150.5"},
        {"price": "0.47", "size": "200.0"},
        {"price": "0.45", "size": "500.0"},
    ],
    "asks": [
        {"price": "0.52", "size": "100.0"},
        {"price": "0.53", "size": "250.0"},
        {"price": "0.55", "size": "300.0"},
    ],
}

SAMPLE_PRICE_CHANGE_MESSAGE = {
    "event_type": "price_change",
    "asset_id": "12345678901234567890",
    "price": "0.49",
    "size": "75.0",
    "side": "BUY",
    "best_bid": "0.49",
    "best_ask": "0.52",
    "hash": "0xorder123",
}

SAMPLE_LAST_TRADE_MESSAGE = {
    "event_type": "last_trade_price",
    "asset_id": "12345678901234567890",
    "price": "0.50",
    "size": "25.0",
    "side": "BUY",
    "fee_rate": "0.015",
}

SAMPLE_TICK_SIZE_MESSAGE = {
    "event_type": "tick_size_change",
    "asset_id": "12345678901234567890",
    "old_tick_size": "0.01",
    "new_tick_size": "0.001",
}


class WebSocketMessageParser:
    """
    Parser for Polymarket WebSocket messages.

    This is the class we'll implement in api.py.
    For now, we define it here for testing.
    """

    @staticmethod
    def parse_message(raw_message: str) -> Optional[dict]:
        """Parse a raw WebSocket message."""
        try:
            return json.loads(raw_message)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def parse_book_message(message: dict) -> Optional[dict]:
        """Parse an order book snapshot message."""
        if message.get("event_type") != "book":
            return None

        return {
            "asset_id": message.get("asset_id"),
            "market_id": message.get("market"),
            "timestamp": message.get("timestamp"),
            "bids": [
                {"price": float(level["price"]), "size": float(level["size"])}
                for level in message.get("bids", [])
            ],
            "asks": [
                {"price": float(level["price"]), "size": float(level["size"])}
                for level in message.get("asks", [])
            ],
        }

    @staticmethod
    def parse_price_change(message: dict) -> Optional[dict]:
        """Parse a price change message."""
        if message.get("event_type") != "price_change":
            return None

        return {
            "asset_id": message.get("asset_id"),
            "price": float(message.get("price", 0)),
            "size": float(message.get("size", 0)),
            "side": message.get("side"),
            "best_bid": float(message.get("best_bid", 0)) if message.get("best_bid") else None,
            "best_ask": float(message.get("best_ask", 0)) if message.get("best_ask") else None,
        }

    @staticmethod
    def parse_last_trade(message: dict) -> Optional[dict]:
        """Parse a last trade price message."""
        if message.get("event_type") != "last_trade_price":
            return None

        return {
            "asset_id": message.get("asset_id"),
            "price": float(message.get("price", 0)),
            "size": float(message.get("size", 0)),
            "side": message.get("side"),
            "fee_rate": float(message.get("fee_rate", 0)) if message.get("fee_rate") else None,
        }

    @staticmethod
    def convert_to_orderbook(
        book_data: dict,
        token_type: TokenType,
    ) -> TokenOrderBook:
        """Convert parsed book data to TokenOrderBook model."""
        bids = OrderBookSide(
            levels=[
                PriceLevel(price=level["price"], size=level["size"])
                for level in book_data.get("bids", [])
            ]
        )
        asks = OrderBookSide(
            levels=[
                PriceLevel(price=level["price"], size=level["size"])
                for level in book_data.get("asks", [])
            ]
        )

        return TokenOrderBook(
            token_type=token_type,
            bids=bids,
            asks=asks,
        )


class TestParseRawMessage:
    """Tests for raw message parsing."""

    def test_parse_valid_json(self):
        """Test parsing valid JSON message."""
        raw = json.dumps(SAMPLE_BOOK_MESSAGE)
        result = WebSocketMessageParser.parse_message(raw)

        assert result is not None
        assert result["event_type"] == "book"

    def test_parse_invalid_json(self):
        """Test parsing invalid JSON returns None."""
        raw = "this is not valid json {"
        result = WebSocketMessageParser.parse_message(raw)

        assert result is None

    def test_parse_empty_string(self):
        """Test parsing empty string returns None."""
        result = WebSocketMessageParser.parse_message("")

        assert result is None


class TestParseBookMessage:
    """Tests for order book message parsing."""

    def test_parse_book_message(self):
        """Test parsing a full book message."""
        result = WebSocketMessageParser.parse_book_message(SAMPLE_BOOK_MESSAGE)

        assert result is not None
        assert result["asset_id"] == "12345678901234567890"
        assert result["market_id"] == "0xabc123"
        assert len(result["bids"]) == 3
        assert len(result["asks"]) == 3

    def test_book_message_bids_sorted(self):
        """Test that bids are parsed with correct prices."""
        result = WebSocketMessageParser.parse_book_message(SAMPLE_BOOK_MESSAGE)

        # First bid should be highest price (0.48)
        assert result["bids"][0]["price"] == 0.48
        assert result["bids"][0]["size"] == 150.5

    def test_book_message_asks_sorted(self):
        """Test that asks are parsed with correct prices."""
        result = WebSocketMessageParser.parse_book_message(SAMPLE_BOOK_MESSAGE)

        # First ask should be lowest price (0.52)
        assert result["asks"][0]["price"] == 0.52
        assert result["asks"][0]["size"] == 100.0

    def test_parse_non_book_message_returns_none(self):
        """Test that non-book messages return None."""
        result = WebSocketMessageParser.parse_book_message(SAMPLE_PRICE_CHANGE_MESSAGE)

        assert result is None

    def test_parse_book_with_empty_levels(self):
        """Test parsing book with empty bid/ask levels."""
        empty_book = {
            "event_type": "book",
            "asset_id": "123",
            "market": "0xabc",
            "timestamp": 1234567890,
            "buys": [],
            "sells": [],
        }

        result = WebSocketMessageParser.parse_book_message(empty_book)

        assert result is not None
        assert len(result["bids"]) == 0
        assert len(result["asks"]) == 0


class TestParsePriceChange:
    """Tests for price change message parsing."""

    def test_parse_price_change(self):
        """Test parsing a price change message."""
        result = WebSocketMessageParser.parse_price_change(SAMPLE_PRICE_CHANGE_MESSAGE)

        assert result is not None
        assert result["asset_id"] == "12345678901234567890"
        assert result["price"] == 0.49
        assert result["size"] == 75.0
        assert result["side"] == "BUY"
        assert result["best_bid"] == 0.49
        assert result["best_ask"] == 0.52

    def test_parse_non_price_change_returns_none(self):
        """Test that non-price_change messages return None."""
        result = WebSocketMessageParser.parse_price_change(SAMPLE_BOOK_MESSAGE)

        assert result is None


class TestParseLastTrade:
    """Tests for last trade price message parsing."""

    def test_parse_last_trade(self):
        """Test parsing a last trade message."""
        result = WebSocketMessageParser.parse_last_trade(SAMPLE_LAST_TRADE_MESSAGE)

        assert result is not None
        assert result["price"] == 0.50
        assert result["size"] == 25.0
        assert result["side"] == "BUY"
        assert result["fee_rate"] == 0.015


class TestConvertToOrderBook:
    """Tests for converting to OrderBook model."""

    def test_convert_to_orderbook(self):
        """Test converting parsed data to TokenOrderBook."""
        book_data = WebSocketMessageParser.parse_book_message(SAMPLE_BOOK_MESSAGE)
        result = WebSocketMessageParser.convert_to_orderbook(book_data, TokenType.YES)

        assert result is not None
        assert result.token_type == TokenType.YES
        assert len(result.bids.levels) == 3
        assert len(result.asks.levels) == 3
        assert result.bids.levels[0].price == 0.48
        assert result.asks.levels[0].price == 0.52

    def test_convert_empty_book(self):
        """Test converting empty book data."""
        book_data = {
            "bids": [],
            "asks": [],
        }
        result = WebSocketMessageParser.convert_to_orderbook(book_data, TokenType.NO)

        assert result is not None
        assert result.token_type == TokenType.NO
        assert len(result.bids.levels) == 0
        assert len(result.asks.levels) == 0


class TestHandleUnknownMessages:
    """Tests for handling unknown/malformed messages."""

    def test_handle_unknown_event_type(self):
        """Test that unknown event types don't crash."""
        unknown_msg = {
            "event_type": "unknown_future_event",
            "data": "some data",
        }

        # All parsers should return None for unknown types
        assert WebSocketMessageParser.parse_book_message(unknown_msg) is None
        assert WebSocketMessageParser.parse_price_change(unknown_msg) is None
        assert WebSocketMessageParser.parse_last_trade(unknown_msg) is None

    def test_handle_missing_fields(self):
        """Test handling messages with missing required fields."""
        incomplete_book = {
            "event_type": "book",
            # Missing asset_id, market, etc.
        }

        result = WebSocketMessageParser.parse_book_message(incomplete_book)

        # Should still parse, just with None values
        assert result is not None
        assert result["asset_id"] is None
        assert len(result["bids"]) == 0

    def test_handle_invalid_number_strings(self):
        """Test handling messages with invalid number strings."""
        bad_numbers = {
            "event_type": "price_change",
            "asset_id": "123",
            "price": "not_a_number",
            "size": "also_not_a_number",
            "side": "BUY",
        }

        # Should raise ValueError when trying to convert
        with pytest.raises(ValueError):
            WebSocketMessageParser.parse_price_change(bad_numbers)


class TestMessageTimestamps:
    """Tests for handling message timestamps."""

    def test_timestamp_is_milliseconds(self):
        """Test that timestamp is interpreted as milliseconds."""
        result = WebSocketMessageParser.parse_book_message(SAMPLE_BOOK_MESSAGE)

        # Timestamp is a string in the actual API
        assert result["timestamp"] == "1707500000000"

        # Convert to datetime to verify it's reasonable
        timestamp_seconds = int(result["timestamp"]) / 1000
        dt = datetime.fromtimestamp(timestamp_seconds)
        assert dt.year >= 2024


# Run with: pytest tests/test_websocket_messages.py -v
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
