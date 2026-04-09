"""
Tests for watchdog backtester.

Tests use synthetic price data to verify the backtester correctly
replays data through the anomaly detector and catches expected spikes.
"""

import asyncio
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.watchdog.backtester import (
    ALL_SCENARIOS,
    IRAN_SCENARIOS,
    BacktestAlert,
    BacktestResult,
    BacktestScenario,
    WatchdogBacktester,
)
from core.watchdog.models import AnomalyAlert, PriceSnapshot, WatchdogConfig


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def config():
    """Default watchdog config for testing."""
    return WatchdogConfig(
        # Lower thresholds for testing
        absolute_thresholds=[(0.05, 1800), (0.10, 3600)],
        relative_thresholds=[(0.50, 3600), (1.00, 14400)],
        warmup_seconds=0,  # No warmup in tests
        price_poll_interval_seconds=60,
        min_price_floor=0.02,
        resolution_price_ceiling=0.96,
        alert_cooldown_seconds=60,
    )


@pytest.fixture
def backtester(config):
    return WatchdogBacktester(config)


def make_price_series(
    start_time: datetime,
    prices: list[float],
    interval_seconds: int = 60,
) -> list[PriceSnapshot]:
    """Create a synthetic price series."""
    snapshots = []
    for i, price in enumerate(prices):
        snapshots.append(PriceSnapshot(
            timestamp=start_time + timedelta(seconds=i * interval_seconds),
            mid_price=price,
            source="backtest",
        ))
    return snapshots


# ============================================================================
# BacktestScenario tests
# ============================================================================


class TestBacktestScenario:
    def test_scenario_fields(self):
        s = BacktestScenario(
            name="Test scenario",
            slug="test-slug",
            expect_alert=True,
        )
        assert s.name == "Test scenario"
        assert s.slug == "test-slug"
        assert s.expect_alert is True
        assert s.focus_token_ids == []

    def test_iran_scenarios_defined(self):
        """Verify built-in Iran scenarios exist."""
        assert len(IRAN_SCENARIOS) >= 4
        slugs = [s.slug for s in IRAN_SCENARIOS]
        assert "usisrael-strikes-iran-on" in slugs
        assert "us-x-iran-ceasefire-by" in slugs
        assert "us-forces-enter-iran-by" in slugs

    def test_all_scenarios_have_required_fields(self):
        for s in ALL_SCENARIOS:
            assert s.name, f"Scenario missing name"
            assert s.slug, f"Scenario {s.name} missing slug"
            assert isinstance(s.expect_alert, bool)


# ============================================================================
# BacktestResult tests
# ============================================================================


class TestBacktestResult:
    def test_empty_result(self):
        scenario = BacktestScenario(name="test", slug="test-slug", expect_alert=True)
        result = BacktestResult(
            scenario=scenario,
            alerts=[],
            tokens_fetched=0,
            price_points_total=0,
        )
        assert not result.caught
        assert not result.passed  # expected alert but none fired
        assert result.max_score == 0.0
        assert result.first_alert_time is None

    def test_result_with_alerts(self):
        scenario = BacktestScenario(name="test", slug="test-slug", expect_alert=True)
        alert = AnomalyAlert(
            alert_id="test",
            event_id="1",
            event_title="Test Event",
            event_slug="test-slug",
            outcome_name="Yes",
            token_id="token123",
            price_before=0.10,
            price_after=0.30,
            pct_change=2.0,
            abs_change=0.20,
            window_seconds=1800,
            threshold_type="absolute",
            suspicion_score=7.5,
            is_off_hours=True,
            event_volume_24h=50000,
            detected_at=datetime(2026, 3, 1, 10, 0),
        )
        bt_alert = BacktestAlert(
            scenario_name="test",
            alert=alert,
            simulated_time=datetime(2026, 3, 1, 10, 0),
        )
        result = BacktestResult(
            scenario=scenario,
            alerts=[bt_alert],
            tokens_fetched=1,
            price_points_total=100,
            time_range=(datetime(2026, 3, 1), datetime(2026, 3, 2)),
        )
        assert result.caught
        assert result.passed
        assert result.max_score == 7.5
        assert result.first_alert_time == datetime(2026, 3, 1, 10, 0)

    def test_passed_when_no_alert_expected(self):
        scenario = BacktestScenario(name="test", slug="test-slug", expect_alert=False)
        result = BacktestResult(
            scenario=scenario,
            alerts=[],
            tokens_fetched=1,
            price_points_total=100,
        )
        assert result.passed  # No alert expected, none fired = pass

    def test_failed_when_unexpected_alert(self):
        scenario = BacktestScenario(name="test", slug="test-slug", expect_alert=False)
        alert = AnomalyAlert(
            alert_id="test", event_id="1", event_title="Test", event_slug="test-slug",
            outcome_name="Yes", token_id="t", price_before=0.1, price_after=0.5,
            pct_change=4.0, abs_change=0.4, window_seconds=1800,
            threshold_type="absolute", suspicion_score=5.0, is_off_hours=False,
            event_volume_24h=10000, detected_at=datetime(2026, 1, 1),
        )
        result = BacktestResult(
            scenario=scenario,
            alerts=[BacktestAlert(
                scenario_name="test", alert=alert,
                simulated_time=datetime(2026, 1, 1),
            )],
            tokens_fetched=1,
            price_points_total=100,
        )
        assert not result.passed  # Alert fired but not expected

    def test_insider_window_detection(self):
        scenario = BacktestScenario(
            name="test", slug="test-slug", expect_alert=True,
            insider_window_start=datetime(2026, 3, 1, 8, 0),
            insider_window_end=datetime(2026, 3, 1, 14, 0),
        )
        alert = AnomalyAlert(
            alert_id="test", event_id="1", event_title="Test", event_slug="test-slug",
            outcome_name="Yes", token_id="t", price_before=0.07, price_after=0.25,
            pct_change=2.57, abs_change=0.18, window_seconds=1800,
            threshold_type="absolute", suspicion_score=8.0, is_off_hours=True,
            event_volume_24h=100000, detected_at=datetime(2026, 3, 1, 10, 0),
        )
        result = BacktestResult(
            scenario=scenario,
            alerts=[BacktestAlert(
                scenario_name="test", alert=alert,
                simulated_time=datetime(2026, 3, 1, 10, 0),  # Within window
            )],
            tokens_fetched=1,
            price_points_total=100,
        )
        assert result.caught_during_insider_window

    def test_summary_output(self):
        scenario = BacktestScenario(name="Test Scenario", slug="test", expect_alert=True)
        result = BacktestResult(
            scenario=scenario, alerts=[], tokens_fetched=5, price_points_total=1000,
        )
        summary = result.summary()
        assert "FAIL" in summary  # Expected alert but none fired
        assert "Test Scenario" in summary


