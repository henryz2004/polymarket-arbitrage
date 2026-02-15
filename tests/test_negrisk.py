"""
Tests for Negrisk Arbitrage Module
====================================
"""

import pytest
from datetime import datetime

from core.negrisk.models import (
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
            taker_fee_bps=150,  # 1.5%
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
        # Fees = 1.5% * 0.93 = 0.01395
        # Gas = 0.01 * 3 = 0.03
        # Net edge = 0.07 - 0.01395 - 0.03 = 0.02605 (2.6%)

        opportunity = detector._check_event(event)
        assert opportunity is not None
        assert opportunity.sum_of_asks == 0.93
        assert opportunity.gross_edge == pytest.approx(0.07, rel=0.01)
        assert opportunity.net_edge > 0.01  # Profitable after fees
        assert opportunity.num_legs == 3

    def test_reject_insufficient_edge(self):
        """Test rejection when edge is too small."""
        config = NegriskConfig(
            min_net_edge=0.025,  # 2.5% minimum net edge (strict)
            taker_fee_bps=150,
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
