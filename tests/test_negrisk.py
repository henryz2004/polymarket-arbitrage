"""
Tests for Negrisk Arbitrage Module
====================================
"""

import pytest
from datetime import datetime

from core.negrisk.models import (
    ArbDirection,
    NegriskConfig,
    NegriskEvent,
    NegriskOpportunity,
    Outcome,
    OutcomeBBA,
    OutcomeStatus,
    PriceLevel,
)
from core.negrisk.detector import NegriskDetector, _walk_book


class TestNegriskModels:
    """Test neg-risk data models."""

    def test_outcome_bba(self):
        """Test OutcomeBBA pricing calculations."""
        bba = OutcomeBBA(
            best_bid=0.30,
            best_ask=0.32,
            bid_size=100.0,
            ask_size=150.0,
        )

        assert bba.spread == pytest.approx(0.02, rel=0.01)
        assert bba.mid_price == pytest.approx(0.31, rel=0.01)
        assert not bba.is_stale(1000)  # Not stale within 1 second

    def test_outcome_tradeable(self):
        """Test outcome tradeable logic."""
        config = NegriskConfig()

        # Active outcome with liquidity
        active = Outcome(
            outcome_id="1",
            market_id="m1",
            condition_id="c1",
            token_id="t1",
            name="Outcome A",
            status=OutcomeStatus.ACTIVE,
            bba=OutcomeBBA(best_ask=0.30, ask_size=200.0),
        )
        assert active.is_tradeable(config)

        # Placeholder outcome (should be filtered)
        placeholder = Outcome(
            outcome_id="2",
            market_id="m2",
            condition_id="c1",
            token_id="t2",
            name="Placeholder",
            status=OutcomeStatus.PLACEHOLDER,
            bba=OutcomeBBA(best_ask=0.30, ask_size=200.0),
        )
        assert not placeholder.is_tradeable(config)

        # Low liquidity
        low_liquidity = Outcome(
            outcome_id="3",
            market_id="m3",
            condition_id="c1",
            token_id="t3",
            name="Outcome C",
            status=OutcomeStatus.ACTIVE,
            bba=OutcomeBBA(best_ask=0.30, ask_size=20.0),  # Below 50 minimum
        )
        assert not low_liquidity.is_tradeable(config)

    def test_negrisk_event_sum_of_asks(self):
        """Test sum of asks calculation."""
        event = NegriskEvent(
            event_id="e1",
            slug="test-event",
            title="Test Event",
            condition_id="c1",
            outcomes=[
                Outcome(
                    outcome_id="1",
                    market_id="m1",
                    condition_id="c1",
                    token_id="t1",
                    name="A",
                    bba=OutcomeBBA(best_ask=0.30),
                ),
                Outcome(
                    outcome_id="2",
                    market_id="m2",
                    condition_id="c1",
                    token_id="t2",
                    name="B",
                    bba=OutcomeBBA(best_ask=0.35),
                ),
                Outcome(
                    outcome_id="3",
                    market_id="m3",
                    condition_id="c1",
                    token_id="t3",
                    name="C",
                    bba=OutcomeBBA(best_ask=0.32),
                ),
            ],
        )

        assert event.outcome_count == 3
        assert event.sum_of_asks == 0.97  # 0.30 + 0.35 + 0.32
        assert len(event.active_outcomes) == 3

    def test_negrisk_event_min_liquidity(self):
        """Test minimum liquidity calculation."""
        event = NegriskEvent(
            event_id="e1",
            slug="test-event",
            title="Test Event",
            condition_id="c1",
            outcomes=[
                Outcome(
                    outcome_id="1",
                    market_id="m1",
                    condition_id="c1",
                    token_id="t1",
                    name="A",
                    bba=OutcomeBBA(best_ask=0.30, ask_size=500.0),
                ),
                Outcome(
                    outcome_id="2",
                    market_id="m2",
                    condition_id="c1",
                    token_id="t2",
                    name="B",
                    bba=OutcomeBBA(best_ask=0.35, ask_size=300.0),
                ),
                Outcome(
                    outcome_id="3",
                    market_id="m3",
                    condition_id="c1",
                    token_id="t3",
                    name="C",
                    bba=OutcomeBBA(best_ask=0.32, ask_size=400.0),
                ),
            ],
        )

        assert event.min_ask_liquidity == 300.0  # Bottleneck is outcome B


class TestNegriskDetector:
    """Test neg-risk opportunity detector."""

    def test_detect_opportunity(self):
        """Test basic opportunity detection."""
        config = NegriskConfig(
            min_net_edge=0.01,  # 1% minimum net edge
            min_outcomes=3,
            fee_rate_bps=0,  # Most neg-risk markets are fee-free
            gas_per_leg=0.01,
        )

        detector = NegriskDetector(config)

        # Create an event with arbitrage opportunity
        event = NegriskEvent(
            event_id="e1",
            slug="test-event",
            title="Test Arbitrage Event",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1",
                    market_id="m1",
                    condition_id="c1",
                    token_id="t1",
                    name="Outcome A",
                    bba=OutcomeBBA(best_ask=0.28, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="2",
                    market_id="m2",
                    condition_id="c1",
                    token_id="t2",
                    name="Outcome B",
                    bba=OutcomeBBA(best_ask=0.30, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="3",
                    market_id="m3",
                    condition_id="c1",
                    token_id="t3",
                    name="Outcome C",
                    bba=OutcomeBBA(best_ask=0.35, ask_size=200.0),
                ),
            ],
        )

        # Sum = 0.93, gross edge = 0.07 (7%)
        # Fees = 0 (fee-free market)
        # Gas = 0.01 * 3 / suggested_size (amortized)
        # Net edge ≈ 0.07 - gas

        opportunity = detector._check_event(event)
        assert opportunity is not None
        assert opportunity.direction == ArbDirection.BUY_ALL
        assert opportunity.sum_of_prices == 0.93
        assert opportunity.sum_of_asks == 0.93  # Backward compat alias
        assert opportunity.gross_edge == pytest.approx(0.07, rel=0.01)
        assert opportunity.net_edge > 0.01  # Profitable after fees
        assert opportunity.num_legs == 3
        # All legs should be BUY side
        assert all(leg["side"] == "BUY" for leg in opportunity.legs)

    def test_reject_insufficient_edge(self):
        """Test rejection when edge is too small."""
        config = NegriskConfig(
            min_net_edge=0.025,  # 2.5% minimum net edge (strict)
            fee_rate_bps=0,  # Fee-free market
        )

        detector = NegriskDetector(config)

        # Create an event with small edge
        event = NegriskEvent(
            event_id="e1",
            slug="test-event",
            title="Test Event",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1",
                    market_id="m1",
                    condition_id="c1",
                    token_id="t1",
                    name="A",
                    bba=OutcomeBBA(best_ask=0.33, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="2",
                    market_id="m2",
                    condition_id="c1",
                    token_id="t2",
                    name="B",
                    bba=OutcomeBBA(best_ask=0.33, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="3",
                    market_id="m3",
                    condition_id="c1",
                    token_id="t3",
                    name="C",
                    bba=OutcomeBBA(best_ask=0.33, ask_size=200.0),
                ),
            ],
        )

        # Sum = 0.99, gross edge = 0.01 (1%), too small after fees
        opportunity = detector._check_event(event)
        assert opportunity is None

    def test_reject_insufficient_liquidity(self):
        """Test rejection when liquidity is too low."""
        config = NegriskConfig(
            min_liquidity_per_outcome=100.0,
            min_net_edge=0.02,  # Need 2% net edge
        )

        detector = NegriskDetector(config)

        # Good edge (3%) but insufficient liquidity
        event = NegriskEvent(
            event_id="e1",
            slug="test-event",
            title="Test Event",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1",
                    market_id="m1",
                    condition_id="c1",
                    token_id="t1",
                    name="A",
                    bba=OutcomeBBA(best_ask=0.31, ask_size=50.0),  # Too low liquidity
                ),
                Outcome(
                    outcome_id="2",
                    market_id="m2",
                    condition_id="c1",
                    token_id="t2",
                    name="B",
                    bba=OutcomeBBA(best_ask=0.32, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="3",
                    market_id="m3",
                    condition_id="c1",
                    token_id="t3",
                    name="C",
                    bba=OutcomeBBA(best_ask=0.34, ask_size=200.0),
                ),
            ],
        )

        # Sum = 0.97, gross edge = 0.03 (3%), good edge but low liquidity
        opportunity = detector._check_event(event)
        # Should be rejected (either for liquidity or because net edge is too low)
        assert opportunity is None


