"""
Limitless Exchange API Client
===============================

Thin async httpx wrapper for the Limitless REST API.

Endpoints:
- GET /markets/active?page=N    — paginated active markets
- GET /markets/{slug}/orderbook  — bids/asks arrays for a sub-market
"""

import asyncio
import logging
from typing import Optional

import httpx


logger = logging.getLogger(__name__)

# Retry config for 429 rate limits
_MAX_RETRIES = 3
_INITIAL_BACKOFF = 2.0  # seconds


class LimitlessAPIClient:
    """Async HTTP client for Limitless Exchange REST API."""

    BASE_URL = "https://api.limitless.exchange"

    def __init__(self, timeout: float = 30.0):
        self._client: Optional[httpx.AsyncClient] = None
        self._timeout = timeout

    async def start(self) -> None:
        """Initialize the HTTP client."""
        self._client = httpx.AsyncClient(timeout=self._timeout)

    async def stop(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _request_with_retry(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Make an HTTP request with retry-on-429 and exponential backoff."""
        backoff = _INITIAL_BACKOFF
        for attempt in range(_MAX_RETRIES + 1):
            resp = await self._client.request(method, url, **kwargs)
            if resp.status_code != 429:
                resp.raise_for_status()
                return resp
            if attempt < _MAX_RETRIES:
                logger.debug(f"429 rate-limited on {url}, retrying in {backoff:.0f}s (attempt {attempt + 1}/{_MAX_RETRIES})")
                await asyncio.sleep(backoff)
                backoff *= 2
        # Final attempt was also 429 — raise
        resp.raise_for_status()
        return resp  # unreachable, raise_for_status throws

    async def get_active_markets(self, page: int = 1) -> dict:
        """
        Fetch a page of active markets.

        Args:
            page: Page number (1-indexed).

        Returns:
            Dict with 'data' (list of markets) and 'totalMarketsCount'.
        """
        resp = await self._request_with_retry(
            "GET",
            f"{self.BASE_URL}/markets/active",
            params={"page": page},
        )
        return resp.json()

    async def get_all_active_markets(self) -> list[dict]:
        """
        Fetch all active markets with pagination.

        Returns:
            Full list of market dicts.
        """
        all_markets = []
        page = 1

        while True:
            result = await self.get_active_markets(page=page)

            data = result.get("data", [])
            if not data:
                break

            all_markets.extend(data)
            total = result.get("totalMarketsCount", 0)

            if len(all_markets) >= total:
                break

            page += 1

        return all_markets

    async def get_orderbook(self, slug: str) -> dict:
        """
        Fetch orderbook for a sub-market.

        Args:
            slug: Sub-market slug (e.g. "manchester-city-1771008464998")

        Returns:
            Dict with 'bids', 'asks', 'tokenId', 'midpoint', etc.
            Bids/asks entries: {"price": float, "size": int, "side": str}
        """
        resp = await self._request_with_retry(
            "GET",
            f"{self.BASE_URL}/markets/{slug}/orderbook",
        )
        return resp.json()