# ============================================================================
# WatchdogBacktester core logic tests (synthetic data)
# ============================================================================


class TestBacktesterReplay:
    """Test the core replay engine with synthetic data."""

    def test_detects_sharp_spike(self, config):
        """A 7c -> 30c spike in 30 minutes should trigger an alert."""
        bt = WatchdogBacktester(config)

        # Create scenario
        scenario = BacktestScenario(
            name="Synthetic spike",
            slug="test-spike",
            expect_alert=True,
        )

        # Build synthetic market data
        market_data = [{
            "token_id": "token_spike",
            "event_id": "1",
            "event_title": "Test Event",
            "outcome_name": "Yes",
            "volume_24h": 50000,
        }]

        # Price: stable at 7c for 2 hours, then spike to 30c over 10 minutes
        t0 = datetime(2026, 3, 1, 0, 0)
        prices = [0.07] * 120  # 2 hours at 1/min
        # Spike
        prices += [0.10, 0.14, 0.18, 0.22, 0.25, 0.27, 0.28, 0.29, 0.30, 0.30]
        # Hold
        prices += [0.30] * 30

        price_data = {"token_spike": make_price_series(t0, prices, interval_seconds=60)}

        result = bt._run_from_cache(scenario, market_data, price_data)

        assert result.caught, f"Expected alert for 7c->30c spike. Got {len(result.alerts)} alerts."
        assert result.passed
        assert result.max_score >= 3.0  # Should be reasonably suspicious
        # First alert should be during/after the spike
        assert result.first_alert_time >= t0 + timedelta(hours=2)

    def test_no_alert_for_stable_prices(self, config):
        """Stable prices should not trigger any alerts."""
        bt = WatchdogBacktester(config)

        scenario = BacktestScenario(
            name="Stable market",
            slug="test-stable",
            expect_alert=False,
        )

        market_data = [{
            "token_id": "token_stable",
            "event_id": "1",
            "event_title": "Stable Event",
            "outcome_name": "Yes",
            "volume_24h": 50000,
        }]

        t0 = datetime(2026, 3, 1, 0, 0)
        # Price oscillates between 45c and 47c — no significant move
        prices = [0.45 + (i % 3) * 0.01 for i in range(300)]

        price_data = {"token_stable": make_price_series(t0, prices, interval_seconds=60)}

        result = bt._run_from_cache(scenario, market_data, price_data)

        assert not result.caught, f"Should not alert on stable prices. Got {len(result.alerts)} alerts."
        assert result.passed

    def test_detects_off_hours_spike(self, config):
        """A spike during off-hours (7-11 UTC) should get a higher score."""
        bt = WatchdogBacktester(config)

        scenario = BacktestScenario(
            name="Off-hours spike",
            slug="test-offhours",
            expect_alert=True,
        )

        market_data = [{
            "token_id": "token_oh",
            "event_id": "1",
            "event_title": "Off-Hours Event",
            "outcome_name": "Yes",
            "volume_24h": 50000,
        }]

        # Start at 6 UTC, spike happens at ~8 UTC (off-hours)
        t0 = datetime(2026, 3, 1, 6, 0)
        prices = [0.07] * 120  # 2 hours stable (6-8 UTC)
        prices += [0.10, 0.15, 0.20, 0.25, 0.30]  # Spike at 8 UTC
        prices += [0.30] * 30

        price_data = {"token_oh": make_price_series(t0, prices, interval_seconds=60)}

        result = bt._run_from_cache(scenario, market_data, price_data)

        assert result.caught
        # Off-hours + low baseline + magnitude should give high score
        assert result.max_score >= 5.0, f"Off-hours spike should score >=5, got {result.max_score}"

    def test_resolution_price_filtered(self, config):
        """Prices settling at 99c (resolution) should not trigger alerts."""
        bt = WatchdogBacktester(config)

        scenario = BacktestScenario(
            name="Resolution settle",
            slug="test-resolution",
            expect_alert=False,
        )

        market_data = [{
            "token_id": "token_res",
            "event_id": "1",
            "event_title": "Resolved Event",
            "outcome_name": "Yes",
            "volume_24h": 50000,
        }]

        t0 = datetime(2026, 3, 1, 0, 0)
        # Price at 50c for a while, then jumps to 99c (resolution)
        prices = [0.50] * 60
        prices += [0.70, 0.85, 0.92, 0.96, 0.99]
        prices += [0.99] * 60

        price_data = {"token_res": make_price_series(t0, prices, interval_seconds=60)}

        result = bt._run_from_cache(scenario, market_data, price_data)

        # The detector should filter out the 96c+ readings
        # However it might catch the 50c -> 92c move before it hits 96c
        # That's actually correct behavior — we want to catch the move toward resolution
        # So this test verifies the ceiling filter works for the final state
        # Note: there may still be alerts for the 50c->92c portion
        if result.caught:
            # Any alerts should be for prices below the ceiling
            for a in result.alerts:
                assert a.alert.price_after < 0.96, \
                    f"Alert at resolution price {a.alert.price_after} should be filtered"

    def test_multiple_tokens(self, config):
        """Test with multiple tokens in the same event."""
        bt = WatchdogBacktester(config)

        scenario = BacktestScenario(
            name="Multi-token event",
            slug="test-multi",
            expect_alert=True,
        )

        market_data = [
            {"token_id": "token_a", "event_id": "1", "event_title": "Multi Event",
             "outcome_name": "March 1", "volume_24h": 50000},
            {"token_id": "token_b", "event_id": "1", "event_title": "Multi Event",
             "outcome_name": "March 2", "volume_24h": 50000},
            {"token_id": "token_c", "event_id": "1", "event_title": "Multi Event",
             "outcome_name": "March 3", "volume_24h": 50000},
        ]

        t0 = datetime(2026, 3, 1, 0, 0)

        # Token A: stable
        prices_a = [0.30] * 200

        # Token B: spike! (the insider signal)
        prices_b = [0.05] * 120 + [0.08, 0.12, 0.18, 0.22, 0.28, 0.32] + [0.32] * 74

        # Token C: slow drift (not suspicious)
        prices_c = [0.20 + i * 0.0005 for i in range(200)]

        price_data = {
            "token_a": make_price_series(t0, prices_a),
            "token_b": make_price_series(t0, prices_b),
            "token_c": make_price_series(t0, prices_c),
        }

        result = bt._run_from_cache(scenario, market_data, price_data)

        assert result.caught
        assert result.tokens_fetched == 3
        # The alert should be for token_b
        spike_alerts = [a for a in result.alerts if a.alert.token_id == "token_b"]
        assert len(spike_alerts) > 0, "Expected alert for token_b spike"

    def test_low_price_floor_filtering(self, config):
        """Prices below min_price_floor should not trigger alerts."""
        bt = WatchdogBacktester(config)

        scenario = BacktestScenario(
            name="Sub-penny noise",
            slug="test-penny",
            expect_alert=False,
        )

        market_data = [{
            "token_id": "token_penny",
            "event_id": "1",
            "event_title": "Penny Event",
            "outcome_name": "Yes",
            "volume_24h": 50000,
        }]

        t0 = datetime(2026, 3, 1, 0, 0)
        # All prices below floor (2c)
        prices = [0.001] * 60 + [0.01, 0.015, 0.018, 0.019] + [0.019] * 60

        price_data = {"token_penny": make_price_series(t0, prices, interval_seconds=60)}

        result = bt._run_from_cache(scenario, market_data, price_data)

        assert not result.caught

    def test_time_range_filtering(self, config):
        """Scenario start/end times should restrict data window."""
        bt = WatchdogBacktester(config)

        t0 = datetime(2026, 3, 1, 0, 0)

        scenario = BacktestScenario(
            name="Time-filtered",
            slug="test-time",
            expect_alert=False,
            # Only look at the stable part (first 2 hours)
            start_time=t0,
            end_time=t0 + timedelta(hours=2),
        )

        market_data = [{
            "token_id": "token_tf",
            "event_id": "1",
            "event_title": "Time Event",
            "outcome_name": "Yes",
            "volume_24h": 50000,
        }]

        # Stable for 2 hours, then spike (but outside time window)
        prices = [0.10] * 120  # 2 hours
        prices += [0.10, 0.20, 0.40, 0.60, 0.80]  # Spike after window
        prices += [0.80] * 30

        price_data = {"token_tf": make_price_series(t0, prices, interval_seconds=60)}

        result = bt._run_from_cache(scenario, market_data, price_data)

        # Should not catch the spike since it's outside the time window
        assert not result.caught


