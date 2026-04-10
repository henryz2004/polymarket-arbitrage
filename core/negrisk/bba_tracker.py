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
import time
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
        token_filter: Optional[Callable[[], list[str]]] = None,
    ):
        """
        Initialize the BBA tracker.

        Args:
            registry: The neg-risk registry to update
            config: Configuration
            on_price_update: Callback (event_id, token_id) when prices update
            token_filter: Optional callable that returns a filtered list of
                token IDs to subscribe to. If None, subscribes to all registry
                tokens. Used by the watchdog to only subscribe to watched tokens
                instead of all 10K+ registry tokens, preventing WS timeouts.
        """
        self.registry = registry
        self.config = config
        self.on_price_update = on_price_update
        self._token_filter = token_filter

        self._http_client: Optional[httpx.AsyncClient] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._running = False

        # Sequence tracking for staleness
        self._last_sequence: dict[str, int] = {}
        self._sequence_gaps: int = 0

        # WebSocket connectivity state
        self.ws_connected: bool = False
        self.last_ws_message_at: Optional[datetime] = None

        # Stats
        self._ws_messages_received: int = 0
        self._clob_fetches: int = 0
        self._empty_books: int = 0
        self._last_ws_message: Optional[datetime] = None

        # Per-session diagnostics
        self._ws_session_start: float = 0.0
        self._ws_session_msgs: int = 0
        self._ws_subscribe_size: int = 0

    async def start(self) -> None:
        """Start the BBA tracker."""
        if self._running:
            return

        self._running = True
        # Use connection pooling with explicit limits for CLOB API performance.
        # max_connections=100 allows parallel fetches during seeding/reseeding.
        # max_keepalive_connections=20 keeps warm connections for low-latency
        # slippage checks and gap recovery fetches.
        self._http_client = httpx.AsyncClient(
            timeout=10.0,
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
                keepalive_expiry=30.0,
            ),
        )

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
                self.ws_connected = False
                lifetime = time.monotonic() - self._ws_session_start if self._ws_session_start else 0
                close_code = e.rcvd.code if e.rcvd else None
                close_reason = e.rcvd.reason if e.rcvd else None
                logger.warning(
                    f"WebSocket closed: code={close_code} reason={close_reason!r} "
                    f"session_lifetime={lifetime:.1f}s "
                    f"session_msgs={self._ws_session_msgs} "
                    f"subscribe_payload_bytes={self._ws_subscribe_size} "
                    f"Reconnecting in {reconnect_delay}s..."
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)
            except asyncio.CancelledError:
                self.ws_connected = False
                raise
            except Exception as e:
                self.ws_connected = False
                lifetime = time.monotonic() - self._ws_session_start if self._ws_session_start else 0
                logger.error(
                    f"WebSocket error: {type(e).__name__}: {e} "
                    f"session_lifetime={lifetime:.1f}s "
                    f"session_msgs={self._ws_session_msgs} "
                    f"Reconnecting in {reconnect_delay}s..."
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

    async def _run_websocket(self) -> None:
        """Run a single WebSocket connection session."""
        # Reset per-session diagnostics before attempting connection
        self._ws_session_start = 0.0
        self._ws_session_msgs = 0
        self._ws_subscribe_size = 0

        # Get token IDs — use filter if provided (watchdog), else all registry tokens
        if self._token_filter:
            token_ids = self._token_filter()
        else:
            token_ids = self.registry.get_all_token_ids()

        if not token_ids:
            logger.debug("No tokens to subscribe, waiting...")
            await asyncio.sleep(5)
            return

        logger.info(f"Connecting WebSocket for {len(token_ids)} tokens...")

        async with websockets.connect(
            self.WS_URL,
            ping_interval=None,
            ping_timeout=None,
            close_timeout=5,
        ) as ws:
            # Reset reconnect delay on successful connection
            was_disconnected = not self.ws_connected
            self.ws_connected = True
            self._ws_session_start = time.monotonic()
            self._ws_session_msgs = 0
            logger.info("WebSocket connected")

            # Subscribe to all tokens
            subscribe_msg = {
                "type": "MARKET",
                "assets_ids": token_ids,
            }
            payload = json.dumps(subscribe_msg)
            self._ws_subscribe_size = len(payload)
            await ws.send(payload)
            logger.info(
                f"Subscribed to {len(token_ids)} tokens "
                f"(payload {self._ws_subscribe_size:,} bytes)"
            )

            # Post-reconnect: re-seed CLOB data for near-opportunity events
            # After a disconnect, BBA data becomes stale. Re-seeding ensures
            # we have fresh prices before the next detection scan.
            if was_disconnected:
                asyncio.create_task(
                    self._post_reconnect_reseed(),
                    name="post_reconnect_reseed",
                )

            # Process messages
            async for message in ws:
                if not self._running:
                    break

                self._ws_session_msgs += 1
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
                self.last_ws_message_at = self._last_ws_message

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
        """Check for sequence gaps and trigger CLOB refresh if needed."""
        if token_id in self._last_sequence:
            expected = self._last_sequence[token_id] + 1
            gap = sequence_id - expected
            if gap > 0:
                self._sequence_gaps += 1
                if gap > self.config.ws_sequence_gap_threshold:
                    logger.warning(
                        f"Large sequence gap for {token_id}: expected {expected}, got {sequence_id}. "
                        f"Scheduling CLOB refresh."
                    )
                    # Schedule a CLOB fetch to recover missed data
                    asyncio.create_task(
                        self._fetch_clob_price(token_id),
                        name=f"gap_refresh_{token_id[:8]}"
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

        # Parse full depth
        from core.negrisk.models import PriceLevel
        max_levels = self.config.max_book_levels if hasattr(self.config, 'max_book_levels') else 10
        bid_levels = [PriceLevel(price=float(b["price"]), size=float(b["size"])) for b in bids[:max_levels]]
        ask_levels = [PriceLevel(price=float(a["price"]), size=float(a["size"])) for a in asks[:max_levels]]

        sequence_id = event.get("sequence")

        # Update registry
        self.registry.update_outcome_bba(
            token_id=token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            bid_size=bid_size,
            ask_size=ask_size,
            sequence_id=sequence_id,
            source="websocket",
            bid_levels=bid_levels,
            ask_levels=ask_levels,
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

        GAMMA GUARD: Only fire callback if the outcome has real BBA data
        (websocket/clob sourced). After a registry refresh, outcomes reset
        to gamma-sourced BBA — firing the callback then would cause the
        watchdog to sample stale gamma prices as live data.
        """
        # Trigger callback to notify engine of price change
        if self.on_price_update:
            result = self.registry.get_event_by_token(token_id)
            if result:
                event_obj, outcome = result
                # Don't fire callback on gamma-sourced BBA — no real price data
                if outcome.bba.source == "gamma":
                    return
                self.on_price_update(event_obj.event_id, token_id)

    async def _fetch_clob_price(self, token_id: str) -> bool:
        """
        Fetch fresh price from CLOB API.

        Returns:
            True if CLOB returned a non-empty book, False otherwise.
        """
        try:
            resp = await self._http_client.get(
                f"{self.CLOB_URL}/book",
                params={"token_id": token_id},
            )

            if resp.status_code != 200:
                return False

            data = resp.json()
            self._clob_fetches += 1

            bids = data.get("bids", [])
            asks = data.get("asks", [])

            best_bid = float(bids[0]["price"]) if bids else None
            best_ask = float(asks[0]["price"]) if asks else None
            bid_size = float(bids[0]["size"]) if bids else None
            ask_size = float(asks[0]["size"]) if asks else None

            has_book = bool(bids or asks)
            if not has_book:
                self._empty_books += 1

            # Update registry
            self.registry.update_outcome_bba(
                token_id=token_id,
                best_bid=best_bid,
                best_ask=best_ask,
                bid_size=bid_size,
                ask_size=ask_size,
                source="clob",
            )

            # Trigger callback
            if self.on_price_update:
                result = self.registry.get_event_by_token(token_id)
                if result:
                    event_obj, outcome = result
                    self.on_price_update(event_obj.event_id, token_id)

            return has_book

        except Exception as e:
            logger.debug(f"CLOB fetch error for {token_id}: {e}")
            return False

    async def fetch_all_prices(self, event: NegriskEvent) -> dict:
        """
        Fetch fresh prices from CLOB for all outcomes in an event.

        Returns:
            Dict with seeding stats: {"seeded": N, "empty": N, "failed": N}
        """
        stats = {"seeded": 0, "empty": 0, "failed": 0}
        tasks = []
        for outcome in event.active_outcomes:
            if outcome.token_id:
                tasks.append(self._fetch_clob_price(outcome.token_id))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    stats["failed"] += 1
                elif result is True:
                    stats["seeded"] += 1
                else:
                    stats["empty"] += 1

        return stats

    async def fetch_batch_prices(self, token_ids: list[str]) -> dict[str, dict]:
        """
        Fetch prices for multiple tokens using CLOB /prices endpoint.

        More efficient than individual /book calls for cross-validation.

        Returns:
            Dict of token_id -> {"buy": price, "sell": price} or empty dict on failure.
        """
        if not token_ids or not self._http_client:
            return {}

        try:
            # CLOB /prices accepts a list of token_ids
            resp = await self._http_client.get(
                f"{self.CLOB_URL}/prices",
                params={"token_ids": ",".join(token_ids)},
            )

            if resp.status_code != 200:
                logger.debug(f"CLOB /prices returned {resp.status_code}")
                return {}

            return resp.json()

        except Exception as e:
            logger.debug(f"CLOB /prices batch fetch error: {e}")
            return {}

    def get_gamma_only_tokens(self) -> list[str]:
        """
        Get token IDs that still have only gamma-sourced prices (no CLOB/WebSocket data).

        These are candidates for re-seeding via CLOB fetch.
        """
        gamma_tokens = []
        for event in self.registry.get_all_events():
            for outcome in event.active_outcomes:
                if outcome.token_id and outcome.bba.source == "gamma":
                    gamma_tokens.append(outcome.token_id)
        return gamma_tokens

    def get_empty_book_tokens(self) -> list[str]:
        """
        Get token IDs that have CLOB-sourced data but empty books (no bid/ask).

        These may have become active since last check.
        """
        empty_tokens = []
        for event in self.registry.get_all_events():
            for outcome in event.active_outcomes:
                if outcome.token_id and outcome.bba.source in ("clob", "websocket"):
                    if outcome.bba.best_bid is None and outcome.bba.best_ask is None:
                        empty_tokens.append(outcome.token_id)
        return empty_tokens

    async def reseed_gamma_tokens(self) -> dict:
        """
        Re-seed tokens that still have gamma-only prices.

        Uses batch /prices for a quick check, then fetches individual /book
        for tokens where prices have changed or are newly available.

        Returns:
            Dict with reseed stats: {"checked": N, "reseeded": N, "still_empty": N, "failed": N}
        """
        stats = {"checked": 0, "reseeded": 0, "still_empty": 0, "failed": 0}

        gamma_tokens = self.get_gamma_only_tokens()
        empty_tokens = self.get_empty_book_tokens()
        all_tokens = list(set(gamma_tokens + empty_tokens))

        if not all_tokens:
            return stats

        stats["checked"] = len(all_tokens)

        # Batch check prices in chunks of 100
        batch_size = 100
        tokens_to_reseed = []

        for i in range(0, len(all_tokens), batch_size):
            batch = all_tokens[i:i + batch_size]
            batch_prices = await self.fetch_batch_prices(batch)

            if batch_prices:
                for token_id in batch:
                    price_data = batch_prices.get(token_id)
                    if price_data:
                        # Token has prices in batch endpoint — worth fetching full book
                        tokens_to_reseed.append(token_id)
            else:
                # Batch endpoint failed — fall back to fetching all individually
                tokens_to_reseed.extend(batch)

            # Rate limit between batches
            if i + batch_size < len(all_tokens):
                await asyncio.sleep(0.2)

        # Fetch individual /book for tokens that need re-seeding
        for token_id in tokens_to_reseed:
            try:
                has_book = await self._fetch_clob_price(token_id)
                if has_book:
                    stats["reseeded"] += 1
                else:
                    stats["still_empty"] += 1
            except Exception:
                stats["failed"] += 1

            # Rate limit individual fetches
            await asyncio.sleep(0.05)

        return stats

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

    async def _post_reconnect_reseed(self) -> None:
        """
        Re-seed CLOB data after WebSocket reconnection.

        After a disconnect, BBA timestamps are stale. Re-seed
        near-opportunity events first (highest priority), then
        remaining high-volume events in batches.
        """
        try:
            # Small delay to let WS messages start flowing
            await asyncio.sleep(1.0)

            # Priority 1: re-seed events near opportunity threshold
            near_events = self.registry.get_near_opportunity_events(threshold=0.05)
            if near_events:
                logger.info(f"Post-reconnect: re-seeding {len(near_events)} near-opportunity events")
                for event in near_events:
                    try:
                        await self.fetch_all_prices(event)
                    except Exception as e:
                        logger.debug(f"Post-reconnect reseed error for {event.event_id}: {e}")
                    await asyncio.sleep(0.2)  # Rate limit

            # Priority 2: re-seed top-volume events (up to 50)
            all_events = self.registry.get_tradeable_events()
            near_ids = {e.event_id for e in near_events} if near_events else set()
            remaining = [e for e in all_events if e.event_id not in near_ids]
            remaining.sort(key=lambda e: e.volume_24h, reverse=True)

            batch = remaining[:50]
            if batch:
                logger.info(f"Post-reconnect: re-seeding {len(batch)} high-volume events")
                for event in batch:
                    try:
                        await self.fetch_all_prices(event)
                    except Exception as e:
                        logger.debug(f"Post-reconnect reseed error for {event.event_id}: {e}")
                    await asyncio.sleep(0.3)

            total = len(near_events or []) + len(batch)
            logger.info(f"Post-reconnect re-seed complete: {total} events refreshed")

        except Exception as e:
            logger.error(f"Post-reconnect reseed failed: {e}")

    def get_stats(self) -> dict:
        """Get tracker statistics."""
        return {
            "ws_messages": self._ws_messages_received,
            "clob_fetches": self._clob_fetches,
            "empty_books": self._empty_books,
            "sequence_gaps": self._sequence_gaps,
            "last_ws_message": self._last_ws_message.isoformat() if self._last_ws_message else None,
            "tokens_tracked": len(self._last_sequence),
        }
