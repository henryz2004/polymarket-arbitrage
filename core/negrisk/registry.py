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

    Performance optimization: Maintains incremental sum_of_asks/sum_of_bids
    per event, updated on each price tick. This allows the engine to skip
    full detection on events that are nowhere near an opportunity.
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

        # Incremental sum tracking for fast opportunity pre-filtering
        # Updated on each price tick, avoids full detection on cold events
        self._event_sum_asks: dict[str, Optional[float]] = {}   # event_id -> sum of best asks
        self._event_sum_bids: dict[str, Optional[float]] = {}   # event_id -> sum of best bids
        self._event_coverage: dict[str, int] = {}               # event_id -> count of priced outcomes

        # Pre-classified gamma-only tokens (refreshed at registry refresh)
        self._gamma_only_tokens: set[str] = set()

    async def start(self) -> None:
        """Start the registry with initial fetch and periodic refresh."""
        if self._running:
            return

        self._running = True
        self._http_client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=5,
                keepalive_expiry=60.0,
            ),
        )

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

            # Preserve WebSocket/CLOB-sourced BBA data from existing events.
            # Registry refresh creates new Outcome objects with gamma-sourced BBA,
            # which would destroy real-time price data from WebSocket. Carry forward
            # any non-gamma BBA to the new outcome objects.
            for event_id, new_event in new_events.items():
                old_event = self._events.get(event_id)
                if not old_event:
                    continue
                # Build lookup: token_id -> old outcome BBA
                old_bba_by_token: dict[str, "OutcomeBBA"] = {}
                for old_outcome in old_event.outcomes:
                    if old_outcome.token_id and old_outcome.bba.source != "gamma":
                        old_bba_by_token[old_outcome.token_id] = old_outcome.bba
                # Carry forward non-gamma BBA to new outcomes
                for new_outcome in new_event.outcomes:
                    if new_outcome.token_id and new_outcome.token_id in old_bba_by_token:
                        new_outcome.bba = old_bba_by_token[new_outcome.token_id]

            self._events = new_events
            self._token_to_outcome = new_token_map
            self._last_refresh = datetime.utcnow()

            # Recompute incremental sums for all events
            self._event_sum_asks.clear()
            self._event_sum_bids.clear()
            self._event_coverage.clear()
            for event in new_events.values():
                self._recompute_event_sums(event)

            # Pre-classify gamma-only tokens
            self._gamma_only_tokens = {
                o.token_id
                for event in new_events.values()
                for o in event.active_outcomes
                if o.token_id and o.bba.source == "gamma"
            }

            # Calculate priority scores for all events
            self._calculate_priority_scores()

            mode_label = "all" if self.config.watchdog_mode else "neg-risk"
            logger.info(
                f"Registry refreshed: {len(self._events)} {mode_label} events, "
                f"{len(self._token_to_outcome)} tokens"
            )

        except Exception as e:
            logger.error(f"Failed to fetch neg-risk events: {e}")

    def _calculate_priority_scores(self) -> None:
        """
        Calculate priority scores for all events based on:
        1. Resolution proximity — exponential ramp as resolution approaches
        2. Volume spike — high recent volume signals active trading
        3. Spread volatility — wide spreads = more price uncertainty = more arb potential
        """
        if not self.config.prioritize_near_resolution:
            return

        # Calculate average volume for spike detection
        volumes = [e.volume_24h for e in self._events.values() if e.volume_24h > 0]
        avg_volume = sum(volumes) / len(volumes) if volumes else 0

        now = datetime.utcnow()

        for event in self._events.values():
            score = 0.0

            # 1. Resolution proximity (0-1 score, exponential ramp)
            if event.end_date:
                hours_remaining = (event.end_date - now).total_seconds() / 3600
                event.hours_to_resolution = max(0, hours_remaining)

                if 0 < hours_remaining < self.config.resolution_window_hours:
                    # Exponential ramp: last hours are disproportionately valuable
                    # At 24h: 0.0, at 12h: 0.25, at 6h: 0.5, at 1h: 0.87, at 0h: 1.0
                    linear = 1.0 - (hours_remaining / self.config.resolution_window_hours)
                    score += linear * linear  # Quadratic ramp (0 to 1.0)

            # 2. Volume spike bonus (0-0.5 score)
            if avg_volume > 0 and event.volume_24h > avg_volume * self.config.volume_spike_threshold:
                spike_ratio = min(event.volume_24h / avg_volume, 5.0)  # Cap at 5x
                score += (spike_ratio - self.config.volume_spike_threshold) * 0.25  # Up to 0.75
                score = min(score, 1.5)  # Cap total score

            # 3. Spread volatility — avg spread across active outcomes
            spreads = []
            for o in event.active_outcomes:
                if o.bba.spread is not None:
                    spreads.append(o.bba.spread)
            if spreads:
                avg_spread = sum(spreads) / len(spreads)
                event.spread_volatility = round(avg_spread, 4)
                # Wide spreads (>5c avg) add priority — more price uncertainty
                if avg_spread > 0.05:
                    spread_bonus = min((avg_spread - 0.05) * 2.0, 0.3)  # Up to 0.3
                    score += spread_bonus
                    score = min(score, 1.5)

            event.priority_score = round(score, 3)

    def _is_negrisk_event(self, event_data: dict) -> bool:
        """Check if an event should be tracked.

        In normal mode: only neg-risk events.
        In watchdog mode: ALL events (neg-risk + non-neg-risk) so the watchdog
        can monitor non-neg-risk multi-outcome markets like "US x Iran ceasefire by...?"
        """
        # In watchdog mode, accept all events (keyword filtering is done by WatchdogEngine)
        if not self.config.watchdog_mode:
            # Normal mode: require neg-risk flag
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

            # Parse end_date from Gamma API response
            end_date = None
            end_date_str = event_data.get("endDate") or event_data.get("end_date_iso")
            if end_date_str:
                try:
                    end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    # Convert to UTC if timezone-aware
                    if end_date.tzinfo is not None:
                        end_date = end_date.replace(tzinfo=None)  # Store as naive UTC
                except (ValueError, TypeError, AttributeError):
                    pass

            # Check actual neg-risk status from event data
            is_neg_risk = bool(
                event_data.get("negRisk", False) or event_data.get("enableNegRisk", False)
            )

            event = NegriskEvent(
                event_id=event_id,
                slug=event_data.get("slug", ""),
                title=event_data.get("title", "") or event_data.get("question", ""),
                condition_id=event_data.get("conditionId", ""),
                category=str(event_data.get("category", "") or "").lower(),
                outcomes=outcomes,
                neg_risk=is_neg_risk,
                neg_risk_augmented=neg_risk_augmented,
                volume_24h=float(event_data.get("volume24hr", 0) or 0),
                liquidity=float(event_data.get("liquidity", 0) or 0),
                end_date=end_date,
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

    def get_event_ids(self) -> list[str]:
        """Get all tracked event IDs."""
        return list(self._events.keys())

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
        bid_levels: Optional[list] = None,
        ask_levels: Optional[list] = None,
    ) -> None:
        """Update BBA for an outcome by token ID, maintaining incremental sums."""
        result = self.get_event_by_token(token_id)
        if not result:
            return

        event, outcome = result

        # Incrementally update sum_of_asks and sum_of_bids
        # Subtract old contribution, add new contribution
        old_ask = outcome.bba.best_ask
        old_bid = outcome.bba.best_bid
        event_id = event.event_id

        outcome.bba.best_bid = best_bid
        outcome.bba.best_ask = best_ask
        outcome.bba.bid_size = bid_size
        outcome.bba.ask_size = ask_size
        outcome.bba.last_updated = datetime.utcnow()
        outcome.bba.sequence_id = sequence_id
        outcome.bba.source = source
        if bid_levels is not None:
            outcome.bba.bid_levels = bid_levels
        if ask_levels is not None:
            outcome.bba.ask_levels = ask_levels

        # Update incremental ask sum
        current_sum_asks = self._event_sum_asks.get(event_id)
        if current_sum_asks is not None:
            if old_ask is not None:
                current_sum_asks -= old_ask
            if best_ask is not None:
                current_sum_asks += best_ask
            self._event_sum_asks[event_id] = current_sum_asks
        elif best_ask is not None:
            # First time — compute full sum
            self._recompute_event_sums(event)

        # Update incremental bid sum
        current_sum_bids = self._event_sum_bids.get(event_id)
        if current_sum_bids is not None:
            if old_bid is not None:
                current_sum_bids -= old_bid
            if best_bid is not None:
                current_sum_bids += best_bid
            self._event_sum_bids[event_id] = current_sum_bids
        elif best_bid is not None:
            self._recompute_event_sums(event)

        # Track coverage (how many outcomes have prices)
        if old_ask is None and best_ask is not None:
            self._event_coverage[event_id] = self._event_coverage.get(event_id, 0) + 1
        elif old_ask is not None and best_ask is None:
            self._event_coverage[event_id] = max(0, self._event_coverage.get(event_id, 0) - 1)

        # Remove from gamma-only set if we got real data
        if source in ("clob", "websocket") and token_id in self._gamma_only_tokens:
            self._gamma_only_tokens.discard(token_id)

    def _recompute_event_sums(self, event: NegriskEvent) -> None:
        """Recompute full sum_of_asks and sum_of_bids for an event."""
        active = event.active_outcomes
        asks = [o.bba.best_ask for o in active if o.bba.best_ask is not None]
        bids = [o.bba.best_bid for o in active if o.bba.best_bid is not None]
        self._event_sum_asks[event.event_id] = sum(asks) if asks else None
        self._event_sum_bids[event.event_id] = sum(bids) if bids else None
        self._event_coverage[event.event_id] = len(asks)

    def get_event_proximity(self, event_id: str) -> dict:
        """
        Get incremental opportunity proximity data for an event.

        Returns dict with sum_asks, sum_bids, coverage, and
        approximate gross edges. Used for fast pre-filtering.
        """
        return {
            "sum_asks": self._event_sum_asks.get(event_id),
            "sum_bids": self._event_sum_bids.get(event_id),
            "coverage": self._event_coverage.get(event_id, 0),
        }

    def is_near_opportunity(self, event_id: str, threshold: float = 0.05) -> bool:
        """
        Fast check: is this event within `threshold` of an arb opportunity?

        Uses incremental sums (no recomputation). Returns True if:
        - BUY side: sum_asks < 1.0 + threshold  (e.g. < 1.05)
        - SELL side: sum_bids > 1.0 - threshold  (e.g. > 0.95)
        - Coverage is incomplete (might be near opportunity with missing data)

        This is a fast pre-filter — full detection still runs on True.
        """
        event = self._events.get(event_id)
        if not event:
            return False

        num_active = len(event.active_outcomes)
        coverage = self._event_coverage.get(event_id, 0)

        # If coverage is incomplete, can't rule out opportunity
        if coverage < num_active:
            return True

        sum_asks = self._event_sum_asks.get(event_id)
        sum_bids = self._event_sum_bids.get(event_id)

        if sum_asks is not None and sum_asks < 1.0 + threshold:
            return True
        if sum_bids is not None and sum_bids > 1.0 - threshold:
            return True

        return False

    def is_gamma_only(self, token_id: str) -> bool:
        """Check if a token is pre-classified as gamma-only (no CLOB/WS data)."""
        return token_id in self._gamma_only_tokens

    def get_gamma_only_count(self) -> int:
        """Get count of tokens with only gamma-sourced data."""
        return len(self._gamma_only_tokens)

    def get_near_opportunity_events(self, threshold: float = 0.05) -> list[NegriskEvent]:
        """
        Get events that are within `threshold` of an arb opportunity.

        Much faster than get_tradeable_events() + full detection because
        it uses pre-computed incremental sums to filter.
        """
        near = []
        for event_id, event in self._events.items():
            if self.is_near_opportunity(event_id, threshold):
                near.append(event)
        return near

    def get_stats(self) -> dict:
        """Get registry statistics."""
        total_outcomes = sum(len(e.outcomes) for e in self._events.values())
        active_outcomes = sum(len(e.active_outcomes) for e in self._events.values())

        # Count events near opportunity threshold
        near_opp_count = sum(
            1 for eid in self._events
            if self.is_near_opportunity(eid, 0.05)
        )

        return {
            "events_tracked": len(self._events),
            "total_outcomes": total_outcomes,
            "active_outcomes": active_outcomes,
            "tokens_tracked": len(self._token_to_outcome),
            "gamma_only_tokens": len(self._gamma_only_tokens),
            "near_opportunity_events": near_opp_count,
            "last_refresh": self._last_refresh.isoformat() if self._last_refresh else None,
        }