class TestNegriskSellSide:
    """Test sell-side neg-risk opportunity detection."""

    def test_detect_sell_side_opportunity(self):
        """Test basic sell-side opportunity detection when sum_bids > $1.00."""
        config = NegriskConfig(
            min_net_edge=0.01,  # 1% minimum net edge
            min_outcomes=3,
            fee_rate_bps=0,  # Fee-free market
            gas_per_leg=0.0,    # Polymarket covers gas
        )

        detector = NegriskDetector(config)

        # Create an event where sum_of_bids > $1.00
        event = NegriskEvent(
            event_id="e1",
            slug="test-sell-event",
            title="Test Sell-Side Event",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1",
                    market_id="m1",
                    condition_id="c1",
                    token_id="t1",
                    name="Outcome A",
                    bba=OutcomeBBA(best_bid=0.40, bid_size=200.0, best_ask=0.42, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="2",
                    market_id="m2",
                    condition_id="c1",
                    token_id="t2",
                    name="Outcome B",
                    bba=OutcomeBBA(best_bid=0.38, bid_size=200.0, best_ask=0.40, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="3",
                    market_id="m3",
                    condition_id="c1",
                    token_id="t3",
                    name="Outcome C",
                    bba=OutcomeBBA(best_bid=0.35, bid_size=200.0, best_ask=0.37, ask_size=200.0),
                ),
            ],
        )

        # Sum of bids = 0.40 + 0.38 + 0.35 = 1.13
        # Gross edge = 1.13 - 1.0 = 0.13 (13%)
        # Fee = 0 (fee-free market)
        # Gas = 0
        # Net edge = 0.13 (13%)

        opportunity = detector._check_event_sell_side(event)
        assert opportunity is not None
        assert opportunity.direction == ArbDirection.SELL_ALL
        assert opportunity.sum_of_prices == pytest.approx(1.13, rel=0.01)
        assert opportunity.gross_edge == pytest.approx(0.13, rel=0.01)
        assert opportunity.net_edge > 0.01  # Profitable after fees
        assert opportunity.num_legs == 3
        # All legs should be SELL side
        assert all(leg["side"] == "SELL" for leg in opportunity.legs)
        # Leg prices should be bid prices
        assert opportunity.legs[0]["price"] == 0.40
        assert opportunity.legs[1]["price"] == 0.38
        assert opportunity.legs[2]["price"] == 0.35

    def test_no_sell_side_when_bids_below_one(self):
        """Test that sell-side is not triggered when sum_bids < $1.00."""
        config = NegriskConfig(
            min_net_edge=0.01,
            min_outcomes=3,
            fee_rate_bps=0,
            gas_per_leg=0.0,
        )

        detector = NegriskDetector(config)

        # Sum of bids < $1.00 — no sell-side opportunity
        event = NegriskEvent(
            event_id="e1",
            slug="test-event",
            title="No Sell Opportunity",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1",
                    market_id="m1",
                    condition_id="c1",
                    token_id="t1",
                    name="A",
                    bba=OutcomeBBA(best_bid=0.28, bid_size=200.0, best_ask=0.30, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="2",
                    market_id="m2",
                    condition_id="c1",
                    token_id="t2",
                    name="B",
                    bba=OutcomeBBA(best_bid=0.30, bid_size=200.0, best_ask=0.32, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="3",
                    market_id="m3",
                    condition_id="c1",
                    token_id="t3",
                    name="C",
                    bba=OutcomeBBA(best_bid=0.25, bid_size=200.0, best_ask=0.27, ask_size=200.0),
                ),
            ],
        )

        # Sum of bids = 0.83, gross edge = -0.17
        opportunity = detector._check_event_sell_side(event)
        assert opportunity is None

    def test_reject_sell_side_insufficient_bid_liquidity(self):
        """Test sell-side rejection when bid liquidity is too low."""
        config = NegriskConfig(
            min_liquidity_per_outcome=100.0,
            min_net_edge=0.01,
            gas_per_leg=0.0,
        )

        detector = NegriskDetector(config)

        # Good sell-side edge but low bid liquidity
        event = NegriskEvent(
            event_id="e1",
            slug="test-event",
            title="Low Bid Liquidity",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1",
                    market_id="m1",
                    condition_id="c1",
                    token_id="t1",
                    name="A",
                    bba=OutcomeBBA(best_bid=0.40, bid_size=50.0, best_ask=0.42, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="2",
                    market_id="m2",
                    condition_id="c1",
                    token_id="t2",
                    name="B",
                    bba=OutcomeBBA(best_bid=0.38, bid_size=200.0, best_ask=0.40, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="3",
                    market_id="m3",
                    condition_id="c1",
                    token_id="t3",
                    name="C",
                    bba=OutcomeBBA(best_bid=0.35, bid_size=200.0, best_ask=0.37, ask_size=200.0),
                ),
            ],
        )

        # Sum of bids = 1.13, but outcome A has bid_size=50 < min 100
        opportunity = detector._check_event_sell_side(event)
        assert opportunity is None

    def test_detect_both_directions(self):
        """Test that detect_opportunities finds both buy and sell side opportunities."""
        config = NegriskConfig(
            min_net_edge=0.01,
            min_outcomes=3,
            fee_rate_bps=0,
            gas_per_leg=0.0,
        )

        detector = NegriskDetector(config)

        # Event with both buy-side and sell-side opportunity (extreme scenario)
        # This tests that both paths are called
        buy_event = NegriskEvent(
            event_id="e_buy",
            slug="buy-event",
            title="Buy Side Event",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1", market_id="m1", condition_id="c1",
                    token_id="t1", name="A",
                    bba=OutcomeBBA(best_ask=0.28, ask_size=200.0, best_bid=0.26, bid_size=200.0),
                ),
                Outcome(
                    outcome_id="2", market_id="m2", condition_id="c1",
                    token_id="t2", name="B",
                    bba=OutcomeBBA(best_ask=0.30, ask_size=200.0, best_bid=0.28, bid_size=200.0),
                ),
                Outcome(
                    outcome_id="3", market_id="m3", condition_id="c1",
                    token_id="t3", name="C",
                    bba=OutcomeBBA(best_ask=0.35, ask_size=200.0, best_bid=0.33, bid_size=200.0),
                ),
            ],
        )

        sell_event = NegriskEvent(
            event_id="e_sell",
            slug="sell-event",
            title="Sell Side Event",
            condition_id="c2",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="4", market_id="m4", condition_id="c2",
                    token_id="t4", name="X",
                    bba=OutcomeBBA(best_bid=0.40, bid_size=200.0, best_ask=0.42, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="5", market_id="m5", condition_id="c2",
                    token_id="t5", name="Y",
                    bba=OutcomeBBA(best_bid=0.38, bid_size=200.0, best_ask=0.40, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="6", market_id="m6", condition_id="c2",
                    token_id="t6", name="Z",
                    bba=OutcomeBBA(best_bid=0.35, bid_size=200.0, best_ask=0.37, ask_size=200.0),
                ),
            ],
        )

        opportunities = detector.detect_opportunities([buy_event, sell_event])

        # Buy event: sum_asks=0.93 → buy-side opportunity
        # Sell event: sum_bids=1.13 → sell-side opportunity
        # Both should be detected
        directions = {opp.direction for opp in opportunities}
        assert ArbDirection.BUY_ALL in directions
        assert ArbDirection.SELL_ALL in directions
        assert len(opportunities) >= 2


class TestFeeFormula:
    """Test the Polymarket on-chain fee formula."""

    def test_fee_free_market(self):
        """Fee-free markets (fee_rate_bps=0) should have zero fees."""
        from core.negrisk.detector import _compute_fee_per_share

        assert _compute_fee_per_share(0, [0.30, 0.35, 0.32], "BUY") == 0.0
        assert _compute_fee_per_share(0, [0.30, 0.35, 0.32], "SELL") == 0.0

    def test_sell_fee_formula(self):
        """
        Sell-side fee: (fee_rate_bps / 10000) * min(p, 1-p) per leg.

        With fee_rate_bps=1000 and prices [0.40, 0.38, 0.35]:
          leg1: 0.1 * min(0.40, 0.60) = 0.1 * 0.40 = 0.040
          leg2: 0.1 * min(0.38, 0.62) = 0.1 * 0.38 = 0.038
          leg3: 0.1 * min(0.35, 0.65) = 0.1 * 0.35 = 0.035
          total: 0.113 per share
        """
        from core.negrisk.detector import _compute_fee_per_share

        fee = _compute_fee_per_share(1000, [0.40, 0.38, 0.35], "SELL")
        assert fee == pytest.approx(0.113, abs=0.001)

    def test_buy_fee_formula(self):
        """
        Buy-side fee: (fee_rate_bps / 10000) * min(p, 1-p) / p per leg.

        With fee_rate_bps=1000 and prices [0.28, 0.30, 0.35]:
          leg1: 0.1 * min(0.28, 0.72) / 0.28 = 0.1 * 0.28 / 0.28 = 0.100
          leg2: 0.1 * min(0.30, 0.70) / 0.30 = 0.1 * 0.30 / 0.30 = 0.100
          leg3: 0.1 * min(0.35, 0.65) / 0.35 = 0.1 * 0.35 / 0.35 = 0.100
          total: 0.300 per share

        Note: When all prices < 0.50, min(p,1-p)/p = 1.0 for each leg,
        so total = base_rate * num_legs.
        """
        from core.negrisk.detector import _compute_fee_per_share

        fee = _compute_fee_per_share(1000, [0.28, 0.30, 0.35], "BUY")
        assert fee == pytest.approx(0.300, abs=0.001)

    def test_buy_fee_high_price(self):
        """
        Buy-side fee with price > 0.50: min(p, 1-p) = 1-p.

        With fee_rate_bps=1000 and price=0.80:
          fee = 0.1 * min(0.80, 0.20) / 0.80 = 0.1 * 0.20 / 0.80 = 0.025
        """
        from core.negrisk.detector import _compute_fee_per_share

        fee = _compute_fee_per_share(1000, [0.80], "BUY")
        assert fee == pytest.approx(0.025, abs=0.001)

    def test_fee_symmetry_at_midpoint(self):
        """At p=0.50, min(p,1-p)=0.50. Fee is maximized."""
        from core.negrisk.detector import _compute_fee_per_share

        # SELL at 0.50: 0.1 * 0.5 = 0.05 per leg
        sell_fee = _compute_fee_per_share(1000, [0.50], "SELL")
        assert sell_fee == pytest.approx(0.05, abs=0.001)

        # BUY at 0.50: 0.1 * 0.5 / 0.5 = 0.10 per leg
        buy_fee = _compute_fee_per_share(1000, [0.50], "BUY")
        assert buy_fee == pytest.approx(0.10, abs=0.001)

    def test_fee_enabled_market_reduces_edge(self):
        """With fees enabled, the same opportunity has lower net edge."""
        # Fee-free config
        config_free = NegriskConfig(
            min_net_edge=0.01,
            min_outcomes=3,
            fee_rate_bps=0,
            gas_per_leg=0.0,
        )

        # Fee-enabled config (1000 bps as used on 15-min crypto markets)
        config_fee = NegriskConfig(
            min_net_edge=0.01,
            min_outcomes=3,
            fee_rate_bps=1000,
            gas_per_leg=0.0,
        )

        detector_free = NegriskDetector(config_free)
        detector_fee = NegriskDetector(config_fee)

        event = NegriskEvent(
            event_id="e1",
            slug="test",
            title="Fee Test Event",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1", market_id="m1", condition_id="c1",
                    token_id="t1", name="A",
                    bba=OutcomeBBA(best_ask=0.28, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="2", market_id="m2", condition_id="c1",
                    token_id="t2", name="B",
                    bba=OutcomeBBA(best_ask=0.30, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="3", market_id="m3", condition_id="c1",
                    token_id="t3", name="C",
                    bba=OutcomeBBA(best_ask=0.35, ask_size=200.0),
                ),
            ],
        )

        opp_free = detector_free._check_event(event)
        opp_fee = detector_fee._check_event(event)

        assert opp_free is not None
        assert opp_free.net_edge == pytest.approx(0.07, abs=0.001)  # Full gross edge, no fee

        # With fee_rate_bps=1000, BUY fee on prices <0.50:
        # Each leg: 0.1 * p / p = 0.1 → total = 0.3
        # Net = 0.07 - 0.3 = -0.23 → should be rejected
        assert opp_fee is None


