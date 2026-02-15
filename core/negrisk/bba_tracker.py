"""
Negrisk BBA Tracker
====================

Real-time Best Bid/Ask tracking for neg-risk outcomes via WebSocket.

Features:
1. WebSocket subscription to all outcome tokens
2. Sequence number tracking for staleness detection
3. Triggers CLOB fetch for price confirmation
4. Updates registry with fresh BBA data
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import AsyncIterator, Callable, Optional

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from core.negrisk.models import NegriskConfig, NegriskEvent
from core.negrisk.registry import NegriskRegistry


logger = logging.getLogger(__name__)


class BBATracker:
    """
    Real-time BBA tracker for neg-risk outcomes.

    Subscribes to WebSocket for all tracked tokens and maintains
    fresh BBA data in the registry.
    """

    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    CLOB_URL = "https://clob.polymarket.com"

    def __init__(
        self,
        registry: NegriskRegistry,
        config: NegriskConfig,
        on_price_update: Optional[Callable[[str, str], None]] = None,
    ):
        """
        Initialize the BBA tracker.

        Args:
            registry: The neg-risk registry to update
            config: Configuration
            on_price_update: Callback (event_id, token_id) when prices update
        """
        self.registry = registry
        self.config = config
        self.on_price_update = on_price_update

        self._http_client: Optional[httpx.AsyncClient] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._running = False

        # Sequence tracking for staleness
        self._last_sequence: dict[str, int] = {}
        self._sequence_gaps: int = 0

        # Stats
        self._ws_messages_received: int = 0
        self._clob_fetches: int = 0
        self._last_ws_message: Optional[datetime] = None

    async def start(self) -> None:
        """Start the BBA tracker."""
        if self._running:
            return

        self._running = True
        self._http_client = httpx.AsyncClient(timeout=10.0)

        # Start WebSocket task
        self._ws_task = asyncio.create_task(
            self._websocket_loop(),
            name="negrisk_bba_ws"
        )

        logger.info("BBA Tracker started")

    async def stop(self) -> None:
        """Stop the BBA tracker."""
        self._running = False

        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

        if self._http_client:
            await self._http_client.aclose()

        logger.info("BBA Tracker stopped")

    async def _websocket_loop(self) -> None:
        """Main WebSocket loop with reconnection."""
        reconnect_delay = self.config.bba_ws_reconnect_delay
        max_reconnect_delay = 30.0

        while self._running:
            try:
                await self._run_websocket()
            except ConnectionClosed as e:
                logger.warning(f"WebSocket closed: {e}. Reconnecting in {reconnect_delay}s...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"WebSocket error: {e}. Reconnecting in {reconnect_delay}s...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

    async def _run_websocket(self) -> None:
        """Run a single WebSocket connection session."""
        # Get all token IDs from registry
        token_ids = self.registry.get_all_token_ids()

        if not token_ids:
            logger.debug("No tokens to subscribe, waiting...")
            await asyncio.sleep(5)
            return

        logger.info(f"Connecting WebSocket for {len(token_ids)} tokens...")

        async with websockets.connect(
            self.WS_URL,
            ping_interval=30,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            # Reset reconnect delay on successful connection
            logger.info("WebSocket connected")

            # Subscribe to all tokens
            subscribe_msg = {
                "type": "MARKET",
                "assets_ids": token_ids,
            }
            await ws.send(json.dumps(subscribe_msg))
            logger.info(f"Subscribed to {len(token_ids)} tokens")

            # Process messages
            async for message in ws:
                if not self._running:
                    break

                await self._process_ws_message(message)

    async def _process_ws_message(self, message: str) -> None:
        """Process a WebSocket message."""
        try:
            raw_data = json.loads(message)
            events = raw_data if isinstance(raw_data, list) else [raw_data]

            for event in events:
                if not isinstance(event, dict):
                    continue

                event_type = event.get("event_type")
                asset_id = event.get("asset_id")

                if not asset_id:
                    continue

                self._ws_messages_received += 1
                self._last_ws_message = datetime.utcnow()

                # Check sequence for staleness
                sequence_id = event.get("sequence")
                if sequence_id is not None:
                    self._check_sequence(asset_id, sequence_id)

                if event_type == "book":
                    # Full order book snapshot
                    await self._handle_book_update(asset_id, event)
                elif event_type == "price_change":
                    # Price change - just trigger callback, no CLOB fetch
                    # We'll fetch fresh prices from CLOB only before execution
                    await self._handle_price_change(asset_id, event)

        except json.JSONDecodeError:
            logger.debug("Failed to parse WebSocket message")
        except Exception as e:
            logger.debug(f"Error processing WebSocket message: {e}")

    def _check_sequence(self, token_id: str, sequence_id: int) -> None:
        """Check for sequence gaps (indicates missed messages)."""
        if token_id in self._last_sequence:
            expected = self._last_sequence[token_id] + 1
            gap = sequence_id - expected
            if gap > 0:
                self._sequence_gaps += 1
                if gap > self.config.ws_sequence_gap_threshold:
                    logger.warning(
                        f"Large sequence gap for {token_id}: expected {expected}, got {sequence_id}"
                    )

        self._last_sequence[token_id] = sequence_id

    async def _handle_book_update(self, token_id: str, event: dict) -> None:
        """Handle a full book snapshot from WebSocket."""
        bids = event.get("bids", [])
        asks = event.get("asks", [])

        best_bid = float(bids[0]["price"]) if bids else None
        best_ask = float(asks[0]["price"]) if asks else None
        bid_size = float(bids[0]["size"]) if bids else None
        ask_size = float(asks[0]["size"]) if asks else None

        sequence_id = event.get("sequence")

        # Update registry
        self.registry.update_outcome_bba(
            token_id=token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            bid_size=bid_size,
            ask_size=ask_size,
            sequence_id=sequence_id,
        )

        # Trigger callback
        if self.on_price_update:
            result = self.registry.get_event_by_token(token_id)
            if result:
                event_obj, outcome = result
                self.on_price_update(event_obj.event_id, token_id)

    async def _handle_price_change(self, token_id: str, event: dict) -> None:
        """
        Handle a price change event.

        PERFORMANCE FIX: Don't fetch from CLOB on every price change.
        WebSocket book events provide BBA data, and we fetch fresh from
        CLOB before execution anyway. Just trigger the callback.
        """
        # Trigger callback to notify engine of price change
        if self.on_price_update:
            result = self.registry.get_event_by_token(token_id)
            if result:
                event_obj, outcome = result
                self.on_price_update(event_obj.event_id, token_id)

    async def _fetch_clob_price(self, token_id: str) -> None:
        """Fetch fresh price from CLOB API."""
        try:
            resp = await self._http_client.get(
                f"{self.CLOB_URL}/book",
                params={"token_id": token_id},
            )

            if resp.status_code != 200:
                return

            data = resp.json()
            self._clob_fetches += 1

            bids = data.get("bids", [])
            asks = data.get("asks", [])

            best_bid = float(bids[0]["price"]) if bids else None
            best_ask = float(asks[0]["price"]) if asks else None
            bid_size = float(bids[0]["size"]) if bids else None
            ask_size = float(asks[0]["size"]) if asks else None

            # Update registry
            self.registry.update_outcome_bba(
                token_id=token_id,
                best_bid=best_bid,
                best_ask=best_ask,
                bid_size=bid_size,
                ask_size=ask_size,
            )

            # Trigger callback
            if self.on_price_update:
                result = self.registry.get_event_by_token(token_id)
                if result:
                    event_obj, outcome = result
                    self.on_price_update(event_obj.event_id, token_id)

        except Exception as e:
            logger.debug(f"CLOB fetch error for {token_id}: {e}")

    async def fetch_all_prices(self, event: NegriskEvent) -> None:
        """Fetch fresh prices from CLOB for all outcomes in an event."""
        tasks = []
        for outcome in event.active_outcomes:
            if outcome.token_id:
                tasks.append(self._fetch_clob_price(outcome.token_id))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def stream_price_updates(self) -> AsyncIterator[tuple[str, str]]:
        """
        Stream price updates as they occur.

        Yields:
            Tuple of (event_id, token_id) for each update
        """
        update_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

        # Set up callback to queue updates
        original_callback = self.on_price_update

        def queue_update(event_id: str, token_id: str) -> None:
            update_queue.put_nowait((event_id, token_id))
            if original_callback:
                original_callback(event_id, token_id)

        self.on_price_update = queue_update

        try:
            while self._running:
                try:
                    update = await asyncio.wait_for(update_queue.get(), timeout=1.0)
                    yield update
                except asyncio.TimeoutError:
                    continue
        finally:
            self.on_price_update = original_callback

    def get_stats(self) -> dict:
        """Get tracker statistics."""
        return {
            "ws_messages": self._ws_messages_received,
            "clob_fetches": self._clob_fetches,
            "sequence_gaps": self._sequence_gaps,
            "last_ws_message": self._last_ws_message.isoformat() if self._last_ws_message else None,
            "tokens_tracked": len(self._last_sequence),
        }