# ============================================================================
# Cache tests
# ============================================================================


class TestBacktestCache:
    def test_save_and_load_cache(self, backtester, tmp_path):
        """Test cache save/load roundtrip."""
        cache_file = tmp_path / "test_cache.jsonl"

        markets = [{
            "token_id": "token_cache",
            "event_id": "1",
            "event_title": "Cache Test",
            "outcome_name": "Yes",
            "volume_24h": 50000,
        }]

        t0 = datetime(2026, 3, 1, 0, 0)
        snapshots = make_price_series(t0, [0.10, 0.15, 0.20, 0.25], interval_seconds=60)
        prices = {"token_cache": snapshots}

        backtester._save_cache(cache_file, markets, prices)
        loaded_markets, loaded_prices = backtester._load_cache(cache_file)

        assert len(loaded_markets) == 1
        assert loaded_markets[0]["token_id"] == "token_cache"
        assert "token_cache" in loaded_prices
        assert len(loaded_prices["token_cache"]) == 4

        # Verify prices match
        for orig, loaded in zip(snapshots, loaded_prices["token_cache"]):
            assert abs(orig.mid_price - loaded.mid_price) < 0.0001
            assert abs(orig.timestamp.timestamp() - loaded.timestamp.timestamp()) < 1

    def test_cache_roundtrip_preserves_detection(self, config, tmp_path):
        """Verify detection results are identical between fresh and cached runs."""
        bt = WatchdogBacktester(config)
        cache_file = tmp_path / "roundtrip.jsonl"

        scenario = BacktestScenario(
            name="Roundtrip test",
            slug="test-roundtrip",
            expect_alert=True,
        )

        market_data = [{
            "token_id": "token_rt",
            "event_id": "1",
            "event_title": "Roundtrip Event",
            "outcome_name": "Yes",
            "volume_24h": 50000,
        }]

        t0 = datetime(2026, 3, 1, 0, 0)
        prices = [0.07] * 120 + [0.10, 0.15, 0.20, 0.25, 0.30] + [0.30] * 30
        price_data = {"token_rt": make_price_series(t0, prices)}

        # Run fresh
        result1 = bt._run_from_cache(scenario, market_data, price_data)

        # Save and reload
        bt._save_cache(cache_file, market_data, price_data)
        loaded_markets, loaded_prices = bt._load_cache(cache_file)
        result2 = bt._run_from_cache(scenario, loaded_markets, loaded_prices)

        # Same number of alerts
        assert len(result1.alerts) == len(result2.alerts)
        assert result1.caught == result2.caught
        assert abs(result1.max_score - result2.max_score) < 0.01


# ============================================================================
# Integration test (requires network)
# ============================================================================


class TestBacktestIntegration:
    """Integration tests that hit the real API. Marked slow."""

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        True,  # Skip by default — run manually with: pytest -k "test_fetch_real" --no-header -rN
        reason="Requires network access to Gamma/CLOB APIs"
    )
    async def test_fetch_real_iran_strike_data(self, config):
        """Verify we can fetch real price data for the Iran strike market."""
        bt = WatchdogBacktester(config)
        async with httpx.AsyncClient(timeout=30.0) as client:
            markets = await bt._fetch_event_markets(client, "usisrael-strikes-iran-on")
            assert len(markets) > 0, "Should find Iran strike markets"

            # Fetch price history for the first active market
            for m in markets:
                history = await bt._fetch_price_history(client, m["token_id"])
                if history:
                    assert len(history) > 10, f"Expected substantial price history, got {len(history)}"
                    break
