"""
Binary Bundle Arbitrage Detector
=================================

Detects YES+NO bundle arbitrage on single binary markets.

BUY_BINARY:  best_ask_yes + best_ask_no < $1.00 → buy both, guaranteed $1.00 payout
SELL_BINARY: best_bid_yes + best_bid_no > $1.00 → sell both, receive > $1.00
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from core.negrisk.detector import _compute_fee_per_share
from core.negrisk.models import (
    ArbDirection,
    NegriskConfig,
    NegriskOpportunity,
    NegriskEvent,
    NegriskStats,
    Outcome,
    OutcomeBBA,
    OutcomeStatus,
)

logger = logging.getLogger(__name__)


@dataclass
class BinaryMarket:
    """A single binary market with YES and NO tokens."""
    market_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    yes_bba: OutcomeBBA = field(default_factory=OutcomeBBA)
    no_bba: OutcomeBBA = field(default_factory=OutcomeBBA)
    volume_24h: float = 0.0
    fee_rate_bps: float = 0  # Per-market fee rate


class BinaryBundleDetector:
    """Detects YES+NO bundle arb on binary markets."""

    def __init__(self, config: NegriskConfig):
        self.config = config
        self.stats = NegriskStats()
        self._recent_opportunities: dict[str, NegriskOpportunity] = {}
        self._opportunity_cooldown: dict[str, datetime] = {}

    def check_market_buy(self, market: BinaryMarket) -> Optional[NegriskOpportunity]:
        """
        Check if best_ask_yes + best_ask_no < 1.0
        If so, buying both YES and NO guarantees $1.00 payout.
        """
        if market.yes_bba.best_ask is None or market.no_bba.best_ask is None:
            return None

        sum_asks = market.yes_bba.best_ask + market.no_bba.best_ask
        gross_edge = 1.0 - sum_asks

        if gross_edge <= 0:
            return None

        # Check liquidity
        yes_liq = market.yes_bba.ask_size or 0
        no_liq = market.no_bba.ask_size or 0
        min_liq = min(yes_liq, no_liq)

        if min_liq < self.config.min_liquidity_per_outcome:
            return None

        # Sizing
        max_size = min(min_liq, self.config.max_position_per_event / sum_asks if sum_asks > 0 else 0)
        suggested_size = max_size * 0.8

        if suggested_size <= 0:
            return None

        # Fees (2 legs)
        prices = [market.yes_bba.best_ask, market.no_bba.best_ask]
        fee_per_share = _compute_fee_per_share(market.fee_rate_bps, prices, "BUY")

        # Gas
        total_gas = self.config.gas_per_leg * 2
        gas_per_share = total_gas / suggested_size if suggested_size > 0 else total_gas

        net_edge = gross_edge - fee_per_share - gas_per_share

        if net_edge < self.config.min_net_edge:
            return None

        # Cooldown
        if market.market_id in self._opportunity_cooldown:
            if datetime.utcnow() < self._opportunity_cooldown[market.market_id]:
                return None
        self._opportunity_cooldown[market.market_id] = datetime.utcnow() + timedelta(seconds=2)

        # Build a synthetic NegriskEvent wrapper for compatibility
        yes_outcome = Outcome(
            outcome_id=f"{market.market_id}_yes",
            market_id=market.market_id,
            condition_id="",
            token_id=market.yes_token_id,
            name="Yes",
            bba=market.yes_bba,
        )
        no_outcome = Outcome(
            outcome_id=f"{market.market_id}_no",
            market_id=market.market_id,
            condition_id="",
            token_id=market.no_token_id,
            name="No",
            bba=market.no_bba,
        )

        event = NegriskEvent(
            event_id=market.market_id,
            slug=market.market_id,
            title=market.question,
            condition_id="",
            outcomes=[yes_outcome, no_outcome],
            neg_risk=False,  # This is a binary market, not neg-risk
        )

        legs = [
            {"token_id": market.yes_token_id, "market_id": market.market_id,
             "outcome_name": "Yes", "side": "BUY", "price": market.yes_bba.best_ask, "size": suggested_size},
            {"token_id": market.no_token_id, "market_id": market.market_id,
             "outcome_name": "No", "side": "BUY", "price": market.no_bba.best_ask, "size": suggested_size},
        ]

        opportunity = NegriskOpportunity(
            opportunity_id=f"binary_buy_{uuid.uuid4().hex[:12]}",
            event=event,
            direction=ArbDirection.BUY_BINARY,
            sum_of_prices=sum_asks,
            gross_edge=gross_edge,
            net_edge=net_edge,
            suggested_size=suggested_size,
            max_size=max_size,
            legs=legs,
            detected_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(seconds=5),
        )

        self.stats.opportunities_detected += 1
        self._recent_opportunities[opportunity.opportunity_id] = opportunity

        logger.info(
            f"BINARY BUY opportunity: {market.question[:40]} | "
            f"sum_asks={sum_asks:.4f} | net_edge={net_edge:.4f} | size={suggested_size:.0f}"
        )

        return opportunity

    def check_market_sell(self, market: BinaryMarket) -> Optional[NegriskOpportunity]:
        """
        Check if best_bid_yes + best_bid_no > 1.0
        If so, selling both YES and NO guarantees profit.
        """
        if market.yes_bba.best_bid is None or market.no_bba.best_bid is None:
            return None

        sum_bids = market.yes_bba.best_bid + market.no_bba.best_bid
        gross_edge = sum_bids - 1.0

        if gross_edge <= 0:
            return None

        # Check liquidity
        yes_liq = market.yes_bba.bid_size or 0
        no_liq = market.no_bba.bid_size or 0
        min_liq = min(yes_liq, no_liq)

        if min_liq < self.config.min_liquidity_per_outcome:
            return None

        # Sizing
        max_size = min(min_liq, self.config.max_position_per_event / sum_bids if sum_bids > 0 else 0)
        suggested_size = max_size * 0.8

        if suggested_size <= 0:
            return None

        # Fees (2 legs)
        prices = [market.yes_bba.best_bid, market.no_bba.best_bid]
        fee_per_share = _compute_fee_per_share(market.fee_rate_bps, prices, "SELL")

        # Gas
        total_gas = self.config.gas_per_leg * 2
        gas_per_share = total_gas / suggested_size if suggested_size > 0 else total_gas

        net_edge = gross_edge - fee_per_share - gas_per_share

        if net_edge < self.config.min_net_edge:
            return None

        # Cooldown
        cooldown_key = f"sell_{market.market_id}"
        if cooldown_key in self._opportunity_cooldown:
            if datetime.utcnow() < self._opportunity_cooldown[cooldown_key]:
                return None
        self._opportunity_cooldown[cooldown_key] = datetime.utcnow() + timedelta(seconds=2)

        # Build a synthetic NegriskEvent wrapper for compatibility
        yes_outcome = Outcome(
            outcome_id=f"{market.market_id}_yes",
            market_id=market.market_id,
            condition_id="",
            token_id=market.yes_token_id,
            name="Yes",
            bba=market.yes_bba,
        )
        no_outcome = Outcome(
            outcome_id=f"{market.market_id}_no",
            market_id=market.market_id,
            condition_id="",
            token_id=market.no_token_id,
            name="No",
            bba=market.no_bba,
        )

        event = NegriskEvent(
            event_id=market.market_id,
            slug=market.market_id,
            title=market.question,
            condition_id="",
            outcomes=[yes_outcome, no_outcome],
            neg_risk=False,  # This is a binary market, not neg-risk
        )

        legs = [
            {"token_id": market.yes_token_id, "market_id": market.market_id,
             "outcome_name": "Yes", "side": "SELL", "price": market.yes_bba.best_bid, "size": suggested_size},
            {"token_id": market.no_token_id, "market_id": market.market_id,
             "outcome_name": "No", "side": "SELL", "price": market.no_bba.best_bid, "size": suggested_size},
        ]

        opportunity = NegriskOpportunity(
            opportunity_id=f"binary_sell_{uuid.uuid4().hex[:12]}",
            event=event,
            direction=ArbDirection.SELL_BINARY,
            sum_of_prices=sum_bids,
            gross_edge=gross_edge,
            net_edge=net_edge,
            suggested_size=suggested_size,
            max_size=max_size,
            legs=legs,
            detected_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(seconds=5),
        )

        self.stats.opportunities_detected += 1
        self._recent_opportunities[opportunity.opportunity_id] = opportunity

        logger.info(
            f"BINARY SELL opportunity: {market.question[:40]} | "
            f"sum_bids={sum_bids:.4f} | net_edge={net_edge:.4f} | size={suggested_size:.0f}"
        )

        return opportunity

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
            "best_edge_seen": round(self.stats.best_edge_seen, 4),
            "best_edge_event": self.stats.best_edge_event,
            "recent_opportunities": len(self._recent_opportunities),
        }
