"""
Kalshi API Client
=================

Client for interacting with Kalshi prediction market exchange.
Supports both public (unauthenticated) and private (authenticated) endpoints.

API Documentation: https://docs.kalshi.com
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional, AsyncIterator
import httpx

from kalshi_client.auth import KalshiAuth
from kalshi_client.models import (
    KalshiMarket,
    KalshiOrderBook,
    KalshiEvent,
    KalshiSeries,
    KalshiCandlestick,
    KalshiTrade,
)
from polymarket_client.models import PriceLevel, OrderBook

logger = logging.getLogger(__name__)


def _parse_fp_dollars(val) -> Optional[float]:
    """Parse a FixedPointDollars string (e.g., '0.5600') to float, or None."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _parse_fp_count(val) -> float:
    """Parse a FixedPointCount string (e.g., '10.00') to float, defaulting to 0."""
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


class KalshiClient:
    """
    Async client for Kalshi prediction market API.

    Supports both unauthenticated (public market data) and authenticated
    (trading, WebSocket) modes.
    """

    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

    def __init__(
        self,
        auth: Optional[KalshiAuth] = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        dry_run: bool = True,
    ):
        """
        Initialize Kalshi client.

        Args:
            auth: Optional KalshiAuth for authenticated endpoints
            timeout: Request timeout in seconds
            max_retries: Maximum number of retry attempts
            dry_run: If True, don't place real orders (read-only mode)
        """
        self.auth = auth
        self.timeout = timeout
        self.max_retries = max_retries
        self.dry_run = dry_run
        self._client: Optional[httpx.AsyncClient] = None
        self._markets_cache: dict[str, KalshiMarket] = {}

    async def __aenter__(self) -> "KalshiClient":
        """Async context manager entry."""
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            headers={"Accept": "application/json"},
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _get(self, endpoint: str, params: Optional[dict] = None,
                   authenticated: bool = False) -> dict:
        """
        Make a GET request to the Kalshi API.

        Args:
            endpoint: API endpoint (without base URL)
            params: Query parameters
            authenticated: Whether to include auth headers

        Returns:
            JSON response as dictionary
        """
        if not self._client:
            raise RuntimeError("Client not initialized. Use async with context manager.")

        url = f"{self.BASE_URL}{endpoint}"

        for attempt in range(self.max_retries):
            try:
                headers = {}
                if authenticated and self.auth:
                    path = f"/trade-api/v2{endpoint}"
                    headers = self.auth.get_headers("GET", path)

                response = await self._client.get(url, params=params, headers=headers)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:  # Rate limited
                    wait_time = 2 ** attempt
                    logger.warning(f"Rate limited, waiting {wait_time}s before retry")
                    await asyncio.sleep(wait_time)
                elif e.response.status_code == 404:
                    logger.debug(f"Not found: {endpoint}")
                    return {}
                else:
                    logger.error(f"HTTP error {e.response.status_code}: {e}")
                    raise
            except httpx.RequestError as e:
                logger.warning(f"Request error (attempt {attempt + 1}): {e}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(1)
                else:
                    raise

        return {}
    
    # =========================================================================
    # SERIES ENDPOINTS
    # =========================================================================
    
    async def get_series(self, series_ticker: str) -> Optional[KalshiSeries]:
        """
        Get information about a series.
        
        Args:
            series_ticker: Series ticker (e.g., "KXHIGHNY")
            
        Returns:
            KalshiSeries object or None if not found
        """
        data = await self._get(f"/series/{series_ticker}")
        if not data or "series" not in data:
            return None
        
        s = data["series"]
        return KalshiSeries(
            ticker=s.get("ticker", series_ticker),
            title=s.get("title", ""),
            frequency=s.get("frequency", ""),
            category=s.get("category", ""),
        )
    
    # =========================================================================
    # EVENTS ENDPOINTS
    # =========================================================================
    
    async def get_event(self, event_ticker: str) -> Optional[KalshiEvent]:
        """
        Get information about an event.
        
        Args:
            event_ticker: Event ticker (e.g., "KXHIGHNY-25DEC08")
            
        Returns:
            KalshiEvent object or None if not found
        """
        data = await self._get(f"/events/{event_ticker}")
        if not data or "event" not in data:
            return None
        
        e = data["event"]
        return KalshiEvent(
            event_ticker=e.get("ticker", event_ticker),
            series_ticker=e.get("series_ticker", ""),
            title=e.get("title", ""),
            category=e.get("category", ""),
        )
    
    # =========================================================================
    # MARKETS ENDPOINTS
    # =========================================================================
    
    async def list_markets(
        self,
        status: str = "open",
        series_ticker: Optional[str] = None,
        event_ticker: Optional[str] = None,
        limit: int = 1000,
        cursor: Optional[str] = None,
    ) -> tuple[list[KalshiMarket], Optional[str]]:
        """
        List markets with optional filters.
        
        Args:
            status: Market status filter (open, closed, settled)
            series_ticker: Filter by series
            event_ticker: Filter by event
            limit: Maximum markets to return (max 1000)
            cursor: Pagination cursor
            
        Returns:
            Tuple of (list of markets, next cursor or None)
        """
        params = {"status": status, "limit": min(limit, 1000)}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if cursor:
            params["cursor"] = cursor
        
        data = await self._get("/markets", params=params)
        if not data or "markets" not in data:
            return [], None
        
        markets = []
        for m in data["markets"]:
            market = self._parse_market(m)
            if market:
                markets.append(market)
                self._markets_cache[market.ticker] = market
        
        next_cursor = data.get("cursor")
        return markets, next_cursor
    
    async def list_all_markets(
        self,
        status: str = "open",
        max_markets: int = 10000,
        on_progress: Optional[callable] = None,
    ) -> list[KalshiMarket]:
        """
        Fetch all markets with pagination.
        
        Args:
            status: Market status filter
            max_markets: Maximum total markets to fetch
            on_progress: Optional callback(loaded_count) for progress updates
            
        Returns:
            List of all markets
        """
        all_markets = []
        cursor = None
        
        while len(all_markets) < max_markets:
            markets, next_cursor = await self.list_markets(
                status=status,
                limit=1000,
                cursor=cursor,
            )
            
            if not markets:
                break
            
            all_markets.extend(markets)
            logger.info(f"Kalshi: {len(all_markets)} markets loaded...")
            
            # Report progress
            if on_progress:
                try:
                    on_progress(len(all_markets))
                except:
                    pass
            
            if not next_cursor:
                break
            cursor = next_cursor
            
            # Small delay to avoid rate limiting
            await asyncio.sleep(0.2)
        
        logger.info(f"Kalshi: {len(all_markets)} total markets loaded ✓")
        return all_markets[:max_markets]
    
    async def get_market(self, ticker: str) -> Optional[KalshiMarket]:
        """
        Get a specific market by ticker.
        
        Args:
            ticker: Market ticker
            
        Returns:
            KalshiMarket object or None if not found
        """
        # Check cache first
        if ticker in self._markets_cache:
            return self._markets_cache[ticker]
        
        data = await self._get(f"/markets/{ticker}")
        if not data or "market" not in data:
            return None
        
        market = self._parse_market(data["market"])
        if market:
            self._markets_cache[ticker] = market
        return market
    
    def _parse_market(self, data: dict) -> Optional[KalshiMarket]:
        """Parse market data from API response."""
        try:
            # New API uses *_dollars fields (FixedPointDollars strings).
            # Fall back to legacy cents-based fields if dollars not present.
            yes_price = _parse_fp_dollars(data.get("last_price_dollars"))
            if yes_price is None:
                yes_price = data.get("yes_price", 0) / 100.0 if data.get("yes_price") else 0.0

            no_price = 1.0 - yes_price if yes_price > 0 else 0.0

            # Volume: prefer FP string, fall back to int
            volume = _parse_fp_count(data.get("volume_24h_fp"))
            if volume == 0:
                volume = _parse_fp_count(data.get("volume_fp"))
            if volume == 0:
                volume = float(data.get("volume", 0))

            open_interest = _parse_fp_count(data.get("open_interest_fp"))
            if open_interest == 0:
                open_interest = float(data.get("open_interest", 0))

            # Parse close time
            close_time = None
            if data.get("close_time"):
                try:
                    close_time = datetime.fromisoformat(
                        data["close_time"].replace("Z", "+00:00")
                    )
                except Exception:
                    pass

            # Title: prefer yes_sub_title (new API), fall back to title
            title = data.get("title", "") or data.get("yes_sub_title", "")
            subtitle = data.get("subtitle", "") or data.get("no_sub_title", "")

            return KalshiMarket(
                ticker=data.get("ticker", ""),
                event_ticker=data.get("event_ticker", ""),
                series_ticker=data.get("series_ticker", ""),
                title=title,
                subtitle=subtitle,
                yes_price=yes_price,
                no_price=no_price,
                status=data.get("status", ""),
                result=data.get("result"),
                volume=int(volume),
                open_interest=int(open_interest),
                close_time=close_time,
                category=data.get("category", ""),
            )
        except Exception as e:
            logger.warning(f"Failed to parse Kalshi market: {e}")
            return None
    
    # =========================================================================
    # ORDERBOOK ENDPOINTS
    # =========================================================================
    
    async def get_orderbook(self, ticker: str) -> Optional[KalshiOrderBook]:
        """
        Get order book for a market.
        
        Args:
            ticker: Market ticker
            
        Returns:
            KalshiOrderBook object or None if not found
        """
        data = await self._get(f"/markets/{ticker}/orderbook")
        if not data or "orderbook" not in data:
            return None
        
        ob = data["orderbook"]
        
        # Parse YES bids (prices in cents)
        yes_bids = []
        for level in ob.get("yes", []):
            if len(level) >= 2:
                price_cents = level[0]
                quantity = level[1]
                yes_bids.append(PriceLevel(
                    price=price_cents / 100.0,  # Convert to dollars
                    size=float(quantity)
                ))
        
        # Parse NO bids (prices in cents)
        no_bids = []
        for level in ob.get("no", []):
            if len(level) >= 2:
                price_cents = level[0]
                quantity = level[1]
                no_bids.append(PriceLevel(
                    price=price_cents / 100.0,
                    size=float(quantity)
                ))
        
        # Sort bids descending (best/highest first)
        yes_bids.sort(key=lambda x: x.price, reverse=True)
        no_bids.sort(key=lambda x: x.price, reverse=True)
        
        return KalshiOrderBook(
            ticker=ticker,
            yes_bids=yes_bids,
            no_bids=no_bids,
            timestamp=datetime.utcnow(),
        )
    
    async def get_orderbook_unified(self, ticker: str) -> Optional[OrderBook]:
        """
        Get order book in unified format (compatible with Polymarket).
        
        Args:
            ticker: Market ticker
            
        Returns:
            OrderBook object or None if not found
        """
        kalshi_ob = await self.get_orderbook(ticker)
        if not kalshi_ob:
            return None
        return kalshi_ob.to_unified_orderbook()
    
    # =========================================================================
    # STREAMING (Polling-based for public API)
    # =========================================================================
    
    async def stream_orderbooks(
        self,
        tickers: list[str],
        batch_size: int = 100,
        rotation_delay: float = 2.0,
    ) -> AsyncIterator[tuple[str, OrderBook]]:
        """
        Stream order books for multiple markets using polling.
        
        Args:
            tickers: List of market tickers to stream
            batch_size: Number of markets to fetch per batch
            rotation_delay: Delay between batches in seconds
            
        Yields:
            Tuple of (ticker, OrderBook) for each update
        """
        logger.info(f"Starting Kalshi orderbook stream for {len(tickers)} markets")
        
        while True:
            for i in range(0, len(tickers), batch_size):
                batch = tickers[i:i + batch_size]
                logger.debug(f"Fetching Kalshi orderbooks {i+1}-{min(i+batch_size, len(tickers))} of {len(tickers)}")
                
                # Fetch orderbooks in parallel
                tasks = [self.get_orderbook_unified(ticker) for ticker in batch]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                for ticker, result in zip(batch, results):
                    if isinstance(result, Exception):
                        logger.debug(f"Failed to get Kalshi orderbook for {ticker}: {result}")
                        continue
                    if result:
                        yield (ticker, result)
                
                await asyncio.sleep(rotation_delay)
    
    # =========================================================================
    # CATEGORY/SEARCH HELPERS
    # =========================================================================
    
    async def get_markets_by_category(self, category: str) -> list[KalshiMarket]:
        """
        Get all open markets in a category.
        
        Common categories: elections, economics, crypto, tech, entertainment
        """
        # Kalshi API doesn't have a direct category filter, so we fetch all
        # and filter client-side
        all_markets = await self.list_all_markets(status="open")
        return [m for m in all_markets if m.category.lower() == category.lower()]
    
    async def search_markets(self, query: str) -> list[KalshiMarket]:
        """
        Search markets by title.

        Args:
            query: Search query string

        Returns:
            List of matching markets
        """
        all_markets = await self.list_all_markets(status="open")
        query_lower = query.lower()
        return [
            m for m in all_markets
            if query_lower in m.title.lower() or query_lower in m.subtitle.lower()
        ]

    # =========================================================================
    # MULTIVARIATE EVENTS ENDPOINTS
    # =========================================================================

    async def get_multivariate_events(
        self,
        series_ticker: Optional[str] = None,
        collection_ticker: Optional[str] = None,
        with_nested_markets: bool = True,
        limit: int = 200,
        cursor: Optional[str] = None,
    ) -> tuple[list[KalshiEvent], Optional[str]]:
        """
        Get multivariate (multi-outcome) events.

        These are events with mutually exclusive outcomes — the Kalshi
        equivalent of Polymarket's neg-risk events.

        Args:
            series_ticker: Filter by series
            collection_ticker: Filter by collection
            with_nested_markets: Include market data in response
            limit: Max events per page (1-200)
            cursor: Pagination cursor

        Returns:
            (list of events, next cursor)
        """
        params: dict = {
            "limit": min(limit, 200),
            "with_nested_markets": str(with_nested_markets).lower(),
        }
        if series_ticker:
            params["series_ticker"] = series_ticker
        if collection_ticker:
            params["collection_ticker"] = collection_ticker
        if cursor:
            params["cursor"] = cursor

        data = await self._get("/events/multivariate", params=params)
        if not data:
            return [], None

        events = []
        for e in data.get("events", []):
            markets = []
            for m in e.get("markets", []):
                market = self._parse_market(m)
                if market:
                    markets.append(market)

            event = KalshiEvent(
                event_ticker=e.get("event_ticker", ""),
                series_ticker=e.get("series_ticker", ""),
                title=e.get("title", ""),
                category=e.get("category", ""),
                markets=markets,
            )
            events.append(event)

        next_cursor = data.get("cursor")
        return events, next_cursor

    async def get_all_multivariate_events(
        self,
        with_nested_markets: bool = True,
        max_events: int = 5000,
    ) -> list[KalshiEvent]:
        """
        Fetch all multivariate events with pagination.

        Returns:
            List of all multivariate events with nested markets.
        """
        all_events = []
        cursor = None

        while len(all_events) < max_events:
            events, next_cursor = await self.get_multivariate_events(
                with_nested_markets=with_nested_markets,
                limit=200,
                cursor=cursor,
            )
            if not events:
                break

            all_events.extend(events)
            logger.info(f"Kalshi multivariate: {len(all_events)} events loaded...")

            if not next_cursor:
                break
            cursor = next_cursor
            await asyncio.sleep(0.2)

        return all_events[:max_events]

    # =========================================================================
    # CANDLESTICK ENDPOINTS
    # =========================================================================

    async def get_market_candlesticks(
        self,
        ticker: str,
        start_ts: int,
        end_ts: int,
        period_interval: int = 60,
        series_ticker: Optional[str] = None,
    ) -> list[KalshiCandlestick]:
        """
        Get candlestick data for a market.

        Args:
            ticker: Market ticker
            start_ts: Start timestamp (Unix seconds)
            end_ts: End timestamp (Unix seconds)
            period_interval: Candle interval in minutes (1, 60, or 1440)
            series_ticker: Series ticker (required by API path)

        Returns:
            List of KalshiCandlestick objects
        """
        if series_ticker is None:
            # Derive series ticker from market ticker: "SERIES-EVENT-MARKET" -> "SERIES"
            parts = ticker.split("-")
            series_ticker = parts[0] if parts else ticker

        params = {
            "start_ts": start_ts,
            "end_ts": end_ts,
            "period_interval": period_interval,
        }

        data = await self._get(
            f"/series/{series_ticker}/markets/{ticker}/candlesticks",
            params=params,
        )

        if not data or "candlesticks" not in data:
            return []

        return [self._parse_candlestick(c) for c in data["candlesticks"]]

    def _parse_candlestick(self, data: dict) -> KalshiCandlestick:
        """Parse a candlestick from API response."""
        price = data.get("price", {}) or {}
        yes_bid = data.get("yes_bid", {}) or {}
        yes_ask = data.get("yes_ask", {}) or {}

        return KalshiCandlestick(
            end_period_ts=data.get("end_period_ts", 0),
            price_open=_parse_fp_dollars(price.get("open_dollars")),
            price_high=_parse_fp_dollars(price.get("high_dollars")),
            price_low=_parse_fp_dollars(price.get("low_dollars")),
            price_close=_parse_fp_dollars(price.get("close_dollars")),
            price_mean=_parse_fp_dollars(price.get("mean_dollars")),
            price_previous=_parse_fp_dollars(price.get("previous_dollars")),
            yes_bid_open=_parse_fp_dollars(yes_bid.get("open_dollars")),
            yes_bid_high=_parse_fp_dollars(yes_bid.get("high_dollars")),
            yes_bid_low=_parse_fp_dollars(yes_bid.get("low_dollars")),
            yes_bid_close=_parse_fp_dollars(yes_bid.get("close_dollars")),
            yes_ask_open=_parse_fp_dollars(yes_ask.get("open_dollars")),
            yes_ask_high=_parse_fp_dollars(yes_ask.get("high_dollars")),
            yes_ask_low=_parse_fp_dollars(yes_ask.get("low_dollars")),
            yes_ask_close=_parse_fp_dollars(yes_ask.get("close_dollars")),
            volume=_parse_fp_count(data.get("volume_fp")),
            open_interest=_parse_fp_count(data.get("open_interest_fp")),
        )

    # =========================================================================
    # TRADES ENDPOINT
    # =========================================================================

    async def get_market_trades(
        self,
        ticker: str,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> tuple[list[KalshiTrade], Optional[str]]:
        """
        Get recent trades for a market.

        Args:
            ticker: Market ticker
            limit: Max trades to return
            cursor: Pagination cursor

        Returns:
            (list of trades, next cursor)
        """
        params: dict = {"ticker": ticker, "limit": limit}
        if cursor:
            params["cursor"] = cursor

        data = await self._get("/markets/trades", params=params)
        if not data or "trades" not in data:
            return [], None

        trades = []
        for t in data["trades"]:
            try:
                trade = KalshiTrade(
                    trade_id=t.get("trade_id", ""),
                    market_ticker=t.get("ticker", ticker),
                    side=t.get("taker_side", "yes"),
                    price=_parse_fp_dollars(t.get("yes_price_dollars")) or 0.0,
                    count=_parse_fp_count(t.get("count_fp")),
                    ts=t.get("created_time", 0),
                )
                trades.append(trade)
            except Exception as e:
                logger.debug(f"Failed to parse trade: {e}")
                continue

        return trades, data.get("cursor")

