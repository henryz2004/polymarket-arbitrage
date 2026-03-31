"""
Limitless Exchange Registry
==============================

Discovers and tracks neg-risk group markets from Limitless Exchange API.

Follows the same pattern as NegriskRegistry (core/negrisk/registry.py):
- Fetches events, filters for neg-risk groups
- Groups sub-markets by parent group slug
- Maps to NegriskEvent / Outcome / OutcomeBBA
- Periodic refresh
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from core.negrisk.fee_models import LimitlessFeeModel
from core.negrisk.models import (
    NegriskConfig,
    NegriskEvent,
    Outcome,
    OutcomeBBA,
    OutcomeStatus,
)
from core.negrisk.platforms.limitless.api_client import LimitlessAPIClient


logger = logging.getLogger(__name__)


class LimitlessRegistry:
    """
    Registry for Limitless neg-risk group markets.

    Satisfies RegistryProtocol via structural subtyping.
    """

    def __init__(self, config: NegriskConfig, api_client: Optional[LimitlessAPIClient] = None):
        self.config = config
        self._api_client = api_client or LimitlessAPIClient()
        self._owns_client = api_client is None

        self._events: dict[str, NegriskEvent] = {}
        self._token_to_outcome: dict[str, tuple[str, str]] = {}
        self._refresh_task: Optional[asyncio.Task] = None
        self._running = False
        self._last_refresh: Optional[datetime] = None

    async def start(self) -> None:
        """Start the registry with initial fetch and periodic refresh."""
        if self._running:
            return

        self._running = True

        if self._owns_client:
            await self._api_client.start()

        await self._fetch_markets()

        self._refresh_task = asyncio.create_task(
            self._refresh_loop(),
            name="limitless_registry_refresh",
        )

        logger.info(f"LimitlessRegistry started — tracking {len(self._events)} group events")

    async def stop(self) -> None:
        """Stop the registry."""
        self._running = False

        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass

        if self._owns_client:
            await self._api_client.stop()

        logger.info("LimitlessRegistry stopped")

    async def _refresh_loop(self) -> None:
        """Periodically refresh the market list."""
        while self._running:
            try:
                await asyncio.sleep(self.config.registry_refresh_seconds)
                await self._fetch_markets()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Limitless registry refresh error: {e}")
                await asyncio.sleep(5)

    async def _fetch_markets(self) -> None:
        """Fetch all active markets from Limitless API and filter for neg-risk groups."""
        try:
            all_markets = await self._api_client.get_all_active_markets()

            new_events: dict[str, NegriskEvent] = {}
            new_token_map: dict[str, tuple[str, str]] = {}

            for market_data in all_markets:
                if market_data.get("marketType") != "group":
                    continue

                event = self._parse_group_market(market_data)
                if event and len(event.active_outcomes) >= self.config.min_outcomes:
                    new_events[event.event_id] = event
                    for outcome in event.outcomes:
                        if outcome.token_id:
                            new_token_map[outcome.token_id] = (event.event_id, outcome.outcome_id)

            # Preserve existing BBA data from the tracker.
            # Without this, the refresh overwrites CLOB-sourced BBA (including
            # best_ask=None for empty books) with stale gamma midpoint prices,
            # causing false-positive opportunity alerts.
            for event_id, new_event in new_events.items():
                old_event = self._events.get(event_id)
                if not old_event:
                    continue
                old_outcomes = {o.outcome_id: o for o in old_event.outcomes}
                for outcome in new_event.outcomes:
                    old = old_outcomes.get(outcome.outcome_id)
                    if old and old.bba.source in ("clob", "websocket"):
                        outcome.bba = old.bba

            self._events = new_events
            self._token_to_outcome = new_token_map
            self._last_refresh = datetime.utcnow()

            logger.info(
                f"Limitless registry refreshed: {len(self._events)} group events, "
                f"{len(self._token_to_outcome)} tokens"
            )

        except Exception as e:
            logger.error(f"Failed to fetch Limitless markets: {e}")

    def _parse_group_market(self, data: dict) -> Optional[NegriskEvent]:
        """Parse a Limitless group market into a NegriskEvent."""
        try:
            group_slug = data.get("slug", "")
            if not group_slug:
                return None

            sub_markets = data.get("markets", [])
            if len(sub_markets) < self.config.min_outcomes:
                return None

            # Check volume threshold
            volume_str = data.get("volumeFormatted") or data.get("volume", "0")
            try:
                volume = float(volume_str)
            except (ValueError, TypeError):
                volume = 0.0

            if volume < self.config.min_event_volume_24h:
                return None

            # Parse outcomes from sub-markets
            outcomes = []
            api_yes_prices = []
            for sub in sub_markets:
                outcome = self._parse_sub_market(sub, group_slug)
                if outcome:
                    outcomes.append(outcome)
                # Collect API YES prices for mutual-exclusivity check
                prices = sub.get("prices", [])
                if isinstance(prices, list) and len(prices) >= 2:
                    try:
                        api_yes_prices.append(float(prices[0]))
                    except (ValueError, TypeError):
                        pass

            if len(outcomes) < self.config.min_outcomes:
                return None

            # Filter non-mutually-exclusive groups: in a true neg-risk market,
            # the sum of YES probabilities should be near 1.0. If it's well above
            # 1.0 (e.g. 1.5+), the sub-markets are independent events, not
            # mutually exclusive outcomes. Threshold: 1.5 to allow for market
            # inefficiency in legit neg-risk markets while catching independent groups.
            if api_yes_prices and len(api_yes_prices) >= 3:
                sum_yes = sum(api_yes_prices)
                if sum_yes > 1.5:
                    logger.debug(
                        f"Skipping non-mutually-exclusive group: {group_slug} "
                        f"(sum of YES prices = {sum_yes:.2f}, expected ~1.0)"
                    )
                    return None

            # Parse expiration
            end_date = None
            exp_ts = data.get("expirationTimestamp")
            if exp_ts:
                try:
                    end_date = datetime.utcfromtimestamp(exp_ts / 1000.0)
                except (ValueError, TypeError, OSError):
                    pass

            # Skip expired events
            if end_date and end_date < datetime.utcnow():
                logger.debug(f"Skipping expired event: {group_slug} (expired {end_date})")
                return None

            # Skip events beyond max horizon
            if end_date and self.config.max_horizon_days > 0:
                from datetime import timedelta
                max_end = datetime.utcnow() + timedelta(days=self.config.max_horizon_days)
                if end_date > max_end:
                    logger.debug(
                        f"Skipping long-horizon event: {group_slug} "
                        f"(ends {end_date.date()}, max horizon {self.config.max_horizon_days:.0f}d)"
                    )
                    return None

            # Parse creation time
            created_at = None
            created_str = data.get("createdAt")
            if created_str:
                try:
                    # ISO 8601 format: "2026-02-02T23:04:45.112Z"
                    created_at = datetime.fromisoformat(created_str.replace("Z", "+00:00")).replace(tzinfo=None)
                except (ValueError, TypeError):
                    pass

            # Estimate dynamic fee based on lifecycle position
            fee_rate_bps = LimitlessFeeModel.estimate_fee_bps(
                created_at=created_at,
                end_date=end_date,
            )

            event = NegriskEvent(
                event_id=group_slug,
                slug=group_slug,
                title=data.get("title", ""),
                condition_id=data.get("negRiskMarketId", ""),
                platform="limitless",
                outcomes=outcomes,
                neg_risk=True,
                neg_risk_augmented=False,
                volume_24h=volume,
                liquidity=0.0,
                end_date=end_date,
                created_at=created_at,
                fee_rate_bps=fee_rate_bps,
                last_updated=datetime.utcnow(),
            )

            return event

        except Exception as e:
            logger.debug(f"Failed to parse Limitless group market: {e}")
            return None

    def _parse_sub_market(self, sub: dict, group_slug: str) -> Optional[Outcome]:
        """Parse a Limitless sub-market into an Outcome."""
        try:
            sub_slug = sub.get("slug", "")
            sub_id = sub.get("id")
            if not sub_slug or sub_id is None:
                return None

            tokens = sub.get("tokens", {})
            yes_token = str(tokens.get("yes", ""))
            if not yes_token:
                return None

            title = sub.get("title", "")
            neg_risk_req = sub.get("negRiskRequestId")

            # Parse initial prices — prices array is [yes_price, no_price]
            prices = sub.get("prices", [])
            best_ask = None
            if isinstance(prices, list) and len(prices) >= 2:
                # prices[0] is the YES price, prices[1] is the NO price
                best_ask = float(prices[0])

            # Determine status
            status = OutcomeStatus.ACTIVE
            name_lower = title.lower()
            if "other" in name_lower:
                status = OutcomeStatus.OTHER
            elif "placeholder" in name_lower or "unnamed" in name_lower:
                status = OutcomeStatus.PLACEHOLDER

            outcome = Outcome(
                outcome_id=f"{sub_id}_yes",
                market_id=sub_slug,  # Used for orderbook API
                condition_id=neg_risk_req or "",
                token_id=yes_token,
                name=title,
                status=status,
                bba=OutcomeBBA(
                    best_ask=best_ask,
                    last_updated=datetime.utcnow(),
                    source="gamma",  # API-only initially, same provenance label
                ),
                volume_24h=0.0,
                liquidity=0.0,
            )

            return outcome

        except Exception as e:
            logger.debug(f"Failed to parse Limitless sub-market: {e}")
            return None

    # ── RegistryProtocol interface ──

    def get_event(self, event_id: str) -> Optional[NegriskEvent]:
        """Get a specific event by ID (group slug)."""
        return self._events.get(event_id)

    def get_all_events(self) -> list[NegriskEvent]:
        """Get all tracked group events."""
        return list(self._events.values())

    def get_tradeable_events(self) -> list[NegriskEvent]:
        """Get events suitable for trading."""
        tradeable = []
        for event in self._events.values():
            tradeable_outcomes = [
                o for o in event.outcomes if o.is_tradeable(self.config)
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
        """Get all token IDs."""
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
        if bid_levels is not None:
            outcome.bba.bid_levels = bid_levels
        if ask_levels is not None:
            outcome.bba.ask_levels = ask_levels

    def get_stats(self) -> dict:
        """Get registry statistics."""
        total_outcomes = sum(len(e.outcomes) for e in self._events.values())
        active_outcomes = sum(len(e.active_outcomes) for e in self._events.values())

        return {
            "platform": "limitless",
            "events_tracked": len(self._events),
            "total_outcomes": total_outcomes,
            "active_outcomes": active_outcomes,
            "tokens_tracked": len(self._token_to_outcome),
            "last_refresh": self._last_refresh.isoformat() if self._last_refresh else None,
        }
