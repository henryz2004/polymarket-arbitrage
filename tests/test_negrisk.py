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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