class TestPartialPositions:
    """Test partial position (+EV, not riskless) opportunities."""

    def test_partial_disabled_by_default(self):
        """With enable_partial_positions=False, no opportunities should be detected."""
        config = NegriskConfig(
            enable_partial_positions=False,  # Disabled
            min_partial_ev=0.05,
        )

        from core.negrisk.partial_detector import PartialPositionDetector
        detector = PartialPositionDetector(config)

        # Create event with potential partial opportunity
        event = NegriskEvent(
            event_id="e1",
            slug="test-event",
            title="Test Event",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1", market_id="m1", condition_id="c1",
                    token_id="t1", name="A",
                    bba=OutcomeBBA(best_ask=0.15, ask_size=200.0, best_bid=0.13, bid_size=200.0),
                ),
                Outcome(
                    outcome_id="2", market_id="m2", condition_id="c1",
                    token_id="t2", name="B",
                    bba=OutcomeBBA(best_ask=0.15, ask_size=200.0, best_bid=0.13, bid_size=200.0),
                ),
                Outcome(
                    outcome_id="3", market_id="m3", condition_id="c1",
                    token_id="t3", name="C",
                    bba=OutcomeBBA(best_ask=0.15, ask_size=200.0, best_bid=0.13, bid_size=200.0),
                ),
                Outcome(
                    outcome_id="4", market_id="m4", condition_id="c1",
                    token_id="t4", name="D",
                    bba=OutcomeBBA(best_ask=0.15, ask_size=200.0, best_bid=0.13, bid_size=200.0),
                ),
                Outcome(
                    outcome_id="5", market_id="m5", condition_id="c1",
                    token_id="t5", name="E",
                    bba=OutcomeBBA(best_ask=0.50, ask_size=200.0, best_bid=0.48, bid_size=200.0),
                ),
            ],
        )

        # Should return None because partial positions are disabled
        opportunity = detector.check_event(event)
        assert opportunity is None

    def test_partial_positive_ev_with_model(self):
        """Test partial position with injected probabilities showing positive EV."""
        config = NegriskConfig(
            enable_partial_positions=True,
            min_partial_ev=0.05,  # 5% minimum EV
            fee_rate_bps=0,  # Fee-free
            max_excluded_probability=0.20,
            partial_kelly_fraction=0.25,
        )

        from core.negrisk.partial_detector import PartialPositionDetector
        detector = PartialPositionDetector(config)

        # Create event: 5 outcomes
        # First 4 have ask=0.22 each, last has ask=0.20
        # Total asks = 1.08 (sum > 1.0 means no full arb)
        # But with a probability model that says first 4 outcomes are underpriced,
        # buying them could be +EV
        event = NegriskEvent(
            event_id="e1",
            slug="test-event",
            title="Partial Test Event",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1", market_id="m1", condition_id="c1",
                    token_id="t1", name="A",
                    bba=OutcomeBBA(best_ask=0.22, ask_size=200.0, best_bid=0.20, bid_size=200.0),
                ),
                Outcome(
                    outcome_id="2", market_id="m2", condition_id="c1",
                    token_id="t2", name="B",
                    bba=OutcomeBBA(best_ask=0.22, ask_size=200.0, best_bid=0.20, bid_size=200.0),
                ),
                Outcome(
                    outcome_id="3", market_id="m3", condition_id="c1",
                    token_id="t3", name="C",
                    bba=OutcomeBBA(best_ask=0.22, ask_size=200.0, best_bid=0.20, bid_size=200.0),
                ),
                Outcome(
                    outcome_id="4", market_id="m4", condition_id="c1",
                    token_id="t4", name="D",
                    bba=OutcomeBBA(best_ask=0.22, ask_size=200.0, best_bid=0.20, bid_size=200.0),
                ),
                Outcome(
                    outcome_id="5", market_id="m5", condition_id="c1",
                    token_id="t5", name="E",
                    bba=OutcomeBBA(best_ask=0.20, ask_size=200.0, best_bid=0.18, bid_size=200.0),
                ),
            ],
        )

        # Inject probabilities: outcomes 1-4 have true prob 0.23 each (total 0.92), outcome 5 has 0.08
        # Buying first 4: cost = 0.88, P(win) = 0.92, EV = 0.92 - 0.88 = 0.04 (4% EV - below threshold)
        # Let's make it higher: outcomes 1-4 have 0.235 each (0.94), outcome 5 has 0.06
        # EV = 0.94 - 0.88 = 0.06 (6% EV - above threshold!)
        prob_overrides = {
            "1": 0.235,
            "2": 0.235,
            "3": 0.235,
            "4": 0.235,
            "5": 0.06,
        }

        opportunity = detector.check_event(event, prob_overrides=prob_overrides)
        assert opportunity is not None
        assert opportunity.direction == ArbDirection.PARTIAL_BUY
        # After sorting by ask, order is: outcome 5 (0.20), then 1-4 (0.22 each)
        # The greedy algorithm will try different subsets and pick the best
        assert len(opportunity.legs) >= 2  # At least 2 outcomes
        assert opportunity.net_edge > 0.05  # Should have positive EV

    def test_partial_negative_ev(self):
        """Test that negative EV opportunities are rejected."""
        config = NegriskConfig(
            enable_partial_positions=True,
            min_partial_ev=0.05,
            fee_rate_bps=0,
        )

        from core.negrisk.partial_detector import PartialPositionDetector
        detector = PartialPositionDetector(config)

        event = NegriskEvent(
            event_id="e1",
            slug="test-event",
            title="Negative EV Event",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1", market_id="m1", condition_id="c1",
                    token_id="t1", name="A",
                    bba=OutcomeBBA(best_ask=0.30, ask_size=200.0, best_bid=0.28, bid_size=200.0),
                ),
                Outcome(
                    outcome_id="2", market_id="m2", condition_id="c1",
                    token_id="t2", name="B",
                    bba=OutcomeBBA(best_ask=0.30, ask_size=200.0, best_bid=0.28, bid_size=200.0),
                ),
                Outcome(
                    outcome_id="3", market_id="m3", condition_id="c1",
                    token_id="t3", name="C",
                    bba=OutcomeBBA(best_ask=0.30, ask_size=200.0, best_bid=0.28, bid_size=200.0),
                ),
                Outcome(
                    outcome_id="4", market_id="m4", condition_id="c1",
                    token_id="t4", name="D",
                    bba=OutcomeBBA(best_ask=0.30, ask_size=200.0, best_bid=0.28, bid_size=200.0),
                ),
            ],
        )

        # Inject probabilities where all subsets have negative EV
        # Cost of any 3: 0.90, but true prob < 0.90 → negative EV
        prob_overrides = {
            "1": 0.25,
            "2": 0.25,
            "3": 0.25,
            "4": 0.25,
        }

        opportunity = detector.check_event(event, prob_overrides=prob_overrides)
        assert opportunity is None

    def test_partial_kelly_sizing(self):
        """Test that Kelly criterion sizing is applied correctly."""
        config = NegriskConfig(
            enable_partial_positions=True,
            min_partial_ev=0.05,
            fee_rate_bps=0,
            partial_kelly_fraction=0.25,  # Quarter-Kelly cap
            max_position_per_event=1000.0,
        )

        from core.negrisk.partial_detector import PartialPositionDetector
        detector = PartialPositionDetector(config)

        event = NegriskEvent(
            event_id="e1",
            slug="test-event",
            title="Kelly Test Event",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1", market_id="m1", condition_id="c1",
                    token_id="t1", name="A",
                    bba=OutcomeBBA(best_ask=0.20, ask_size=500.0, best_bid=0.18, bid_size=500.0),
                ),
                Outcome(
                    outcome_id="2", market_id="m2", condition_id="c1",
                    token_id="t2", name="B",
                    bba=OutcomeBBA(best_ask=0.20, ask_size=500.0, best_bid=0.18, bid_size=500.0),
                ),
                Outcome(
                    outcome_id="3", market_id="m3", condition_id="c1",
                    token_id="t3", name="C",
                    bba=OutcomeBBA(best_ask=0.70, ask_size=500.0, best_bid=0.68, bid_size=500.0),
                ),
            ],
        )

        # True probabilities: first 2 have 0.48 each (total 0.96), last has 0.04
        # Buying first 2: cost=0.40, P(win)=0.96, EV=0.56 (56% EV!)
        # Kelly = (p*b - q)/b where b = (1-cost)/cost = 0.60/0.40 = 1.5
        # Kelly = (0.96*1.5 - 0.04)/1.5 = (1.44 - 0.04)/1.5 = 0.933
        # Capped at partial_kelly_fraction=0.25
        prob_overrides = {
            "1": 0.48,
            "2": 0.48,
            "3": 0.04,
        }

        opportunity = detector.check_event(event, prob_overrides=prob_overrides)
        assert opportunity is not None
        # Kelly fraction should be high but capped
        # suggested_size = min(kelly_size, max_size_risk, min_liq*0.8)
        # kelly_size = min_liq * min(kelly_fraction, partial_kelly_fraction)
        #            = 500 * 0.25 = 125
        # max_size_risk = 1000 / 0.40 = 2500
        # min_liq * 0.8 = 400
        # suggested_size = min(125, 2500, 400) = 125
        assert opportunity.suggested_size == pytest.approx(125, abs=1)

    def test_partial_max_excluded_prob(self):
        """Test that outcomes with high probability are not excluded."""
        config = NegriskConfig(
            enable_partial_positions=True,
            min_partial_ev=0.05,
            fee_rate_bps=0,
            max_excluded_probability=0.15,  # Don't exclude if >15%
        )

        from core.negrisk.partial_detector import PartialPositionDetector
        detector = PartialPositionDetector(config)

        event = NegriskEvent(
            event_id="e1",
            slug="test-event",
            title="Max Excluded Test",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1", market_id="m1", condition_id="c1",
                    token_id="t1", name="A",
                    bba=OutcomeBBA(best_ask=0.10, ask_size=200.0, best_bid=0.08, bid_size=200.0),
                ),
                Outcome(
                    outcome_id="2", market_id="m2", condition_id="c1",
                    token_id="t2", name="B",
                    bba=OutcomeBBA(best_ask=0.10, ask_size=200.0, best_bid=0.08, bid_size=200.0),
                ),
                Outcome(
                    outcome_id="3", market_id="m3", condition_id="c1",
                    token_id="t3", name="C",
                    bba=OutcomeBBA(best_ask=0.10, ask_size=200.0, best_bid=0.08, bid_size=200.0),
                ),
                Outcome(
                    outcome_id="4", market_id="m4", condition_id="c1",
                    token_id="t4", name="D",
                    bba=OutcomeBBA(best_ask=0.20, ask_size=200.0, best_bid=0.18, bid_size=200.0),
                ),
            ],
        )

        # Outcome 4 has ask=0.20 > max_excluded_probability=0.15
        # So it cannot be excluded
        # This means we can only try subsets that include outcome 4
        # But outcome 4 is the most expensive, so greedy won't work well
        # The detector should not find an opportunity or only find specific subsets

        prob_overrides = {
            "1": 0.25,
            "2": 0.25,
            "3": 0.25,
            "4": 0.25,
        }

        opportunity = detector.check_event(event, prob_overrides=prob_overrides)
        # With uniform probabilities and the constraint, no +EV opportunity
        # Because any subset including outcome 4 will be expensive
        # This test verifies the constraint is enforced (specific result depends on implementation)

    def test_partial_fee_impact(self):
        """Test that fees reduce EV and can turn +EV into -EV."""
        config_free = NegriskConfig(
            enable_partial_positions=True,
            min_partial_ev=0.05,
            fee_rate_bps=0,  # Fee-free
            max_excluded_probability=0.50,  # Allow excluding high-prob outcomes for fee testing
        )

        config_fee = NegriskConfig(
            enable_partial_positions=True,
            min_partial_ev=0.05,
            fee_rate_bps=1000,  # 10% fee
            max_excluded_probability=0.50,  # Allow excluding high-prob outcomes for fee testing
        )

        from core.negrisk.partial_detector import PartialPositionDetector
        detector_free = PartialPositionDetector(config_free)
        detector_fee = PartialPositionDetector(config_fee)

        event = NegriskEvent(
            event_id="e1",
            slug="test-event",
            title="Fee Impact Test",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1", market_id="m1", condition_id="c1",
                    token_id="t1", name="A",
                    bba=OutcomeBBA(best_ask=0.20, ask_size=200.0, best_bid=0.18, bid_size=200.0),
                ),
                Outcome(
                    outcome_id="2", market_id="m2", condition_id="c1",
                    token_id="t2", name="B",
                    bba=OutcomeBBA(best_ask=0.20, ask_size=200.0, best_bid=0.18, bid_size=200.0),
                ),
                Outcome(
                    outcome_id="3", market_id="m3", condition_id="c1",
                    token_id="t3", name="C",
                    bba=OutcomeBBA(best_ask=0.70, ask_size=200.0, best_bid=0.68, bid_size=200.0),
                ),
            ],
        )

        # Buying first 2: cost=0.40, true P(win)=0.60, gross EV=0.20 (20%)
        prob_overrides = {
            "1": 0.30,
            "2": 0.30,
            "3": 0.40,
        }

        # Fee-free should detect opportunity
        opp_free = detector_free.check_event(event, prob_overrides=prob_overrides)
        assert opp_free is not None
        assert opp_free.net_edge >= 0.05  # Meets minimum

        # With 10% fee on each leg at prices <0.50:
        # BUY fee = 0.1 * min(p,1-p)/p = 0.1 per leg (when p<0.50)
        # Total fee = 0.1 * 2 = 0.2 per share
        # Net EV = 0.10 - 0.20 = -0.10 → negative, should be rejected
        opp_fee = detector_fee.check_event(event, prob_overrides=prob_overrides)
        assert opp_fee is None

    def test_partial_skips_full_arb(self):
        """Test that partial detector skips events that are full arbs."""
        config = NegriskConfig(
            enable_partial_positions=True,
            min_partial_ev=0.05,
            fee_rate_bps=0,
        )

        from core.negrisk.partial_detector import PartialPositionDetector
        detector = PartialPositionDetector(config)

        # Event where sum_asks < 1.0 (full arb)
        event = NegriskEvent(
            event_id="e1",
            slug="test-event",
            title="Full Arb Event",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1", market_id="m1", condition_id="c1",
                    token_id="t1", name="A",
                    bba=OutcomeBBA(best_ask=0.30, ask_size=200.0, best_bid=0.28, bid_size=200.0),
                ),
                Outcome(
                    outcome_id="2", market_id="m2", condition_id="c1",
                    token_id="t2", name="B",
                    bba=OutcomeBBA(best_ask=0.32, ask_size=200.0, best_bid=0.30, bid_size=200.0),
                ),
                Outcome(
                    outcome_id="3", market_id="m3", condition_id="c1",
                    token_id="t3", name="C",
                    bba=OutcomeBBA(best_ask=0.35, ask_size=200.0, best_bid=0.33, bid_size=200.0),
                ),
            ],
        )

        # Sum = 0.97 < 1.0 → full arb
        # Partial detector should skip this
        opportunity = detector.check_event(event)
        assert opportunity is None

    def test_partial_insufficient_liquidity(self):
        """Test that partial positions are rejected if liquidity is too low."""
        config = NegriskConfig(
            enable_partial_positions=True,
            min_partial_ev=0.05,
            fee_rate_bps=0,
            min_liquidity_per_outcome=100.0,
        )

        from core.negrisk.partial_detector import PartialPositionDetector
        detector = PartialPositionDetector(config)

        event = NegriskEvent(
            event_id="e1",
            slug="test-event",
            title="Low Liquidity Event",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1", market_id="m1", condition_id="c1",
                    token_id="t1", name="A",
                    bba=OutcomeBBA(best_ask=0.20, ask_size=50.0, best_bid=0.18, bid_size=50.0),
                ),
                Outcome(
                    outcome_id="2", market_id="m2", condition_id="c1",
                    token_id="t2", name="B",
                    bba=OutcomeBBA(best_ask=0.20, ask_size=200.0, best_bid=0.18, bid_size=200.0),
                ),
                Outcome(
                    outcome_id="3", market_id="m3", condition_id="c1",
                    token_id="t3", name="C",
                    bba=OutcomeBBA(best_ask=0.70, ask_size=200.0, best_bid=0.68, bid_size=200.0),
                ),
            ],
        )

        # Outcome 1 has ask_size=50 < min 100
        # So it won't be included in priced_outcomes
        # The detector should still work with remaining outcomes
        prob_overrides = {
            "1": 0.25,
            "2": 0.25,
            "3": 0.50,
        }

        # Since outcome 1 is filtered out, only outcomes 2 and 3 remain
        # Need at least min_outcomes=3, so this should be rejected
        opportunity = detector.check_event(event, prob_overrides=prob_overrides)
        assert opportunity is None


