"""
Negrisk Market Registry
========================

Discovers and tracks neg-risk events from Polymarket's Gamma API.

The registry:
1. Fetches events with negRisk=true from Gamma API
2. Groups markets by event/condition ID
3. Filters out augmented placeholder outcomes
4. Maintains a cached list of tradeable events
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

import httpx

from core.negrisk.models import (
    NegriskConfig,
    NegriskEvent,
    Outcome,
    OutcomeBBA,
    OutcomeStatus,
)


logger = logging.getLogger(__name__)


class NegriskRegistry:
    """
    Registry for neg-risk events.

    Maintains a list of neg-risk events with their outcomes,
    refreshing periodically from the Gamma API.
    """

    GAMMA_API_URL = "https://gamma-api.polymarket.com"

    def __init__(self, config: NegriskConfig):
        self.config = config
        self._events: dict[str, NegriskEvent] = {}
        self._token_to_outcome: dict[str, tuple[str, str]] = {}  # token_id -> (event_id, outcome_id)
        self._http_client: Optional[httpx.AsyncClient] = None
        self._refresh_task: Optional[asyncio.Task] = None
        self._running = False
        self._last_refresh: Optional[datetime] = None

    async def start(self) -> None:
        """Start the registry with initial fetch and periodic refresh."""
        if self._running:
            return

        self._running = True
        self._http_client = httpx.AsyncClient(timeout=30.0)

        # Initial fetch
        await self._fetch_negrisk_events()

        # Start background refresh
        self._refresh_task = asyncio.create_task(
            self._refresh_loop(),
            name="negrisk_registry_refresh"
        )

        logger.info(f"NegriskRegistry started - tracking {len(self._events)} events")

    async def stop(self) -> None:
        """Stop the registry."""
        self._running = False

        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass

        if self._http_client:
            await self._http_client.aclose()

        logger.info("NegriskRegistry stopped")

    async def _refresh_loop(self) -> None:
        """Periodically refresh the event list."""
        while self._running:
            try:
                await asyncio.sleep(self.config.registry_refresh_seconds)
                await self._fetch_negrisk_events()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Registry refresh error: {e}")
                await asyncio.sleep(5)

    async def _fetch_negrisk_events(self) -> None:
        """Fetch all neg-risk events from Gamma API."""
        try:
            all_events = []
            offset = 0
            limit = 100

            logger.debug("Fetching neg-risk events from Gamma API...")

            while self._running:
                resp = await self._http_client.get(
                    f"{self.GAMMA_API_URL}/events",
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

                events = resp.json()
                if not events:
                    break

                # Filter for neg-risk events
                for event_data in events:
                    if self._is_negrisk_event(event_data):
                        all_events.append(event_data)

                offset += limit
                if len(events) < limit:
                    break

                # Rate limiting
                await asyncio.sleep(0.1)

            # Process events
            new_events: dict[str, NegriskEvent] = {}
            new_token_map: dict[str, tuple[str, str]] = {}

            for event_data in all_events:
                event = self._parse_event(event_data)
                if event and len(event.active_outcomes) >= self.config.min_outcomes:
                    new_events[event.event_id] = event
                    for outcome in event.outcomes:
                        if outcome.token_id:
                            new_token_map[outcome.token_id] = (event.event_id, outcome.outcome_id)

            self._events = new_events
            self._token_to_outcome = new_token_map
            self._last_refresh = datetime.utcnow()

            logger.info(
                f"Registry refreshed: {len(self._events)} neg-risk events, "
                f"{len(self._token_to_outcome)} tokens"
            )

        except Exception as e:
            logger.error(f"Failed to fetch neg-risk events: {e}")

    def _is_negrisk_event(self, event_data: dict) -> bool:
        """Check if an event is a neg-risk event."""
        # Check negRisk or enableNegRisk flag
        neg_risk = event_data.get("negRisk", False) or event_data.get("enableNegRisk", False)
        if not neg_risk:
            return False

        # Must have markets
        markets = event_data.get("markets", [])
        if len(markets) < self.config.min_outcomes:
            return False

        # Check volume threshold
        volume = float(event_data.get("volume24hr", 0) or 0)
        if volume < self.config.min_event_volume_24h:
            return False

        return True

    def _parse_event(self, event_data: dict) -> Optional[NegriskEvent]:
        """Parse event data from Gamma API."""
        try:
            event_id = str(event_data.get("id", ""))
            if not event_id:
                return None

            # Parse markets as outcomes
            markets = event_data.get("markets", [])
            outcomes = []

            for market_data in markets:
                outcome = self._parse_outcome(market_data)
                if outcome:
                    outcomes.append(outcome)

            if len(outcomes) < self.config.min_outcomes:
                return None

            # Check for augmented neg-risk (has placeholders)
            neg_risk_augmented = event_data.get("negRiskAugmented", False)

            event = NegriskEvent(
                event_id=event_id,
                slug=event_data.get("slug", ""),
                title=event_data.get("title", "") or event_data.get("question", ""),
                condition_id=event_data.get("conditionId", ""),
                outcomes=outcomes,
                neg_risk=True,
                neg_risk_augmented=neg_risk_augmented,
                volume_24h=float(event_data.get("volume24hr", 0) or 0),
                liquidity=float(event_data.get("liquidity", 0) or 0),
                last_updated=datetime.utcnow(),
            )

            return event

        except Exception as e:
            logger.debug(f"Failed to parse event: {e}")
            return None

    def _parse_outcome(self, market_data: dict) -> Optional[Outcome]:
        """Parse a market as an outcome."""
        try:
            market_id = str(market_data.get("id", ""))
            if not market_id:
                return None

            # Get token ID (YES token)
            clob_token_ids = market_data.get("clobTokenIds", "")
            token_id = ""

            if clob_token_ids:
                try:
                    if isinstance(clob_token_ids, str):
                        token_ids = json.loads(clob_token_ids)
                    else:
                        token_ids = clob_token_ids

                    if isinstance(token_ids, list) and len(token_ids) > 0:
                        # First token is YES
                        token_id = str(token_ids[0]).strip()
                except (json.JSONDecodeError, TypeError):
                    pass

            if not token_id:
                return None

            # Determine outcome name
            question = market_data.get("question", "")
            outcomes_list = market_data.get("outcomes", [])

            # Parse outcome name from question or outcomes list
            name = question
            if isinstance(outcomes_list, str):
                try:
                    outcomes_list = json.loads(outcomes_list)
                except:
                    pass

            if isinstance(outcomes_list, list) and len(outcomes_list) > 0:
                # First outcome is the "YES" outcome name for this market
                name = str(outcomes_list[0])

            # Determine status
            status = OutcomeStatus.ACTIVE
            if "other" in name.lower():
                status = OutcomeStatus.OTHER
            elif "unnamed" in name.lower() or "placeholder" in name.lower():
                status = OutcomeStatus.PLACEHOLDER

            # Parse current prices for initial BBA
            outcome_prices = market_data.get("outcomePrices", "")
            best_ask = None

            if outcome_prices:
                try:
                    if isinstance(outcome_prices, str):
                        prices = json.loads(outcome_prices)
                    else:
                        prices = outcome_prices

                    if isinstance(prices, list) and len(prices) > 0:
                        best_ask = float(prices[0])
                except:
                    pass

            outcome = Outcome(
                outcome_id=f"{market_id}_yes",
                market_id=market_id,
                condition_id=market_data.get("conditionId", ""),
                token_id=token_id,
                name=name,
                status=status,
                bba=OutcomeBBA(
                    best_ask=best_ask,
                    last_updated=datetime.utcnow(),
                    source="gamma",
                ),
                volume_24h=float(market_data.get("volume24hr", 0) or 0),
                liquidity=float(market_data.get("liquidity", 0) or 0),
            )

            return outcome

        except Exception as e:
            logger.debug(f"Failed to parse outcome: {e}")
            return None

    def get_event(self, event_id: str) -> Optional[NegriskEvent]:
        """Get a specific event by ID."""
        return self._events.get(event_id)

    def get_all_events(self) -> list[NegriskEvent]:
        """Get all tracked events."""
        return list(self._events.values())

    def get_tradeable_events(self) -> list[NegriskEvent]:
        """
        Get events that are suitable for trading.

        Filters out events with:
        - Too few active outcomes
        - Insufficient volume
        - Only placeholder outcomes
        """
        tradeable = []

        for event in self._events.values():
            # Count tradeable outcomes
            tradeable_outcomes = [
                o for o in event.outcomes
                if o.is_tradeable(self.config)
            ]

            if len(tradeable_outcomes) >= self.config.min_outcomes:
                tradeable.append(event)

        return tradeable

    def get_event_by_token(self, token_id: str) -> Optional[tuple[NegriskEvent, Outcome]]:
        """Get event and outcome for a token ID."""
        if token_id not in self._token_to_outcome:
            return None

        event_id, outcome_id = self._token_to_outcome[token_id]
        event = self._events.get(event_id)

        if not event:
            return None

        for outcome in event.outcomes:
            if outcome.outcome_id == outcome_id:
                return (event, outcome)

        return None

    def get_all_token_ids(self) -> list[str]:
        """Get all token IDs for WebSocket subscription."""
        return list(self._token_to_outcome.keys())

    def update_outcome_bba(
        self,
        token_id: str,
        best_bid: Optional[float],
        best_ask: Optional[float],
        bid_size: Optional[float] = None,
        ask_size: Optional[float] = None,
        sequence_id: Optional[int] = None,
        source: str = "unknown",
    ) -> None:
        """Update BBA for an outcome by token ID."""
        result = self.get_event_by_token(token_id)
        if not result:
            return

        event, outcome = result
        outcome.bba.best_bid = best_bid
        outcome.bba.best_ask = best_ask
        outcome.bba.bid_size = bid_size
        outcome.bba.ask_size = ask_size
        outcome.bba.last_updated = datetime.utcnow()
        outcome.bba.sequence_id = sequence_id
        outcome.bba.source = source

    def get_stats(self) -> dict:
        """Get registry statistics."""
        total_outcomes = sum(len(e.outcomes) for e in self._events.values())
        active_outcomes = sum(len(e.active_outcomes) for e in self._events.values())

        return {
            "events_tracked": len(self._events),
            "total_outcomes": total_outcomes,
            "active_outcomes": active_outcomes,
            "tokens_tracked": len(self._token_to_outcome),
            "last_refresh": self._last_refresh.isoformat() if self._last_refresh else None,
        }
