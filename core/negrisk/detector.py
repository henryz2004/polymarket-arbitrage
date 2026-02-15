"""
Negrisk Opportunity Detector
==============================

Detects arbitrage opportunities in neg-risk events.

The core logic:
1. Sum all outcome ask prices
2. If sum < $1.00 - fees - gas, there's an arbitrage opportunity
3. Buying all outcomes guarantees a profit when one resolves to YES
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional

from core.negrisk.models import (
    NegriskConfig,
    NegriskEvent,
    NegriskOpportunity,
    NegriskStats,
)


logger = logging.getLogger(__name__)


class NegriskDetector:
    """
    Detects neg-risk arbitrage opportunities.

    Scans events for situations where:
    sum(all outcome asks) + fees + gas < $1.00

    This guarantees profit since exactly one outcome will resolve to $1.00.
    """

    def __init__(self, config: NegriskConfig):
        self.config = config
        self.stats = NegriskStats()

        # Track recent opportunities to avoid duplicates
        self._recent_opportunities: dict[str, NegriskOpportunity] = {}
        self._opportunity_cooldown: dict[str, datetime] = {}

    def detect_opportunities(self, events: list[NegriskEvent]) -> list[NegriskOpportunity]:
        """
        Scan all events for arbitrage opportunities.

        Args:
            events: List of neg-risk events to scan

        Returns:
            List of detected opportunities
        """
        opportunities = []

        for event in events:
            opportunity = self._check_event(event)
            if opportunity:
                opportunities.append(opportunity)

        return opportunities

    def _check_event(self, event: NegriskEvent) -> Optional[NegriskOpportunity]:
        """Check a single event for arbitrage opportunity."""
        # Get tradeable outcomes (includes OTHER, excludes PLACEHOLDER/RESOLVED)
        tradeable = [o for o in event.outcomes if o.is_tradeable(self.config)]

        if len(tradeable) < self.config.min_outcomes:
            return None

        if len(tradeable) > self.config.max_legs:
            logger.debug(f"Event {event.title} has too many legs: {len(tradeable)}")
            return None

        # Check for stale data
        if event.has_stale_data(self.config.staleness_ttl_ms):
            self.stats.stale_data_rejections += 1
            return None

        # CRITICAL FIX: Calculate sum of asks from tradeable outcomes only
        # This ensures we're pricing the same set of outcomes we're actually trading
        asks = [o.bba.best_ask for o in tradeable]
        if None in asks or len(asks) == 0:
            return None
        sum_of_asks = sum(asks)

        # Calculate fees and costs
        num_legs = len(tradeable)
        taker_fee_pct = self.config.taker_fee_bps / 10000  # Convert bps to decimal
        total_gas = self.config.gas_per_leg * num_legs

        # Fee is applied to each leg's notional
        # For neg-risk arb, we're buying at ask prices, so fee = taker_fee_pct * sum_of_asks
        fee_cost = taker_fee_pct * sum_of_asks

        # Calculate edges
        gross_edge = 1.0 - sum_of_asks
        net_edge = gross_edge - fee_cost - total_gas

        # Check minimum net edge (after fees and gas)
        if net_edge < self.config.min_net_edge:
            return None

        # CRITICAL FIX: Check liquidity from tradeable outcomes only
        ask_sizes = [o.bba.ask_size for o in tradeable if o.bba.ask_size is not None]
        if not ask_sizes:
            self.stats.liquidity_rejections += 1
            return None

        min_liquidity = min(ask_sizes)
        if min_liquidity < self.config.min_liquidity_per_outcome:
            self.stats.liquidity_rejections += 1
            return None

        # Calculate sizing
        # Size is constrained by:
        # 1. Minimum liquidity across all tradeable outcomes (bottleneck)
        # 2. Max position per event
        # 3. Min liquidity per outcome requirement

        max_size_liquidity = min_liquidity
        max_size_risk = self.config.max_position_per_event / sum_of_asks if sum_of_asks > 0 else 0

        max_size = min(max_size_liquidity, max_size_risk)
        suggested_size = max_size * 0.8  # Use 80% of max for safety

        if suggested_size <= 0:
            return None

        # Check cooldown to avoid spam
        cooldown_key = event.event_id
        if cooldown_key in self._opportunity_cooldown:
            if datetime.utcnow() < self._opportunity_cooldown[cooldown_key]:
                return None

        self._opportunity_cooldown[cooldown_key] = datetime.utcnow() + timedelta(seconds=2)

        # Build leg specifications
        legs = []
        for outcome in tradeable:
            leg = {
                "token_id": outcome.token_id,
                "market_id": outcome.market_id,
                "outcome_name": outcome.name,
                "side": "BUY",
                "price": outcome.bba.best_ask,
                "size": suggested_size,
            }
            legs.append(leg)

        # Create opportunity
        opportunity = NegriskOpportunity(
            opportunity_id=f"negrisk_{uuid.uuid4().hex[:12]}",
            event=event,
            sum_of_asks=sum_of_asks,
            gross_edge=gross_edge,
            net_edge=net_edge,
            suggested_size=suggested_size,
            max_size=max_size,
            legs=legs,
            detected_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(seconds=5),  # 5s expiry
        )

        # Track stats
        self.stats.opportunities_detected += 1
        if net_edge > self.stats.best_edge_seen:
            self.stats.best_edge_seen = net_edge
            self.stats.best_edge_event = event.title

        # Cache opportunity
        self._recent_opportunities[opportunity.opportunity_id] = opportunity

        logger.info(
            f"Neg-risk opportunity: {event.title[:40]} | "
            f"sum_asks={sum_of_asks:.4f} | gross={gross_edge:.4f} | "
            f"fees={fee_cost:.4f} | gas={total_gas:.4f} | "
            f"NET edge={net_edge:.4f} | legs={num_legs} | size={suggested_size:.2f}"
        )

        return opportunity

    def validate_opportunity(self, opportunity: NegriskOpportunity) -> bool:
        """
        Validate an opportunity before execution.

        Re-checks all conditions to ensure opportunity is still valid.
        """
        if not opportunity.is_valid(self.config):
            return False

        # Re-check with fresh data
        event = opportunity.event

        # Stale check
        if event.has_stale_data(self.config.staleness_ttl_ms):
            logger.warning(f"Opportunity {opportunity.opportunity_id} rejected: stale data")
            return False

        # CRITICAL FIX: Calculate sum_of_asks from tradeable outcomes only,
        # matching the logic in _check_event. Don't use event.sum_of_asks
        # which is based on active_outcomes - these can differ when an
        # outcome is active but has insufficient liquidity.
        tradeable = [o for o in event.outcomes if o.is_tradeable(self.config)]
        asks = [o.bba.best_ask for o in tradeable]
        if None in asks or len(asks) == 0:
            logger.warning(f"Opportunity {opportunity.opportunity_id} rejected: missing prices")
            return False

        sum_of_asks = sum(asks)
        num_legs = len(tradeable)

        taker_fee_pct = self.config.taker_fee_bps / 10000
        fee_cost = taker_fee_pct * sum_of_asks
        total_gas = self.config.gas_per_leg * num_legs

        net_edge = 1.0 - sum_of_asks - fee_cost - total_gas

        if net_edge < self.config.min_net_edge:
            logger.warning(
                f"Opportunity {opportunity.opportunity_id} rejected: "
                f"edge deteriorated to {net_edge:.4f}"
            )
            return False

        return True

    def get_recent_opportunities(self, max_age_seconds: float = 60.0) -> list[NegriskOpportunity]:
        """Get recently detected opportunities."""
        cutoff = datetime.utcnow() - timedelta(seconds=max_age_seconds)
        return [
            opp for opp in self._recent_opportunities.values()
            if opp.detected_at > cutoff
        ]

    def clear_expired_opportunities(self) -> int:
        """Clear expired opportunities from cache."""
        now = datetime.utcnow()
        expired = [
            opp_id for opp_id, opp in self._recent_opportunities.items()
            if opp.expires_at and opp.expires_at < now
        ]
        for opp_id in expired:
            del self._recent_opportunities[opp_id]
        return len(expired)

    def mark_executed(self, opportunity_id: str) -> None:
        """Mark an opportunity as executed."""
        if opportunity_id in self._recent_opportunities:
            opp = self._recent_opportunities[opportunity_id]
            opp.executed = True
            self.stats.opportunities_executed += 1
            self.stats.total_profit += opp.expected_profit
            self.stats.total_volume += opp.total_cost

    def get_stats(self) -> NegriskStats:
        """Get detector statistics."""
        return self.stats

    def get_stats_dict(self) -> dict:
        """Get statistics as a dictionary."""
        return {
            "opportunities_detected": self.stats.opportunities_detected,
            "opportunities_submitted": self.stats.opportunities_submitted,
            "opportunities_executed": self.stats.opportunities_executed,
            "total_profit": round(self.stats.total_profit, 2),
            "total_volume": round(self.stats.total_volume, 2),
            "stale_data_rejections": self.stats.stale_data_rejections,
            "liquidity_rejections": self.stats.liquidity_rejections,
            "execution_failures": self.stats.execution_failures,
            "best_edge_seen": round(self.stats.best_edge_seen, 4),
            "best_edge_event": self.stats.best_edge_event,
            "recent_opportunities": len(self._recent_opportunities),
        }