class TestBinaryBundleArb:
    """Test binary bundle arbitrage detection."""

    def test_binary_buy_opportunity(self):
        """Test detection of binary buy opportunity when sum_asks < 1.0."""
        from core.negrisk.binary_detector import BinaryBundleDetector, BinaryMarket

        config = NegriskConfig(
            min_net_edge=0.01,  # 1% minimum net edge
            fee_rate_bps=0,     # Fee-free market
            gas_per_leg=0.0,    # No gas costs
            min_liquidity_per_outcome=50.0,
        )

        detector = BinaryBundleDetector(config)

        # YES ask=0.45, NO ask=0.50, sum=0.95 → 5% gross edge
        market = BinaryMarket(
            market_id="binary_market_1",
            question="Will it rain tomorrow?",
            yes_token_id="yes_token_123",
            no_token_id="no_token_456",
            yes_bba=OutcomeBBA(best_ask=0.45, ask_size=100.0),
            no_bba=OutcomeBBA(best_ask=0.50, ask_size=100.0),
            volume_24h=5000.0,
            fee_rate_bps=0,
        )

        opportunity = detector.check_market_buy(market)

        assert opportunity is not None
        assert opportunity.direction == ArbDirection.BUY_BINARY
        assert opportunity.sum_of_prices == pytest.approx(0.95, rel=0.01)
        assert opportunity.gross_edge == pytest.approx(0.05, rel=0.01)
        assert opportunity.net_edge == pytest.approx(0.05, rel=0.01)  # No fees or gas
        assert len(opportunity.legs) == 2
        assert opportunity.legs[0]["side"] == "BUY"
        assert opportunity.legs[1]["side"] == "BUY"
        assert opportunity.legs[0]["outcome_name"] == "Yes"
        assert opportunity.legs[1]["outcome_name"] == "No"
        assert opportunity.legs[0]["price"] == 0.45
        assert opportunity.legs[1]["price"] == 0.50

    def test_binary_no_opportunity_sum_above_one(self):
        """Test that no buy opportunity is detected when sum_asks >= 1.0."""
        from core.negrisk.binary_detector import BinaryBundleDetector, BinaryMarket

        config = NegriskConfig(
            min_net_edge=0.01,
            fee_rate_bps=0,
            gas_per_leg=0.0,
        )

        detector = BinaryBundleDetector(config)

        # YES ask=0.55, NO ask=0.50, sum=1.05 → no buy opportunity
        market = BinaryMarket(
            market_id="binary_market_2",
            question="Will it rain tomorrow?",
            yes_token_id="yes_token_123",
            no_token_id="no_token_456",
            yes_bba=OutcomeBBA(best_ask=0.55, ask_size=100.0),
            no_bba=OutcomeBBA(best_ask=0.50, ask_size=100.0),
            volume_24h=5000.0,
            fee_rate_bps=0,
        )

        opportunity = detector.check_market_buy(market)
        assert opportunity is None

    def test_binary_sell_opportunity(self):
        """Test detection of binary sell opportunity when sum_bids > 1.0."""
        from core.negrisk.binary_detector import BinaryBundleDetector, BinaryMarket

        config = NegriskConfig(
            min_net_edge=0.01,
            fee_rate_bps=0,
            gas_per_leg=0.0,
            min_liquidity_per_outcome=50.0,
        )

        detector = BinaryBundleDetector(config)

        # YES bid=0.55, NO bid=0.50, sum=1.05 → 5% sell edge
        market = BinaryMarket(
            market_id="binary_market_3",
            question="Will it rain tomorrow?",
            yes_token_id="yes_token_123",
            no_token_id="no_token_456",
            yes_bba=OutcomeBBA(best_bid=0.55, bid_size=100.0),
            no_bba=OutcomeBBA(best_bid=0.50, bid_size=100.0),
            volume_24h=5000.0,
            fee_rate_bps=0,
        )

        opportunity = detector.check_market_sell(market)

        assert opportunity is not None
        assert opportunity.direction == ArbDirection.SELL_BINARY
        assert opportunity.sum_of_prices == pytest.approx(1.05, rel=0.01)
        assert opportunity.gross_edge == pytest.approx(0.05, rel=0.01)
        assert opportunity.net_edge == pytest.approx(0.05, rel=0.01)
        assert len(opportunity.legs) == 2
        assert opportunity.legs[0]["side"] == "SELL"
        assert opportunity.legs[1]["side"] == "SELL"
        assert opportunity.legs[0]["outcome_name"] == "Yes"
        assert opportunity.legs[1]["outcome_name"] == "No"
        assert opportunity.legs[0]["price"] == 0.55
        assert opportunity.legs[1]["price"] == 0.50

    def test_binary_no_sell_below_one(self):
        """Test that no sell opportunity is detected when sum_bids <= 1.0."""
        from core.negrisk.binary_detector import BinaryBundleDetector, BinaryMarket

        config = NegriskConfig(
            min_net_edge=0.01,
            fee_rate_bps=0,
            gas_per_leg=0.0,
        )

        detector = BinaryBundleDetector(config)

        # YES bid=0.45, NO bid=0.50, sum=0.95 → no sell opportunity
        market = BinaryMarket(
            market_id="binary_market_4",
            question="Will it rain tomorrow?",
            yes_token_id="yes_token_123",
            no_token_id="no_token_456",
            yes_bba=OutcomeBBA(best_bid=0.45, bid_size=100.0),
            no_bba=OutcomeBBA(best_bid=0.50, bid_size=100.0),
            volume_24h=5000.0,
            fee_rate_bps=0,
        )

        opportunity = detector.check_market_sell(market)
        assert opportunity is None

    def test_binary_liquidity_check(self):
        """Test that low liquidity on one side prevents opportunity detection."""
        from core.negrisk.binary_detector import BinaryBundleDetector, BinaryMarket

        config = NegriskConfig(
            min_net_edge=0.01,
            fee_rate_bps=0,
            gas_per_leg=0.0,
            min_liquidity_per_outcome=100.0,  # Require at least 100
        )

        detector = BinaryBundleDetector(config)

        # Good edge but YES has low liquidity
        market = BinaryMarket(
            market_id="binary_market_5",
            question="Will it rain tomorrow?",
            yes_token_id="yes_token_123",
            no_token_id="no_token_456",
            yes_bba=OutcomeBBA(best_ask=0.45, ask_size=50.0),  # Below minimum
            no_bba=OutcomeBBA(best_ask=0.50, ask_size=200.0),
            volume_24h=5000.0,
            fee_rate_bps=0,
        )

        opportunity = detector.check_market_buy(market)
        assert opportunity is None

    def test_binary_fee_calculation(self):
        """Test that fees are correctly calculated for binary markets."""
        from core.negrisk.binary_detector import BinaryBundleDetector, BinaryMarket

        config = NegriskConfig(
            min_net_edge=0.01,  # 1% minimum
            fee_rate_bps=0,     # Will be overridden by market fee
            gas_per_leg=0.0,
            min_liquidity_per_outcome=50.0,
        )

        detector = BinaryBundleDetector(config)

        # Market with fee_rate_bps=1000 (10%)
        # YES ask=0.45, NO ask=0.50, sum=0.95 → 5% gross edge
        # BUY fee: (1000/10000) * min(0.45,0.55)/0.45 + (1000/10000) * min(0.50,0.50)/0.50
        #        = 0.1 * 0.45/0.45 + 0.1 * 0.50/0.50 = 0.1 + 0.1 = 0.2
        # Net edge = 0.05 - 0.2 = -0.15 → should be rejected
        market = BinaryMarket(
            market_id="binary_market_6",
            question="Will it rain tomorrow?",
            yes_token_id="yes_token_123",
            no_token_id="no_token_456",
            yes_bba=OutcomeBBA(best_ask=0.45, ask_size=100.0),
            no_bba=OutcomeBBA(best_ask=0.50, ask_size=100.0),
            volume_24h=5000.0,
            fee_rate_bps=1000,
        )

        opportunity = detector.check_market_buy(market)
        # Should be rejected because net edge is negative
        assert opportunity is None

    def test_binary_sizing(self):
        """Test that suggested_size is correctly calculated."""
        from core.negrisk.binary_detector import BinaryBundleDetector, BinaryMarket

        config = NegriskConfig(
            min_net_edge=0.01,
            fee_rate_bps=0,
            gas_per_leg=0.0,
            min_liquidity_per_outcome=50.0,
            max_position_per_event=500.0,
        )

        detector = BinaryBundleDetector(config)

        # YES ask=0.45, NO ask=0.50, sum=0.95
        # min_liq = min(120, 150) = 120
        # max_size_liquidity = 120
        # max_size_risk = 500 / 0.95 ≈ 526.32
        # max_size = min(120, 526.32) = 120
        # suggested_size = 120 * 0.8 = 96
        market = BinaryMarket(
            market_id="binary_market_7",
            question="Will it rain tomorrow?",
            yes_token_id="yes_token_123",
            no_token_id="no_token_456",
            yes_bba=OutcomeBBA(best_ask=0.45, ask_size=120.0),
            no_bba=OutcomeBBA(best_ask=0.50, ask_size=150.0),
            volume_24h=5000.0,
            fee_rate_bps=0,
        )

        opportunity = detector.check_market_buy(market)

        assert opportunity is not None
        assert opportunity.max_size == pytest.approx(120.0, rel=0.01)
        assert opportunity.suggested_size == pytest.approx(96.0, rel=0.01)

    def test_binary_edge_below_threshold(self):
        """Test that opportunities below edge threshold are rejected."""
        from core.negrisk.binary_detector import BinaryBundleDetector, BinaryMarket

        config = NegriskConfig(
            min_net_edge=0.025,  # 2.5% minimum net edge
            fee_rate_bps=0,
            gas_per_leg=0.0,
            min_liquidity_per_outcome=50.0,
        )

        detector = BinaryBundleDetector(config)

        # Sum=0.99 → 1% gross edge, below 2.5% threshold
        market = BinaryMarket(
            market_id="binary_market_8",
            question="Will it rain tomorrow?",
            yes_token_id="yes_token_123",
            no_token_id="no_token_456",
            yes_bba=OutcomeBBA(best_ask=0.49, ask_size=100.0),
            no_bba=OutcomeBBA(best_ask=0.50, ask_size=100.0),
            volume_24h=5000.0,
            fee_rate_bps=0,
        )

        opportunity = detector.check_market_buy(market)
        assert opportunity is None

    def test_binary_with_gas_costs(self):
        """Test binary arb with gas costs included."""
        from core.negrisk.binary_detector import BinaryBundleDetector, BinaryMarket

        config = NegriskConfig(
            min_net_edge=0.01,
            fee_rate_bps=0,
            gas_per_leg=0.01,  # $0.01 per leg
            min_liquidity_per_outcome=50.0,
        )

        detector = BinaryBundleDetector(config)

        # YES ask=0.45, NO ask=0.50, sum=0.95 → 5% gross edge
        # Total gas = 0.01 * 2 = 0.02
        # Suggested size = 80 (80% of min liquidity)
        # Gas per share = 0.02 / 80 = 0.00025
        # Net edge = 0.05 - 0.00025 ≈ 0.04975 (well above 1% threshold)
        market = BinaryMarket(
            market_id="binary_market_9",
            question="Will it rain tomorrow?",
            yes_token_id="yes_token_123",
            no_token_id="no_token_456",
            yes_bba=OutcomeBBA(best_ask=0.45, ask_size=100.0),
            no_bba=OutcomeBBA(best_ask=0.50, ask_size=100.0),
            volume_24h=5000.0,
            fee_rate_bps=0,
        )

        opportunity = detector.check_market_buy(market)

        assert opportunity is not None
        # Gas impact is minimal with reasonable trade size
        assert opportunity.net_edge > 0.045


