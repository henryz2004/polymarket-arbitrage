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
    Outcome,
    OutcomeBBA,
    OutcomeStatus,
)
from core.negrisk.detector import NegriskDetector


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
            bba=OutcomeBBA(best_ask=0.30, ask_size=50.0),  # Below 100 minimum
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
