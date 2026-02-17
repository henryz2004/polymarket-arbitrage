"""
Partial Position Detector
==========================

Detects +EV (positive expected value) opportunities by buying a SUBSET
of outcomes in a neg-risk event.

WARNING: This is NOT riskless arbitrage. Positions can lose money if
an excluded outcome wins. Use Kelly criterion for sizing.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from core.negrisk.detector import _compute_fee_per_share
from core.negrisk.models import (
    ArbDirection,
    NegriskConfig,
    NegriskEvent,
    NegriskOpportunity,
    NegriskStats,
    Outcome,
)

logger = logging.getLogger(__name__)


@dataclass
class PartialPositionAnalysis:
    """Analysis of a partial position opportunity."""
    included_outcomes: list[Outcome]
    excluded_outcomes: list[Outcome]
    sum_included_asks: float        # Total cost of buying included outcomes
    sum_excluded_asks: float        # Implied probability of excluded outcomes winning
    prob_win: float                 # Estimated probability included outcome wins
    expected_value: float           # EV per share
    kelly_fraction: float           # Optimal bet fraction
    risk_of_ruin: float            # P(loss) = P(excluded outcome wins)


class PartialPositionDetector:
    """Detects +EV partial position opportunities."""

    def __init__(self, config: NegriskConfig):
        self.config = config
        self.stats = NegriskStats()
        self._recent_opportunities: dict[str, NegriskOpportunity] = {}
        self._opportunity_cooldown: dict[str, datetime] = {}

    def check_event(
        self,
        event: NegriskEvent,
        prob_overrides: Optional[dict[str, float]] = None
    ) -> Optional[NegriskOpportunity]:
        """
        Check if a subset of outcomes offers +EV opportunity.

        Strategy: Greedy approach — sort outcomes by ask price,
        include outcomes starting from cheapest until EV peaks.

        Args:
            event: The neg-risk event to analyze
            prob_overrides: Optional dict[outcome_id, float] of estimated true probabilities.
                If not provided, uses mid-price normalization as default model.

        Returns:
            NegriskOpportunity if +EV opportunity found, None otherwise
        """
        # Check if partial positions are enabled
        if not self.config.enable_partial_positions:
            return None

        # Get all outcomes with ask prices
        priced_outcomes = [
            o for o in event.outcomes
            if o.bba.best_ask is not None
            and o.bba.ask_size is not None
            and o.bba.ask_size >= self.config.min_liquidity_per_outcome
            and o.status.value in ("active", "other")
        ]

        if len(priced_outcomes) < self.config.min_outcomes:
            return None

        # Sort by ask price ascending (cheapest first)
        priced_outcomes.sort(key=lambda o: o.bba.best_ask)

        # Calculate total implied probability from all asks
        total_ask_sum = sum(o.bba.best_ask for o in priced_outcomes)

        # If total_ask_sum < 1.0, this is a full arb — let the main detector handle it
        if total_ask_sum < 1.0:
            return None

        # Determine probability model
        if prob_overrides is not None:
            # Use provided probability overrides
            prob_map = prob_overrides
        else:
            # Default model: mid-price normalization
            # This provides a fairer estimate when spreads are wide
            prob_map = self._compute_mid_price_probabilities(priced_outcomes)

        # Try subsets: include outcomes greedily, find optimal subset
        best_analysis = None
        best_ev = 0.0

        for n_include in range(2, len(priced_outcomes)):
            included = priced_outcomes[:n_include]
            excluded = priced_outcomes[n_include:]

            sum_included = sum(o.bba.best_ask for o in included)
            sum_excluded = sum(o.bba.best_ask for o in excluded)

            # Check max excluded probability constraint
            # Don't exclude outcomes that are very likely to win
            max_excluded_prob = max(o.bba.best_ask for o in excluded)
            if max_excluded_prob > self.config.max_excluded_probability:
                continue

            # Estimate win probability using probability model
            included_ids = [o.outcome_id for o in included]
            prob_win = sum(prob_map.get(oid, 0) for oid in included_ids)

            # EV = P(win) * $1.00 - cost
            # cost = sum_included (what we pay for all included outcome YES shares)
            ev_per_share = prob_win * 1.0 - sum_included

            # Kelly criterion: f* = (p * b - q) / b
            # where p = prob_win, q = 1-p, b = payoff/cost ratio
            # payoff if win = 1.0 - sum_included (profit)
            # loss if lose = sum_included
            if sum_included > 0 and (1.0 - sum_included) > 0:
                b = (1.0 - sum_included) / sum_included  # payoff ratio
                q = 1.0 - prob_win
                kelly = (prob_win * b - q) / b if b > 0 else 0
                kelly = max(0, kelly)  # No negative sizing
            else:
                kelly = 0

            analysis = PartialPositionAnalysis(
                included_outcomes=included,
                excluded_outcomes=excluded,
                sum_included_asks=sum_included,
                sum_excluded_asks=sum_excluded,
                prob_win=prob_win,
                expected_value=ev_per_share,
                kelly_fraction=kelly,
                risk_of_ruin=1.0 - prob_win,
            )

            if ev_per_share > best_ev:
                best_ev = ev_per_share
                best_analysis = analysis

        if best_analysis is None or best_analysis.expected_value < self.config.min_partial_ev:
            return None

        # Apply fees
        included_asks = [o.bba.best_ask for o in best_analysis.included_outcomes]
        fee_per_share = _compute_fee_per_share(
            self.config.fee_rate_bps, included_asks, "BUY"
        )

        net_ev = best_analysis.expected_value - fee_per_share

        if net_ev < self.config.min_partial_ev:
            return None

        # Sizing: use Kelly fraction, capped by config
        min_liq = min(o.bba.ask_size for o in best_analysis.included_outcomes)
        max_size_risk = self.config.max_position_per_event / best_analysis.sum_included_asks

        kelly_size = min_liq * min(best_analysis.kelly_fraction, self.config.partial_kelly_fraction)
        suggested_size = min(kelly_size, max_size_risk, min_liq * 0.8)

        if suggested_size <= 0:
            return None

        # Cooldown
        cooldown_key = f"partial_{event.event_id}"
        if cooldown_key in self._opportunity_cooldown:
            if datetime.utcnow() < self._opportunity_cooldown[cooldown_key]:
                return None
        self._opportunity_cooldown[cooldown_key] = datetime.utcnow() + timedelta(seconds=5)

        # Build legs (only included outcomes)
        legs = []
        for outcome in best_analysis.included_outcomes:
            leg = {
                "token_id": outcome.token_id,
                "market_id": outcome.market_id,
                "outcome_name": outcome.name,
                "side": "BUY",
                "price": outcome.bba.best_ask,
                "size": suggested_size,
            }
            legs.append(leg)

        opportunity = NegriskOpportunity(
            opportunity_id=f"partial_{uuid.uuid4().hex[:12]}",
            event=event,
            direction=ArbDirection.PARTIAL_BUY,
            sum_of_prices=best_analysis.sum_included_asks,
            gross_edge=best_analysis.expected_value,  # EV, not edge in the arb sense
            net_edge=net_ev,
            suggested_size=suggested_size,
            max_size=min(min_liq, max_size_risk),
            legs=legs,
            detected_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(seconds=10),
        )

        self.stats.opportunities_detected += 1
        self._recent_opportunities[opportunity.opportunity_id] = opportunity

        logger.info(
            f"PARTIAL +EV opportunity: {event.title[:40]} | "
            f"included={len(best_analysis.included_outcomes)}/{len(priced_outcomes)} | "
            f"cost={best_analysis.sum_included_asks:.4f} | "
            f"P(win)={best_analysis.prob_win:.3f} | "
            f"EV={net_ev:.4f} | kelly={best_analysis.kelly_fraction:.3f} | "
            f"size={suggested_size:.0f} | "
            f"risk={best_analysis.risk_of_ruin:.3f}"
        )

        return opportunity

    def _compute_mid_price_probabilities(self, outcomes: list[Outcome]) -> dict[str, float]:
        """
        Compute probability estimates using mid-price normalization.

        This provides a fairer estimate than using asks directly, as it removes
        half of the bid-ask spread. The mid-prices are normalized to sum to 1.0.

        Args:
            outcomes: List of outcomes to compute probabilities for

        Returns:
            Dict mapping outcome_id to estimated probability
        """
        prob_map = {}

        # Compute mid-prices
        mid_prices = []
        for o in outcomes:
            if o.bba.best_bid is not None and o.bba.best_ask is not None:
                mid = (o.bba.best_bid + o.bba.best_ask) / 2
            else:
                # If no bid available, use ask as fallback
                mid = o.bba.best_ask
            mid_prices.append((o.outcome_id, mid))

        # Normalize to sum to 1.0
        total = sum(m[1] for m in mid_prices)
        if total > 0:
            for oid, mid in mid_prices:
                prob_map[oid] = mid / total
        else:
            # Fallback to uniform distribution
            uniform_prob = 1.0 / len(outcomes) if outcomes else 0
            for oid, _ in mid_prices:
                prob_map[oid] = uniform_prob

        return prob_map