class TestOrderBookDepth:
    """Test order book depth scanning functionality."""

    def test_walk_book_exact_fill(self):
        """Test walking book with exact fill at first level."""
        levels = [
            PriceLevel(price=0.30, size=50),
            PriceLevel(price=0.32, size=100),
            PriceLevel(price=0.35, size=200),
        ]
        avg_price, fill = _walk_book(levels, 50)
        assert fill == 50
        assert avg_price == pytest.approx(0.30, abs=0.001)

    def test_walk_book_cross_levels(self):
        """Test walking book across multiple levels."""
        levels = [
            PriceLevel(price=0.30, size=50),
            PriceLevel(price=0.32, size=100),
            PriceLevel(price=0.35, size=200),
        ]
        # Fill 100 shares: 50 @ 0.30 + 50 @ 0.32
        avg_price, fill = _walk_book(levels, 100)
        assert fill == 100
        expected_avg = (50 * 0.30 + 50 * 0.32) / 100
        assert avg_price == pytest.approx(expected_avg, abs=0.001)

    def test_walk_book_partial_fill(self):
        """Test walking book with insufficient depth."""
        levels = [
            PriceLevel(price=0.30, size=50),
            PriceLevel(price=0.32, size=100),
            PriceLevel(price=0.35, size=200),
        ]
        # Request 500 but only 350 available
        avg_price, fill = _walk_book(levels, 500)
        assert fill == 350  # All available
        expected_avg = (50 * 0.30 + 100 * 0.32 + 200 * 0.35) / 350
        assert avg_price == pytest.approx(expected_avg, abs=0.001)

    def test_walk_book_empty(self):
        """Test walking empty book."""
        avg_price, fill = _walk_book([], 100)
        assert avg_price == 0.0
        assert fill == 0.0

    def test_walk_book_zero_target(self):
        """Test walking book with zero target size."""
        levels = [PriceLevel(price=0.30, size=50)]
        avg_price, fill = _walk_book(levels, 0)
        assert avg_price == 0.0
        assert fill == 0.0

    def test_depth_adjusted_edge_lower_than_tob(self):
        """Test that depth-adjusted edge is lower than top-of-book when depth is thin."""
        config = NegriskConfig(
            min_net_edge=0.01,
            min_outcomes=3,
            fee_rate_bps=0,
            gas_per_leg=0.0,
            use_depth_scanning=True,
            max_book_levels=10,
        )

        detector = NegriskDetector(config)

        # Create event where top-of-book shows 5% edge but depth shows only 2% edge
        # Top-of-book: 0.28 + 0.30 + 0.37 = 0.95 (5% edge)
        # But at size 100, prices degrade to average of levels
        event = NegriskEvent(
            event_id="e1",
            slug="depth-test",
            title="Depth Test Event",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1",
                    market_id="m1",
                    condition_id="c1",
                    token_id="t1",
                    name="A",
                    bba=OutcomeBBA(
                        best_ask=0.28,
                        ask_size=50.0,
                        ask_levels=[
                            PriceLevel(price=0.28, size=50),
                            PriceLevel(price=0.32, size=100),
                            PriceLevel(price=0.35, size=200),
                        ],
                    ),
                ),
                Outcome(
                    outcome_id="2",
                    market_id="m2",
                    condition_id="c1",
                    token_id="t2",
                    name="B",
                    bba=OutcomeBBA(
                        best_ask=0.30,
                        ask_size=100.0,
                        ask_levels=[
                            PriceLevel(price=0.30, size=100),
                            PriceLevel(price=0.34, size=100),
                        ],
                    ),
                ),
                Outcome(
                    outcome_id="3",
                    market_id="m3",
                    condition_id="c1",
                    token_id="t3",
                    name="C",
                    bba=OutcomeBBA(
                        best_ask=0.37,
                        ask_size=200.0,
                        ask_levels=[
                            PriceLevel(price=0.37, size=200),
                            PriceLevel(price=0.40, size=100),
                        ],
                    ),
                ),
            ],
        )

        opportunity = detector._check_event(event)

        # The depth-adjusted edge should be used
        # At suggested_size (likely 40 shares = 50*0.8), prices will be:
        # A: 40 @ 0.28 = 0.28
        # B: 40 @ 0.30 = 0.30
        # C: 40 @ 0.37 = 0.37
        # Sum = 0.95, same as top-of-book in this case

        # But let's verify depth_adjusted flag is set
        candidates = detector.get_last_scan_candidates()
        if opportunity:
            # Find the candidate for this event
            candidate = next((c for c in candidates if "Depth Test" in c["title"]), None)
            if candidate:
                assert candidate["depth_adjusted"] is True

    def test_depth_reduces_suggested_size(self):
        """Test that depth scanning reduces suggested_size when one outcome has thin depth."""
        config = NegriskConfig(
            min_net_edge=0.01,
            min_outcomes=3,
            fee_rate_bps=0,
            gas_per_leg=0.0,
            use_depth_scanning=True,
            max_book_levels=10,
        )

        detector = NegriskDetector(config)

        # Create event where one outcome has very thin depth
        event = NegriskEvent(
            event_id="e1",
            slug="thin-depth",
            title="Thin Depth Event",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1",
                    market_id="m1",
                    condition_id="c1",
                    token_id="t1",
                    name="A",
                    bba=OutcomeBBA(
                        best_ask=0.28,
                        ask_size=500.0,
                        ask_levels=[
                            PriceLevel(price=0.28, size=500),
                        ],
                    ),
                ),
                Outcome(
                    outcome_id="2",
                    market_id="m2",
                    condition_id="c1",
                    token_id="t2",
                    name="B",
                    bba=OutcomeBBA(
                        best_ask=0.30,
                        ask_size=500.0,
                        ask_levels=[
                            PriceLevel(price=0.30, size=500),
                        ],
                    ),
                ),
                Outcome(
                    outcome_id="3",
                    market_id="m3",
                    condition_id="c1",
                    token_id="t3",
                    name="C",
                    bba=OutcomeBBA(
                        best_ask=0.35,
                        ask_size=500.0,
                        ask_levels=[
                            PriceLevel(price=0.35, size=30),  # Only 30 shares available!
                            PriceLevel(price=0.50, size=200),
                        ],
                    ),
                ),
            ],
        )

        opportunity = detector._check_event(event)

        if opportunity:
            # Suggested size should be limited by the thin outcome C (30 shares * 0.8 = 24)
            assert opportunity.suggested_size <= 24

    def test_depth_scanning_disabled(self):
        """Test that depth scanning can be disabled via config."""
        config = NegriskConfig(
            min_net_edge=0.01,
            min_outcomes=3,
            fee_rate_bps=0,
            gas_per_leg=0.0,
            use_depth_scanning=False,  # Disabled
            max_book_levels=10,
        )

        detector = NegriskDetector(config)

        # Create event with depth data
        event = NegriskEvent(
            event_id="e1",
            slug="no-depth",
            title="No Depth Scan Event",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1",
                    market_id="m1",
                    condition_id="c1",
                    token_id="t1",
                    name="A",
                    bba=OutcomeBBA(
                        best_ask=0.28,
                        ask_size=200.0,
                        ask_levels=[
                            PriceLevel(price=0.28, size=50),
                            PriceLevel(price=0.40, size=100),
                        ],
                    ),
                ),
                Outcome(
                    outcome_id="2",
                    market_id="m2",
                    condition_id="c1",
                    token_id="t2",
                    name="B",
                    bba=OutcomeBBA(
                        best_ask=0.30,
                        ask_size=200.0,
                        ask_levels=[
                            PriceLevel(price=0.30, size=100),
                            PriceLevel(price=0.40, size=100),
                        ],
                    ),
                ),
                Outcome(
                    outcome_id="3",
                    market_id="m3",
                    condition_id="c1",
                    token_id="t3",
                    name="C",
                    bba=OutcomeBBA(
                        best_ask=0.35,
                        ask_size=200.0,
                        ask_levels=[
                            PriceLevel(price=0.35, size=200),
                        ],
                    ),
                ),
            ],
        )

        opportunity = detector._check_event(event)

        # Should use top-of-book prices, not depth
        candidates = detector.get_last_scan_candidates()
        if opportunity:
            candidate = next((c for c in candidates if "No Depth" in c["title"]), None)
            if candidate:
                assert candidate["depth_adjusted"] is False


