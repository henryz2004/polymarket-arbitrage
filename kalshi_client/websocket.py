"""
Kalshi WebSocket Client
========================

Real-time market data streaming via Kalshi's authenticated WebSocket API.

Channels used by the watchdog:
- ticker: BBO price updates (yes_bid, yes_ask, volume)
- trade: Individual trade executions (for whale detection)

URL: wss://api.elections.kalshi.com/trade-api/ws/v2
Auth: RSA-PSS signed headers in handshake
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Callable, Optional

import websockets

from kalshi_client.auth import KalshiAuth
from kalshi_client.models import KalshiTickerUpdate, KalshiTrade

logger = logging.getLogger(__name__)


class KalshiWebSocket:
    """
    Authenticated WebSocket client for Kalshi real-time market data.

    Subscribes to ticker and trade channels, dispatches updates via callbacks.
    Handles reconnection with exponential backoff.
    """

    WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
    DEMO_WS_URL = "wss://demo-api.kalshi.co/trade-api/ws/v2"

    # Subscription batching: Kalshi allows subscribing to multiple tickers
    # in a single message. Batch to avoid flooding the connection.
    MAX_TICKERS_PER_SUB = 50

    def __init__(
        self,
        auth: KalshiAuth,
        on_ticker: Optional[Callable[[KalshiTickerUpdate], None]] = None,
        on_trade: Optional[Callable[[KalshiTrade], None]] = None,
        on_connect: Optional[Callable[[], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
        demo: bool = False,
        reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 60.0,
    ):
        """
        Args:
            auth: KalshiAuth for WebSocket handshake
            on_ticker: Callback for ticker updates
            on_trade: Callback for trade executions
            on_connect: Called on successful connection
            on_disconnect: Called on disconnection
            demo: Use demo environment
            reconnect_delay: Initial reconnect delay (exponential backoff)
            max_reconnect_delay: Maximum reconnect delay
        """
        self.auth = auth
        self.on_ticker = on_ticker
        self.on_trade = on_trade
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self._ws_url = self.DEMO_WS_URL if demo else self.WS_URL
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay

        # State
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._connected = False
        self._message_count = 0
        self._last_message_at: Optional[datetime] = None

        # Subscription tracking
        self._subscribed_tickers: set[str] = set()
        self._pending_tickers: set[str] = set()  # Tickers to subscribe on (re)connect
        self._next_sub_id = 1
        self._subscription_ids: dict[int, str] = {}  # sid -> channel

        # Sequence tracking for gap detection
        self._last_seq: dict[str, int] = {}  # channel -> last seq

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def message_count(self) -> int:
        return self._message_count

    async def start(self) -> None:
        """Start the WebSocket connection loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(
            self._connection_loop(), name="kalshi_ws"
        )

    async def stop(self) -> None:
        """Stop the WebSocket connection."""
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._connected = False

    def subscribe(self, tickers: list[str]) -> None:
        """
        Subscribe to ticker + trade updates for the given market tickers.

        If already connected, sends subscription immediately.
        If not connected, queues for subscription on next connect.
        """
        new_tickers = set(tickers) - self._subscribed_tickers
        if not new_tickers:
            return

        self._pending_tickers.update(new_tickers)

        if self._connected and self._ws:
            # Subscribe immediately
            asyncio.create_task(self._send_subscriptions(new_tickers))

    def unsubscribe(self, tickers: list[str]) -> None:
        """Remove tickers from subscription."""
        for t in tickers:
            self._subscribed_tickers.discard(t)
            self._pending_tickers.discard(t)

    async def _connection_loop(self) -> None:
        """Main connection loop with reconnection."""
        delay = self._reconnect_delay

        while self._running:
            try:
                # Generate auth headers for handshake
                headers = self.auth.get_ws_headers()

                async with websockets.connect(
                    self._ws_url,
                    additional_headers=headers,
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    delay = self._reconnect_delay  # Reset backoff on success

                    logger.info("Kalshi WebSocket connected")
                    if self.on_connect:
                        try:
                            self.on_connect()
                        except Exception:
                            pass

                    # Subscribe to all pending tickers
                    all_tickers = self._pending_tickers | self._subscribed_tickers
                    if all_tickers:
                        await self._send_subscriptions(all_tickers)

                    # Message loop
                    async for raw_msg in ws:
                        try:
                            msg = json.loads(raw_msg)
                            self._message_count += 1
                            self._last_message_at = datetime.utcnow()
                            self._handle_message(msg)
                        except json.JSONDecodeError:
                            logger.debug(f"Non-JSON WS message: {raw_msg[:100]}")
                        except Exception as e:
                            logger.debug(f"WS message handling error: {e}")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._running:
                    logger.warning(f"Kalshi WS disconnected: {e}")
            finally:
                self._connected = False
                self._ws = None
                if self.on_disconnect:
                    try:
                        self.on_disconnect()
                    except Exception:
                        pass

            if not self._running:
                break

            # Exponential backoff
            logger.info(f"Reconnecting in {delay:.1f}s...")
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._max_reconnect_delay)

    async def _send_subscriptions(self, tickers: set[str]) -> None:
        """Send subscription messages for a set of tickers."""
        if not self._ws or not self._connected:
            return

        ticker_list = list(tickers)
        channels = ["ticker"]
        if self.on_trade:
            channels.append("trade")

        # Batch subscriptions
        for i in range(0, len(ticker_list), self.MAX_TICKERS_PER_SUB):
            batch = ticker_list[i:i + self.MAX_TICKERS_PER_SUB]
            sub_id = self._next_sub_id
            self._next_sub_id += 1

            msg = {
                "id": sub_id,
                "cmd": "subscribe",
                "params": {
                    "channels": channels,
                    "market_tickers": batch,
                },
            }
            try:
                await self._ws.send(json.dumps(msg))
                self._subscribed_tickers.update(batch)
                self._pending_tickers -= set(batch)
                logger.debug(
                    f"Subscribed to {len(batch)} tickers "
                    f"(channels: {channels}, sub_id: {sub_id})"
                )
            except Exception as e:
                logger.warning(f"Subscription send failed: {e}")

            # Small delay between batches
            if i + self.MAX_TICKERS_PER_SUB < len(ticker_list):
                await asyncio.sleep(0.1)

        logger.info(
            f"Subscribed to {len(ticker_list)} tickers "
            f"({len(self._subscribed_tickers)} total)"
        )

    def _handle_message(self, msg: dict) -> None:
        """Route incoming WebSocket messages to appropriate handlers."""
        msg_type = msg.get("type", "")

        if msg_type == "ticker":
            self._handle_ticker(msg)
        elif msg_type == "trade":
            self._handle_trade(msg)
        elif msg_type == "subscribed":
            sid = msg.get("msg", {}).get("sid")
            channel = msg.get("msg", {}).get("channel", "")
            if sid:
                self._subscription_ids[sid] = channel
            logger.debug(f"Subscription confirmed: sid={sid}, channel={channel}")
        elif msg_type == "error":
            error = msg.get("msg", {})
            logger.warning(
                f"Kalshi WS error: code={error.get('code')}, "
                f"msg={error.get('msg', '')}"
            )

        # Track sequence numbers for gap detection
        seq = msg.get("seq")
        if seq is not None and msg_type:
            last = self._last_seq.get(msg_type)
            if last is not None and seq > last + 1:
                gap = seq - last - 1
                logger.warning(
                    f"Sequence gap in {msg_type}: {last} -> {seq} ({gap} missing)"
                )
            self._last_seq[msg_type] = seq

    def _handle_ticker(self, msg: dict) -> None:
        """Parse and dispatch a ticker update."""
        if not self.on_ticker:
            return

        data = msg.get("msg", {})
        if not data:
            return

        def _fp(val) -> Optional[float]:
            if val is None:
                return None
            try:
                return float(val)
            except (ValueError, TypeError):
                return None

        update = KalshiTickerUpdate(
            market_ticker=data.get("market_ticker", ""),
            yes_bid=_fp(data.get("yes_bid_dollars")),
            yes_ask=_fp(data.get("yes_ask_dollars")),
            last_price=_fp(data.get("price_dollars")),
            volume=float(_fp(data.get("volume_fp")) or 0),
            open_interest=float(_fp(data.get("open_interest_fp")) or 0),
            yes_bid_size=float(_fp(data.get("yes_bid_size_fp")) or 0),
            yes_ask_size=float(_fp(data.get("yes_ask_size_fp")) or 0),
            last_trade_size=float(_fp(data.get("last_trade_size_fp")) or 0),
            ts=data.get("ts", 0),
        )

        try:
            self.on_ticker(update)
        except Exception as e:
            logger.debug(f"Ticker callback error: {e}")

    def _handle_trade(self, msg: dict) -> None:
        """Parse and dispatch a trade execution."""
        if not self.on_trade:
            return

        data = msg.get("msg", {})
        if not data:
            return

        def _fp(val) -> Optional[float]:
            if val is None:
                return None
            try:
                return float(val)
            except (ValueError, TypeError):
                return None

        try:
            trade = KalshiTrade(
                trade_id=data.get("trade_id", ""),
                market_ticker=data.get("market_ticker", ""),
                side=data.get("taker_side", "yes"),
                price=_fp(data.get("yes_price_dollars")) or 0.0,
                count=float(_fp(data.get("count_fp")) or 0),
                ts=data.get("ts", 0),
            )
            self.on_trade(trade)
        except Exception as e:
            logger.debug(f"Trade callback error: {e}")

    def get_stats(self) -> dict:
        """Get WebSocket connection statistics."""
        return {
            "connected": self._connected,
            "message_count": self._message_count,
            "subscribed_tickers": len(self._subscribed_tickers),
            "last_message_at": (
                self._last_message_at.isoformat() if self._last_message_at else None
            ),
            "sequence_gaps": {
                ch: seq for ch, seq in self._last_seq.items()
            },
        }
