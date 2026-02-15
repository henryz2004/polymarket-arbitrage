"""
Hybrid Price Feed
==================

Three-tier data fetching strategy:
1. Gamma API (bulk, cached 3 min) - Market discovery & initial prices
2. WebSocket (real-time) - Change detection signals
3. CLOB /price (real-time, uncached) - Exact execution prices

This provides the best of both worlds:
- Efficient bulk scanning via Gamma API
- Real-time updates via WebSocket
- Accurate execution prices via CLOB
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator, Callable, Optional

import httpx
import websockets

logger = logging.getLogger(__name__)


@dataclass
class MarketPrice:
    """Real-time market price data."""
    market_id: str
    condition_id: str
    question: str

    # Token IDs
    yes_token_id: str
    no_token_id: str

    # Prices (from Gamma API or CLOB)
    yes_price: float  # e.g., 0.03 = 3c
    no_price: float   # e.g., 0.97 = 97c

    # Order book data (if available)
    yes_best_bid: Optional[float] = None
    yes_best_ask: Optional[float] = None
    no_best_bid: Optional[float] = None
    no_best_ask: Optional[float] = None
    spread: Optional[float] = None

    # Metadata
    volume_24h: float = 0.0
    last_updated: datetime = field(default_factory=datetime.utcnow)
    source: str = "gamma"  # "gamma", "clob", "websocket"

    @property
    def total_price(self) -> float:
        """YES + NO should equal ~1.0 for efficient markets."""
        return self.yes_price + self.no_price

    @property
    def arb_opportunity(self) -> float:
        """Potential arbitrage if total != 1.0."""
        return 1.0 - self.total_price


@dataclass
class PriceFeedStats:
    """Statistics for the price feed."""
    gamma_fetches: int = 0
    ws_messages: int = 0
    clob_fetches: int = 0
    markets_tracked: int = 0
    last_gamma_fetch: Optional[datetime] = None
    last_ws_message: Optional[datetime] = None


class HybridPriceFeed:
    """
    Hybrid price feed combining Gamma API, WebSocket, and CLOB.

    Usage:
        feed = HybridPriceFeed()
        await feed.start()

        # Get all market prices
        prices = feed.get_all_prices()

        # Get specific market
        price = await feed.get_real_price(market_id)

        # Subscribe to updates
        async for update in feed.stream_updates():
            print(f"Price changed: {update}")
    """

    GAMMA_API_URL = "https://gamma-api.polymarket.com"
    CLOB_API_URL = "https://clob.polymarket.com"
    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    def __init__(
        self,
        gamma_refresh_interval: float = 30.0,  # Gamma API refresh interval
        max_ws_markets: int = 100,  # Max markets to subscribe via WebSocket
        min_volume_24h: float = 1000.0,  # Minimum volume to track
        on_price_update: Optional[Callable[[MarketPrice], None]] = None,
    ):
        self.gamma_refresh_interval = gamma_refresh_interval
        self.max_ws_markets = max_ws_markets
        self.min_volume_24h = min_volume_24h
        self.on_price_update = on_price_update

        # State
        self._prices: dict[str, MarketPrice] = {}
        self._token_to_market: dict[str, str] = {}  # token_id -> market_id
        self._ws_subscribed: set[str] = set()  # token_ids subscribed

        # Tasks
        self._gamma_task: Optional[asyncio.Task] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._running = False

        # Stats
        self.stats = PriceFeedStats()

        # HTTP client
        self._http_client: Optional[httpx.AsyncClient] = None

        # Update queue for streaming
        self._update_queue: asyncio.Queue[MarketPrice] = asyncio.Queue()

    async def start(self) -> None:
        """Start the hybrid price feed."""
        if self._running:
            logger.warning("HybridPriceFeed already running")
            return

        self._running = True
        self._http_client = httpx.AsyncClient(timeout=30.0)

        logger.info("Starting HybridPriceFeed...")

        # Initial Gamma API fetch
        await self._fetch_gamma_markets()

        # Start background tasks
        self._gamma_task = asyncio.create_task(
            self._gamma_refresh_loop(),
            name="gamma_refresh"
        )

        self._ws_task = asyncio.create_task(
            self._websocket_loop(),
            name="websocket"
        )

        logger.info(f"HybridPriceFeed started - tracking {len(self._prices)} markets")

    async def stop(self) -> None:
        """Stop the price feed."""
        self._running = False

        if self._gamma_task:
            self._gamma_task.cancel()
            try:
                await self._gamma_task
            except asyncio.CancelledError:
                pass

        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

        if self._http_client:
            await self._http_client.aclose()

        logger.info("HybridPriceFeed stopped")

    async def _fetch_gamma_markets(self) -> None:
        """Fetch all markets from Gamma API."""
        try:
            all_markets = []
            offset = 0
            limit = 500

            while self._running:
                resp = await self._http_client.get(
                    f"{self.GAMMA_API_URL}/markets",
                    params={
                        "limit": limit,
                        "offset": offset,
                        "closed": "false",
                        "active": "true",
                    },
                )

                if resp.status_code != 200:
                    logger.error(f"Gamma API error: {resp.status_code}")
                    break

                markets = resp.json()
                if not markets:
                    break

                all_markets.extend(markets)
                offset += limit

                if len(markets) < limit:
                    break

            # Process markets
            for market in all_markets:
                self._process_gamma_market(market)

            self.stats.gamma_fetches += 1
            self.stats.last_gamma_fetch = datetime.utcnow()
            self.stats.markets_tracked = len(self._prices)

            logger.debug(f"Gamma API: fetched {len(all_markets)} markets, tracking {len(self._prices)}")

        except Exception as e:
            logger.error(f"Gamma API fetch error: {e}")

    def _process_gamma_market(self, market: dict) -> None:
        """Process a market from Gamma API response."""
        try:
            market_id = str(market.get("id", ""))
            condition_id = str(market.get("conditionId", ""))
            question = market.get("question", "")

            # Parse token IDs
            clob_ids = market.get("clobTokenIds", "")
            if not clob_ids:
                return

            token_ids = [t.strip().strip('"') for t in clob_ids.strip("[]").split(",")]
            if len(token_ids) < 2:
                return

            yes_token_id = token_ids[0]
            no_token_id = token_ids[1]

            # Parse prices
            outcome_prices = market.get("outcomePrices")
            if not outcome_prices:
                return

            # Handle both list and string formats
            if isinstance(outcome_prices, str):
                # Format: "[\"0.03\", \"0.97\"]"
                try:
                    import json
                    outcome_prices = json.loads(outcome_prices)
                except:
                    return

            if not isinstance(outcome_prices, list) or len(outcome_prices) < 2:
                return

            try:
                yes_price = float(outcome_prices[0])
                no_price = float(outcome_prices[1])
            except (ValueError, TypeError):
                return

            # Parse volume
            volume_24h = float(market.get("volume24hr", 0) or 0)

            # Filter by volume
            if volume_24h < self.min_volume_24h:
                return

            # Parse bid/ask if available
            best_bid = market.get("bestBid")
            best_ask = market.get("bestAsk")
            spread = market.get("spread")

            # Create or update price
            price = MarketPrice(
                market_id=market_id,
                condition_id=condition_id,
                question=question,
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
                yes_price=yes_price,
                no_price=no_price,
                yes_best_bid=float(best_bid) if best_bid else None,
                yes_best_ask=float(best_ask) if best_ask else None,
                spread=float(spread) if spread else None,
                volume_24h=volume_24h,
                last_updated=datetime.utcnow(),
                source="gamma",
            )

            self._prices[market_id] = price
            self._token_to_market[yes_token_id] = market_id
            self._token_to_market[no_token_id] = market_id

        except Exception as e:
            logger.debug(f"Error processing market: {e}")

    async def _gamma_refresh_loop(self) -> None:
        """Periodically refresh from Gamma API."""
        while self._running:
            try:
                await asyncio.sleep(self.gamma_refresh_interval)
                await self._fetch_gamma_markets()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Gamma refresh error: {e}")
                await asyncio.sleep(5)

    async def _websocket_loop(self) -> None:
        """WebSocket connection for real-time updates."""
        while self._running:
            try:
                # Get high-volume markets to subscribe
                top_markets = sorted(
                    self._prices.values(),
                    key=lambda p: p.volume_24h,
                    reverse=True
                )[:self.max_ws_markets]

                if not top_markets:
                    await asyncio.sleep(5)
                    continue

                # Collect all token IDs
                token_ids = []
                for market in top_markets:
                    token_ids.append(market.yes_token_id)
                    token_ids.append(market.no_token_id)

                logger.info(f"WebSocket: subscribing to {len(top_markets)} markets ({len(token_ids)} tokens)")

                async with websockets.connect(
                    self.WS_URL,
                    ping_interval=30,
                    ping_timeout=10,
                ) as ws:
                    # Subscribe
                    subscribe_msg = {
                        "type": "MARKET",
                        "assets_ids": token_ids,
                    }
                    await ws.send(json.dumps(subscribe_msg))
                    self._ws_subscribed = set(token_ids)

                    # Process messages
                    while self._running:
                        try:
                            message = await asyncio.wait_for(ws.recv(), timeout=60)
                            await self._process_ws_message(message)
                        except asyncio.TimeoutError:
                            continue

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
                await asyncio.sleep(5)

    async def _process_ws_message(self, message: str) -> None:
        """Process a WebSocket message."""
        try:
            raw_data = json.loads(message)
            events = raw_data if isinstance(raw_data, list) else [raw_data]

            for data in events:
                if not isinstance(data, dict):
                    continue

                asset_id = data.get("asset_id", "")
                if not asset_id:
                    continue

                # Find the market
                market_id = self._token_to_market.get(asset_id)
                if not market_id or market_id not in self._prices:
                    continue

                # WebSocket activity detected - fetch real price
                self.stats.ws_messages += 1
                self.stats.last_ws_message = datetime.utcnow()

                # Mark for real price fetch
                await self._fetch_real_price(market_id)

        except Exception as e:
            logger.debug(f"WebSocket message error: {e}")

    async def _fetch_real_price(self, market_id: str) -> Optional[MarketPrice]:
        """Fetch real-time price from CLOB API (uncached)."""
        if market_id not in self._prices:
            return None

        price = self._prices[market_id]

        try:
            # Fetch YES price
            yes_resp = await self._http_client.get(
                f"{self.CLOB_API_URL}/price",
                params={"token_id": price.yes_token_id, "side": "buy"},
            )

            # Fetch NO price
            no_resp = await self._http_client.get(
                f"{self.CLOB_API_URL}/price",
                params={"token_id": price.no_token_id, "side": "buy"},
            )

            if yes_resp.status_code == 200 and no_resp.status_code == 200:
                yes_data = yes_resp.json()
                no_data = no_resp.json()

                new_yes_price = float(yes_data.get("price", price.yes_price))
                new_no_price = float(no_data.get("price", price.no_price))

                # Update if changed
                if new_yes_price != price.yes_price or new_no_price != price.no_price:
                    price.yes_price = new_yes_price
                    price.no_price = new_no_price
                    price.last_updated = datetime.utcnow()
                    price.source = "clob"

                    self.stats.clob_fetches += 1

                    # Notify callback
                    if self.on_price_update:
                        self.on_price_update(price)

                    # Add to update queue
                    await self._update_queue.put(price)

                    logger.debug(f"CLOB price update: {price.question[:30]}... YES={new_yes_price:.4f} NO={new_no_price:.4f}")

            return price

        except Exception as e:
            logger.debug(f"CLOB price fetch error: {e}")
            return None

    async def get_real_price(self, market_id: str) -> Optional[MarketPrice]:
        """Get real-time price for a specific market (forces CLOB fetch)."""
        return await self._fetch_real_price(market_id)

    def get_price(self, market_id: str) -> Optional[MarketPrice]:
        """Get cached price for a market."""
        return self._prices.get(market_id)

    def get_all_prices(self) -> dict[str, MarketPrice]:
        """Get all cached prices."""
        return self._prices.copy()

    def get_arbitrage_opportunities(self, min_edge: float = 0.01) -> list[MarketPrice]:
        """Find markets where YES + NO != 1.0 (potential arbitrage)."""
        opportunities = []

        for price in self._prices.values():
            edge = abs(price.arb_opportunity)
            if edge >= min_edge:
                opportunities.append(price)

        return sorted(opportunities, key=lambda p: abs(p.arb_opportunity), reverse=True)

    async def stream_updates(self) -> AsyncIterator[MarketPrice]:
        """Stream price updates as they occur."""
        while self._running:
            try:
                update = await asyncio.wait_for(self._update_queue.get(), timeout=1.0)
                yield update
            except asyncio.TimeoutError:
                continue

    def get_stats(self) -> dict:
        """Get feed statistics."""
        return {
            "markets_tracked": self.stats.markets_tracked,
            "gamma_fetches": self.stats.gamma_fetches,
            "ws_messages": self.stats.ws_messages,
            "clob_fetches": self.stats.clob_fetches,
            "last_gamma_fetch": self.stats.last_gamma_fetch.isoformat() if self.stats.last_gamma_fetch else None,
            "last_ws_message": self.stats.last_ws_message.isoformat() if self.stats.last_ws_message else None,
            "ws_subscribed": len(self._ws_subscribed),
        }