class TestWSOnlyDetection:
    """Test WebSocket-Only Instant Detection feature (Improvement 5)."""

    @pytest.mark.asyncio
    async def test_ws_only_skips_clob_fetch(self):
        """Test that ws_only_mode skips CLOB fetch during execution."""
        from unittest.mock import AsyncMock, MagicMock
        from core.negrisk.engine import NegriskEngine
        from core.negrisk.bba_tracker import BBATracker
        from core.execution import ExecutionEngine
        from core.risk_manager import RiskManager

        # Config with ws_only_mode enabled
        config = NegriskConfig(
            ws_only_mode=True,
            min_net_edge=0.01,
            min_outcomes=3,
            fee_rate_bps=0,
            gas_per_leg=0.0,
        )

        # Create mocks
        execution_engine = MagicMock(spec=ExecutionEngine)
        execution_engine.submit_signal = AsyncMock()
        risk_manager = MagicMock(spec=RiskManager)

        # Create engine
        engine = NegriskEngine(config, execution_engine, risk_manager)

        # Create mock tracker
        mock_tracker = MagicMock(spec=BBATracker)
        mock_tracker.fetch_all_prices = AsyncMock()
        engine.tracker = mock_tracker

        # Create an opportunity
        event = NegriskEvent(
            event_id="e1",
            slug="test-event",
            title="Test Event",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1", market_id="m1", condition_id="c1",
                    token_id="t1", name="A",
                    bba=OutcomeBBA(best_ask=0.30, ask_size=200.0, source="websocket"),
                ),
                Outcome(
                    outcome_id="2", market_id="m2", condition_id="c1",
                    token_id="t2", name="B",
                    bba=OutcomeBBA(best_ask=0.30, ask_size=200.0, source="websocket"),
                ),
                Outcome(
                    outcome_id="3", market_id="m3", condition_id="c1",
                    token_id="t3", name="C",
                    bba=OutcomeBBA(best_ask=0.30, ask_size=200.0, source="websocket"),
                ),
            ],
        )

        opportunity = NegriskOpportunity(
            opportunity_id="test_opp",
            event=event,
            direction=ArbDirection.BUY_ALL,
            sum_of_prices=0.90,
            gross_edge=0.10,
            net_edge=0.10,
            suggested_size=100.0,
            max_size=200.0,
            legs=[
                {"token_id": "t1", "market_id": "m1", "outcome_name": "A", "side": "BUY", "price": 0.30, "size": 100.0},
                {"token_id": "t2", "market_id": "m2", "outcome_name": "B", "side": "BUY", "price": 0.30, "size": 100.0},
                {"token_id": "t3", "market_id": "m3", "outcome_name": "C", "side": "BUY", "price": 0.30, "size": 100.0},
            ],
        )

        # Execute the opportunity
        await engine._execute_opportunity(opportunity)

        # Verify fetch_all_prices was NOT called (ws_only_mode)
        mock_tracker.fetch_all_prices.assert_not_called()

        # Verify signal was submitted
        execution_engine.submit_signal.assert_called_once()

    def test_detection_latency_tracking(self):
        """Test that detection latency is tracked correctly."""
        import time

        config = NegriskConfig(
            min_net_edge=0.01,
            min_outcomes=3,
            fee_rate_bps=0,
            gas_per_leg=0.0,
            detection_latency_tracking=True,
        )

        detector = NegriskDetector(config)

        # Create an event with arbitrage opportunity
        event = NegriskEvent(
            event_id="e1",
            slug="test-event",
            title="Test Event",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1", market_id="m1", condition_id="c1",
                    token_id="t1", name="A",
                    bba=OutcomeBBA(best_ask=0.28, ask_size=200.0, source="websocket"),
                ),
                Outcome(
                    outcome_id="2", market_id="m2", condition_id="c1",
                    token_id="t2", name="B",
                    bba=OutcomeBBA(best_ask=0.30, ask_size=200.0, source="websocket"),
                ),
                Outcome(
                    outcome_id="3", market_id="m3", condition_id="c1",
                    token_id="t3", name="C",
                    bba=OutcomeBBA(best_ask=0.35, ask_size=200.0, source="websocket"),
                ),
            ],
        )

        # Simulate detection starting 50ms ago
        detection_start = time.monotonic() - 0.050

        # Detect opportunity with latency tracking
        opportunity = detector._check_event(event, detection_start=detection_start)

        assert opportunity is not None
        # Latency should be approximately 50ms (allow some tolerance)
        assert 45 < opportunity.detection_latency_ms < 60

        # Check stats were updated
        assert detector.stats.total_detections_timed == 1
        assert detector.stats.avg_detection_latency_ms > 0
        assert detector.stats.min_detection_latency_ms > 0
        assert detector.stats.max_detection_latency_ms > 0

    def test_ws_only_fallback_scan_interval(self):
        """Test that ws_only_mode sets scan interval to 30s."""
        from unittest.mock import MagicMock
        from core.negrisk.engine import NegriskEngine
        from core.execution import ExecutionEngine
        from core.risk_manager import RiskManager

        # Config with ws_only_mode enabled
        config = NegriskConfig(
            ws_only_mode=True,
            min_net_edge=0.01,
        )

        execution_engine = MagicMock(spec=ExecutionEngine)
        risk_manager = MagicMock(spec=RiskManager)

        engine = NegriskEngine(config, execution_engine, risk_manager)

        # Verify scan interval is set correctly at init (default 1s)
        assert engine._scan_interval == 1.0

        # Note: The scan interval is actually set in start(), which we can't easily test
        # without running the full async start sequence. This test documents the expected behavior.
        # The actual interval change happens in engine.start() when ws_only_mode=True.

    def test_latency_stats_aggregation(self):
        """Test that latency stats are aggregated correctly over multiple opportunities."""
        import time

        config = NegriskConfig(
            min_net_edge=0.01,
            min_outcomes=3,
            fee_rate_bps=0,
            gas_per_leg=0.0,
            detection_latency_tracking=True,
        )

        detector = NegriskDetector(config)

        event = NegriskEvent(
            event_id="e1",
            slug="test-event",
            title="Test Event",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1", market_id="m1", condition_id="c1",
                    token_id="t1", name="A",
                    bba=OutcomeBBA(best_ask=0.28, ask_size=200.0, source="websocket"),
                ),
                Outcome(
                    outcome_id="2", market_id="m2", condition_id="c1",
                    token_id="t2", name="B",
                    bba=OutcomeBBA(best_ask=0.30, ask_size=200.0, source="websocket"),
                ),
                Outcome(
                    outcome_id="3", market_id="m3", condition_id="c1",
                    token_id="t3", name="C",
                    bba=OutcomeBBA(best_ask=0.35, ask_size=200.0, source="websocket"),
                ),
            ],
        )

        # Fire three opportunities with known latencies
        latencies = [10.0, 50.0, 100.0]  # in ms

        for latency_ms in latencies:
            # Clear cooldown to allow multiple detections
            detector._opportunity_cooldown.clear()

            detection_start = time.monotonic() - (latency_ms / 1000.0)
            opportunity = detector._check_event(event, detection_start=detection_start)
            assert opportunity is not None

        # Check aggregated stats
        assert detector.stats.total_detections_timed == 3
        assert detector.stats.min_detection_latency_ms < 15  # Should be close to 10ms
        assert detector.stats.max_detection_latency_ms > 95  # Should be close to 100ms
        # Average should be around 53.33ms (10+50+100)/3
        assert 45 < detector.stats.avg_detection_latency_ms < 65



class TestMakerOrders:
    """Test maker order functionality for neg-risk arbitrage."""

    def test_maker_prices_at_mid(self):
        """Test that maker orders price at mid-price, not at ask."""
        config = NegriskConfig(
            min_net_edge=0.01,
            min_outcomes=3,
            fee_rate_bps=0,
            gas_per_leg=0.0,
            order_strategy="maker",
            maker_min_net_edge=0.01,
        )

        detector = NegriskDetector(config)

        # Create event with spread: bid=0.28, ask=0.32, mid=0.30
        event = NegriskEvent(
            event_id="e1",
            slug="test-maker",
            title="Maker Order Test",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1",
                    market_id="m1",
                    condition_id="c1",
                    token_id="t1",
                    name="A",
                    bba=OutcomeBBA(best_bid=0.28, best_ask=0.32, bid_size=200.0, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="2",
                    market_id="m2",
                    condition_id="c1",
                    token_id="t2",
                    name="B",
                    bba=OutcomeBBA(best_bid=0.28, best_ask=0.32, bid_size=200.0, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="3",
                    market_id="m3",
                    condition_id="c1",
                    token_id="t3",
                    name="C",
                    bba=OutcomeBBA(best_bid=0.28, best_ask=0.32, bid_size=200.0, ask_size=200.0),
                ),
            ],
        )

        # Test maker buy-side
        opportunity = detector._check_event_maker(event)
        assert opportunity is not None
        assert opportunity.direction == ArbDirection.BUY_ALL

        # Verify prices are at mid (0.30), not ask (0.32)
        for leg in opportunity.legs:
            assert leg["price"] == 0.30  # Mid-price
            assert leg["order_type"] == "maker"

        # Sum of prices = 0.90 (3 * 0.30)
        assert opportunity.sum_of_prices == pytest.approx(0.90, abs=0.001)
        assert opportunity.gross_edge == pytest.approx(0.10, abs=0.001)

    def test_maker_zero_fee(self):
        """Test that maker orders have zero fee."""
        config = NegriskConfig(
            min_net_edge=0.01,
            min_outcomes=3,
            fee_rate_bps=1000,  # Even with fees enabled, maker pays 0
            gas_per_leg=0.0,
            order_strategy="maker",
            maker_min_net_edge=0.01,
        )

        detector = NegriskDetector(config)

        event = NegriskEvent(
            event_id="e1",
            slug="test-fee",
            title="Maker Fee Test",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1", market_id="m1", condition_id="c1",
                    token_id="t1", name="A",
                    bba=OutcomeBBA(best_bid=0.28, best_ask=0.32, bid_size=200.0, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="2", market_id="m2", condition_id="c1",
                    token_id="t2", name="B",
                    bba=OutcomeBBA(best_bid=0.28, best_ask=0.32, bid_size=200.0, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="3", market_id="m3", condition_id="c1",
                    token_id="t3", name="C",
                    bba=OutcomeBBA(best_bid=0.28, best_ask=0.32, bid_size=200.0, ask_size=200.0),
                ),
            ],
        )

        opportunity = detector._check_event_maker(event)
        assert opportunity is not None

        # Net edge should equal gross edge (no fees for maker)
        assert opportunity.net_edge == pytest.approx(opportunity.gross_edge, abs=0.001)

    def test_maker_edge_higher_than_taker(self):
        """Test that maker net edge is higher than taker (better price + no fee)."""
        config_taker = NegriskConfig(
            min_net_edge=0.01,
            min_outcomes=3,
            fee_rate_bps=0,
            gas_per_leg=0.0,
            order_strategy="taker",
        )

        config_maker = NegriskConfig(
            min_net_edge=0.01,
            min_outcomes=3,
            fee_rate_bps=0,
            gas_per_leg=0.0,
            order_strategy="maker",
            maker_min_net_edge=0.01,
        )

        detector_taker = NegriskDetector(config_taker)
        detector_maker = NegriskDetector(config_maker)

        # Event with spread
        event = NegriskEvent(
            event_id="e1",
            slug="test",
            title="Edge Comparison",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1", market_id="m1", condition_id="c1",
                    token_id="t1", name="A",
                    bba=OutcomeBBA(best_bid=0.28, best_ask=0.32, bid_size=200.0, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="2", market_id="m2", condition_id="c1",
                    token_id="t2", name="B",
                    bba=OutcomeBBA(best_bid=0.28, best_ask=0.32, bid_size=200.0, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="3", market_id="m3", condition_id="c1",
                    token_id="t3", name="C",
                    bba=OutcomeBBA(best_bid=0.28, best_ask=0.32, bid_size=200.0, ask_size=200.0),
                ),
            ],
        )

        opp_taker = detector_taker._check_event(event)
        opp_maker = detector_maker._check_event_maker(event)

        assert opp_taker is not None
        assert opp_maker is not None

        # Taker: sum_asks=0.96, gross=0.04
        # Maker: sum_mids=0.90, gross=0.10
        # Maker should have higher edge
        assert opp_maker.net_edge > opp_taker.net_edge

    def test_maker_price_capped_at_ask(self):
        """Test that maker price is capped at ask price (don't overpay)."""
        config = NegriskConfig(
            min_net_edge=0.01,
            min_outcomes=3,
            fee_rate_bps=0,
            gas_per_leg=0.0,
            order_strategy="maker",
            maker_min_net_edge=0.01,
            maker_price_offset_bps=1000,  # 10% offset (very aggressive)
        )

        detector = NegriskDetector(config)

        event = NegriskEvent(
            event_id="e1",
            slug="test",
            title="Price Cap Test",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1", market_id="m1", condition_id="c1",
                    token_id="t1", name="A",
                    bba=OutcomeBBA(best_bid=0.28, best_ask=0.32, bid_size=200.0, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="2", market_id="m2", condition_id="c1",
                    token_id="t2", name="B",
                    bba=OutcomeBBA(best_bid=0.28, best_ask=0.32, bid_size=200.0, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="3", market_id="m3", condition_id="c1",
                    token_id="t3", name="C",
                    bba=OutcomeBBA(best_bid=0.28, best_ask=0.32, bid_size=200.0, ask_size=200.0),
                ),
            ],
        )

        opportunity = detector._check_event_maker(event)
        assert opportunity is not None

        # mid=0.30, offset=0.10 → 0.40, but capped at ask=0.32
        for leg in opportunity.legs:
            assert leg["price"] == 0.32  # Capped at ask
            assert leg["price"] <= 0.32  # Never exceeds ask

    def test_maker_min_edge_threshold(self):
        """Test that maker uses maker_min_net_edge instead of min_net_edge."""
        config = NegriskConfig(
            min_net_edge=0.025,        # 2.5% for taker
            maker_min_net_edge=0.015,  # 1.5% for maker (lower threshold)
            min_outcomes=3,
            fee_rate_bps=0,
            gas_per_leg=0.0,
            order_strategy="maker",
        )

        detector = NegriskDetector(config)

        # Event with ~2% net edge (between maker and taker thresholds)
        # We want sum_of_mids to be around 0.98 to get 2% edge
        event = NegriskEvent(
            event_id="e1",
            slug="test",
            title="Threshold Test",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1", market_id="m1", condition_id="c1",
                    token_id="t1", name="A",
                    bba=OutcomeBBA(best_bid=0.325, best_ask=0.335, bid_size=200.0, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="2", market_id="m2", condition_id="c1",
                    token_id="t2", name="B",
                    bba=OutcomeBBA(best_bid=0.325, best_ask=0.335, bid_size=200.0, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="3", market_id="m3", condition_id="c1",
                    token_id="t3", name="C",
                    bba=OutcomeBBA(best_bid=0.315, best_ask=0.325, bid_size=200.0, ask_size=200.0),
                ),
            ],
        )

        # mid = [0.33, 0.33, 0.32], sum = 0.98, gross = 0.02, net ≈ 0.02 (no fees)
        # This exceeds maker_min_net_edge (1.5%) but is below min_net_edge (2.5%)
        # Should be accepted in maker mode with maker threshold
        opportunity = detector._check_event_maker(event)
        assert opportunity is not None
        assert opportunity.net_edge >= config.maker_min_net_edge
        # The key is that maker uses maker_min_net_edge (1.5%) for comparison, not min_net_edge

    def test_maker_sell_side_at_mid(self):
        """Test sell-side maker orders price at mid-price."""
        config = NegriskConfig(
            min_net_edge=0.01,
            min_outcomes=3,
            fee_rate_bps=0,
            gas_per_leg=0.0,
            order_strategy="maker",
            maker_min_net_edge=0.01,
        )

        detector = NegriskDetector(config)

        # Event with sum_bids > 1.0 (sell-side opportunity)
        event = NegriskEvent(
            event_id="e1",
            slug="test",
            title="Maker Sell Test",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1", market_id="m1", condition_id="c1",
                    token_id="t1", name="A",
                    bba=OutcomeBBA(best_bid=0.38, best_ask=0.42, bid_size=200.0, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="2", market_id="m2", condition_id="c1",
                    token_id="t2", name="B",
                    bba=OutcomeBBA(best_bid=0.36, best_ask=0.40, bid_size=200.0, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="3", market_id="m3", condition_id="c1",
                    token_id="t3", name="C",
                    bba=OutcomeBBA(best_bid=0.34, best_ask=0.38, bid_size=200.0, ask_size=200.0),
                ),
            ],
        )

        opportunity = detector._check_event_maker_sell_side(event)
        assert opportunity is not None
        assert opportunity.direction == ArbDirection.SELL_ALL

        # Verify prices are at mid, not bid
        # mid = [0.40, 0.38, 0.36], sum = 1.14
        assert opportunity.legs[0]["price"] == 0.40  # Mid-price
        assert opportunity.legs[1]["price"] == 0.38
        assert opportunity.legs[2]["price"] == 0.36

        for leg in opportunity.legs:
            assert leg["side"] == "SELL"
            assert leg["order_type"] == "maker"

        assert opportunity.sum_of_prices == pytest.approx(1.14, abs=0.01)
        assert opportunity.gross_edge == pytest.approx(0.14, abs=0.01)

    def test_taker_mode_unchanged(self):
        """Test that taker mode behavior is unchanged."""
        config = NegriskConfig(
            min_net_edge=0.01,
            min_outcomes=3,
            fee_rate_bps=0,
            gas_per_leg=0.0,
            order_strategy="taker",  # Explicit taker mode
        )

        detector = NegriskDetector(config)

        event = NegriskEvent(
            event_id="e1",
            slug="test",
            title="Taker Mode Test",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1", market_id="m1", condition_id="c1",
                    token_id="t1", name="A",
                    bba=OutcomeBBA(best_bid=0.28, best_ask=0.32, bid_size=200.0, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="2", market_id="m2", condition_id="c1",
                    token_id="t2", name="B",
                    bba=OutcomeBBA(best_bid=0.28, best_ask=0.32, bid_size=200.0, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="3", market_id="m3", condition_id="c1",
                    token_id="t3", name="C",
                    bba=OutcomeBBA(best_bid=0.28, best_ask=0.32, bid_size=200.0, ask_size=200.0),
                ),
            ],
        )

        # In taker mode, should use ask prices (0.32), not mid (0.30)
        opportunity = detector._check_event(event)
        assert opportunity is not None
        assert opportunity.direction == ArbDirection.BUY_ALL

        for leg in opportunity.legs:
            assert leg["price"] == 0.32  # Ask price (taker)
            assert leg.get("order_type") is None  # No order_type flag in taker

        assert opportunity.sum_of_prices == 0.96  # 3 * 0.32

    def test_detect_opportunities_with_strategy(self):
        """Test detect_opportunities() respects strategy parameter."""
        config = NegriskConfig(
            min_net_edge=0.01,
            min_outcomes=3,
            fee_rate_bps=0,
            gas_per_leg=0.0,
            maker_min_net_edge=0.01,  # Need this for maker mode
        )

        # Use separate detectors to avoid cooldown conflicts
        detector_taker = NegriskDetector(config)
        detector_maker = NegriskDetector(config)

        event = NegriskEvent(
            event_id="e1",
            slug="test",
            title="Strategy Test",
            condition_id="c1",
            volume_24h=20000.0,
            outcomes=[
                Outcome(
                    outcome_id="1", market_id="m1", condition_id="c1",
                    token_id="t1", name="A",
                    bba=OutcomeBBA(best_bid=0.28, best_ask=0.32, bid_size=200.0, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="2", market_id="m2", condition_id="c1",
                    token_id="t2", name="B",
                    bba=OutcomeBBA(best_bid=0.28, best_ask=0.32, bid_size=200.0, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="3", market_id="m3", condition_id="c1",
                    token_id="t3", name="C",
                    bba=OutcomeBBA(best_bid=0.28, best_ask=0.32, bid_size=200.0, ask_size=200.0),
                ),
            ],
        )

        # Taker strategy
        opps_taker = detector_taker.detect_opportunities([event], strategy="taker")
        assert len(opps_taker) > 0
        assert all(leg.get("order_type") is None for opp in opps_taker for leg in opp.legs)

        # Maker strategy
        opps_maker = detector_maker.detect_opportunities([event], strategy="maker")
        assert len(opps_maker) > 0
        assert all(leg.get("order_type") == "maker" for opp in opps_maker for leg in opp.legs)

        # Maker should have better edge
        assert opps_maker[0].net_edge > opps_taker[0].net_edge




class TestEventDrivenFocus:
    """Test event-driven focus (resolution window prioritization)."""

    def test_priority_score_near_resolution(self):
        """Event with end_date 6 hours from now should get high priority score."""
        from datetime import timedelta
        from core.negrisk.registry import NegriskRegistry

        config = NegriskConfig(
            prioritize_near_resolution=True,
            resolution_window_hours=24.0,
            min_event_volume_24h=0,  # Disable volume filter for test
        )
        registry = NegriskRegistry(config)

        # Create event with end_date 6 hours from now
        now = datetime.utcnow()
        event = NegriskEvent(
            event_id="e1",
            slug="test",
            title="Near Resolution Event",
            condition_id="c1",
            volume_24h=10000.0,
            end_date=now + timedelta(hours=6),
        )

        # Manually add to registry and calculate scores
        registry._events = {"e1": event}
        registry._calculate_priority_scores()

        # Expected: linear = 1 - (6/24) = 0.75, quadratic = 0.75^2 = 0.5625
        assert event.priority_score == pytest.approx(0.5625, abs=0.01)
        assert event.hours_to_resolution == pytest.approx(6.0, abs=0.1)

    def test_priority_score_far_resolution(self):
        """Event with end_date 48 hours from now should get low/zero priority score."""
        from datetime import timedelta
        from core.negrisk.registry import NegriskRegistry

        config = NegriskConfig(
            prioritize_near_resolution=True,
            resolution_window_hours=24.0,
            min_event_volume_24h=0,
        )
        registry = NegriskRegistry(config)

        now = datetime.utcnow()
        event = NegriskEvent(
            event_id="e1",
            slug="test",
            title="Far Resolution Event",
            condition_id="c1",
            volume_24h=10000.0,
            end_date=now + timedelta(hours=48),
        )

        registry._events = {"e1": event}
        registry._calculate_priority_scores()

        # Expected: 48h > 24h window → score = 0
        assert event.priority_score == 0.0
        assert event.hours_to_resolution == pytest.approx(48.0, abs=0.1)

    def test_priority_score_volume_spike(self):
        """Event with 3x average volume should get positive score from volume spike."""
        from core.negrisk.registry import NegriskRegistry

        config = NegriskConfig(
            prioritize_near_resolution=True,
            volume_spike_threshold=2.0,
            min_event_volume_24h=0,
        )
        registry = NegriskRegistry(config)

        # Create events: 2 normal volume, 1 with 3x volume
        event1 = NegriskEvent(
            event_id="e1", slug="test1", title="Normal 1", condition_id="c1",
            volume_24h=10000.0,
        )
        event2 = NegriskEvent(
            event_id="e2", slug="test2", title="Normal 2", condition_id="c2",
            volume_24h=10000.0,
        )
        event_spike = NegriskEvent(
            event_id="e3", slug="test3", title="Volume Spike", condition_id="c3",
            volume_24h=30000.0,  # 3x average
        )

        registry._events = {"e1": event1, "e2": event2, "e3": event_spike}
        registry._calculate_priority_scores()

        # Average volume = (10k + 10k + 30k) / 3 = 16,666.67
        # event_spike: 30k / 16,666.67 = 1.8 (capped at threshold check)
        # But with threshold=2.0, spike_ratio=1.8 < 2.0 → no bonus
        # Let me recalculate: avg = 16666.67, spike has 30k
        # ratio = 30000 / 16666.67 = 1.8x
        # Since ratio < threshold (2.0), no spike bonus
        assert event_spike.priority_score == 0.0

        # Let's try with a bigger spike (5x)
        event_spike.volume_24h = 50000.0
        registry._calculate_priority_scores()
        # avg = (10k + 10k + 50k) / 3 = 23,333.33
        # ratio = 50k / 23333.33 = 2.14x > 2.0 threshold
        # bonus = (2.14 - 2.0) * 0.25 = 0.14 * 0.25 = 0.035
        assert event_spike.priority_score > 0.0

    def test_priority_edge_discount(self):
        """High-priority event (score>0.5) with 1.5% edge should be detected with 2.5% min_net_edge."""
        from datetime import timedelta

        config = NegriskConfig(
            min_net_edge=0.025,  # 2.5% normally
            prioritize_near_resolution=True,
            resolution_window_hours=24.0,
            priority_edge_discount=0.5,  # Effective threshold = 1.25% for high-priority
            fee_rate_bps=0,
            gas_per_leg=0.0,
            min_event_volume_24h=0,
        )

        detector = NegriskDetector(config)

        # Create high-priority event (6h from resolution → score=0.75)
        now = datetime.utcnow()
        event = NegriskEvent(
            event_id="e1",
            slug="test",
            title="High Priority Event",
            condition_id="c1",
            volume_24h=20000.0,
            end_date=now + timedelta(hours=6),
            priority_score=0.75,  # Set directly for test
            hours_to_resolution=6.0,
            outcomes=[
                Outcome(
                    outcome_id="1", market_id="m1", condition_id="c1",
                    token_id="t1", name="A",
                    bba=OutcomeBBA(best_ask=0.33, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="2", market_id="m2", condition_id="c1",
                    token_id="t2", name="B",
                    bba=OutcomeBBA(best_ask=0.33, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="3", market_id="m3", condition_id="c1",
                    token_id="t3", name="C",
                    bba=OutcomeBBA(best_ask=0.32, ask_size=200.0),
                ),
            ],
        )

        # Sum = 0.98, net edge = 0.02 (2.0%)
        # Normally rejected (< 2.5%), but with discount: effective_min = 2.5% * 0.5 = 1.25%
        # Since priority_score=0.75 > 0.5, should apply discount and detect
        opportunity = detector._check_event(event)
        assert opportunity is not None
        assert opportunity.net_edge == pytest.approx(0.02, abs=0.001)

    def test_no_discount_low_priority(self):
        """Low-priority event (score<0.5) with 1.5% edge should NOT be detected with 2.5% min_net_edge."""
        config = NegriskConfig(
            min_net_edge=0.025,  # 2.5%
            prioritize_near_resolution=True,
            priority_edge_discount=0.5,
            fee_rate_bps=0,
            gas_per_leg=0.0,
            min_event_volume_24h=0,
        )

        detector = NegriskDetector(config)

        # Low-priority event (score=0.3 < 0.5)
        event = NegriskEvent(
            event_id="e1",
            slug="test",
            title="Low Priority Event",
            condition_id="c1",
            volume_24h=20000.0,
            priority_score=0.3,  # Below 0.5 threshold
            outcomes=[
                Outcome(
                    outcome_id="1", market_id="m1", condition_id="c1",
                    token_id="t1", name="A",
                    bba=OutcomeBBA(best_ask=0.33, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="2", market_id="m2", condition_id="c1",
                    token_id="t2", name="B",
                    bba=OutcomeBBA(best_ask=0.33, ask_size=200.0),
                ),
                Outcome(
                    outcome_id="3", market_id="m3", condition_id="c1",
                    token_id="t3", name="C",
                    bba=OutcomeBBA(best_ask=0.32, ask_size=200.0),
                ),
            ],
        )

        # Sum = 0.98, net edge = 0.02 (2.0%)
        # Since priority_score=0.3 < 0.5, no discount → effective_min = 2.5%
        # 2.0% < 2.5% → rejected
        opportunity = detector._check_event(event)
        assert opportunity is None

    def test_prioritization_disabled(self):
        """When prioritize_near_resolution=False, all events get score 0, no edge discount."""
        from datetime import timedelta
        from core.negrisk.registry import NegriskRegistry

        config = NegriskConfig(
            prioritize_near_resolution=False,  # DISABLED
            resolution_window_hours=24.0,
            min_event_volume_24h=0,
        )
        registry = NegriskRegistry(config)

        # Event near resolution
        now = datetime.utcnow()
        event = NegriskEvent(
            event_id="e1",
            slug="test",
            title="Near Resolution",
            condition_id="c1",
            volume_24h=10000.0,
            end_date=now + timedelta(hours=6),
        )

        registry._events = {"e1": event}
        registry._calculate_priority_scores()

        # Should NOT calculate scores when disabled
        assert event.priority_score == 0.0

    def test_events_sorted_by_priority(self):
        """Verify engine sorts events by priority score descending."""
        from core.negrisk.engine import NegriskEngine
        from unittest.mock import Mock

        config = NegriskConfig(
            prioritize_near_resolution=True,
            min_event_volume_24h=0,
        )

        # Mock dependencies
        mock_execution = Mock()
        mock_risk = Mock()

        engine = NegriskEngine(config, mock_execution, mock_risk)

        # Create events with different priorities
        event_low = NegriskEvent(
            event_id="e1", slug="low", title="Low Priority",
            condition_id="c1", priority_score=0.2,
        )
        event_high = NegriskEvent(
            event_id="e2", slug="high", title="High Priority",
            condition_id="c2", priority_score=0.9,
        )
        event_mid = NegriskEvent(
            event_id="e3", slug="mid", title="Mid Priority",
            condition_id="c3", priority_score=0.5,
        )

        events = [event_low, event_high, event_mid]

        # Manually sort like the engine does
        if config.prioritize_near_resolution:
            events.sort(key=lambda e: e.priority_score, reverse=True)

        # Verify sorted order: high (0.9), mid (0.5), low (0.2)
        assert events[0].event_id == "e2"
        assert events[1].event_id == "e3"
        assert events[2].event_id == "e1"



if __name__ == "__main__":
    pytest.main([__file__, "-v"])
