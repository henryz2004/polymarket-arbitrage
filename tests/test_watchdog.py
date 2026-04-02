"""
Watchdog Unit Tests
====================

Tests for suspicious activity detection components.
"""

import asyncio
from collections import deque
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.watchdog.anomaly_detector import AnomalyDetector
from core.watchdog.models import AnomalyAlert, NewsHeadline, PriceSnapshot, WatchdogConfig
from core.watchdog.news_checker import NewsChecker
from core.watchdog.price_tracker import PriceTracker, WatchedMarket
from core.negrisk.models import NegriskEvent, Outcome, OutcomeBBA, OutcomeStatus


# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────

def make_config(**overrides) -> WatchdogConfig:
    """Create a WatchdogConfig with test-friendly defaults."""
    defaults = {
        "alert_cooldown_seconds": 0,  # No cooldown in tests
        "min_sample_interval_seconds": 0,  # No rate-limiting in tests
        "news_check_enabled": False,
    }
    defaults.update(overrides)
    return WatchdogConfig(**defaults)


def make_event(event_id="e1", title="US Strikes Iran by Feb 28",
               slug="us-strikes-iran", volume_24h=50000.0,
               outcomes=None) -> NegriskEvent:
    """Create a test NegriskEvent."""
    if outcomes is None:
        outcomes = [
            Outcome(
                outcome_id="o1_yes",
                market_id="m1",
                condition_id="c1",
                token_id="token_yes",
                name="Yes",
                status=OutcomeStatus.ACTIVE,
                bba=OutcomeBBA(best_bid=0.06, best_ask=0.08),
            ),
            Outcome(
                outcome_id="o2_yes",
                market_id="m2",
                condition_id="c2",
                token_id="token_no",
                name="No",
                status=OutcomeStatus.ACTIVE,
                bba=OutcomeBBA(best_bid=0.91, best_ask=0.93),
            ),
        ]
    return NegriskEvent(
        event_id=event_id,
        slug=slug,
        title=title,
        condition_id="cond1",
        outcomes=outcomes,
        volume_24h=volume_24h,
    )


def make_price_history(prices: list[float], interval_seconds: int = 60,
                       start_time: datetime = None,
                       source: str = "websocket") -> deque[PriceSnapshot]:
    """Create a deque of PriceSnapshots from a list of prices."""
    if start_time is None:
        start_time = datetime.utcnow() - timedelta(seconds=len(prices) * interval_seconds)

    history = deque()
    for i, price in enumerate(prices):
        ts = start_time + timedelta(seconds=i * interval_seconds)
        history.append(PriceSnapshot(
            timestamp=ts,
            mid_price=price,
            source=source,
        ))
    return history


def inject_history(market: WatchedMarket, history: deque) -> None:
    """Inject price history into both history and live_history deques."""
    market.history = history
    # Also populate live_history for sources that count as "live"
    market.live_history = deque(
        s for s in history if s.source not in ("clob_history", "gamma")
    )


# ──────────────────────────────────────────────────────────────
# PriceTracker Tests
# ──────────────────────────────────────────────────────────────

class TestPriceTracker:
    """Test PriceTracker rolling history and price change calculation."""

    def test_add_watch(self):
        """Adding a watch creates a WatchedMarket entry."""
        config = make_config()
        tracker = PriceTracker(config)
        event = make_event()

        tracker.add_watch(event, event.outcomes[0])

        assert "token_yes" in tracker.get_watched_markets()
        market = tracker.get_watched_markets()["token_yes"]
        assert market.event_id == "e1"
        assert market.outcome_name == "Yes"

    def test_add_watch_idempotent(self):
        """Adding same token twice doesn't create duplicate."""
        config = make_config()
        tracker = PriceTracker(config)
        event = make_event()

        tracker.add_watch(event, event.outcomes[0])
        tracker.add_watch(event, event.outcomes[0])

        assert len(tracker.get_watched_markets()) == 1

    def test_remove_watch(self):
        """Removing a watch deletes the WatchedMarket."""
        config = make_config()
        tracker = PriceTracker(config)
        event = make_event()

        tracker.add_watch(event, event.outcomes[0])
        tracker.remove_watch("token_yes")

        assert "token_yes" not in tracker.get_watched_markets()

    def test_sample_price(self):
        """Sampling a price adds to history."""
        config = make_config()
        tracker = PriceTracker(config)
        event = make_event()

        tracker.add_watch(event, event.outcomes[0])
        tracker.sample_price("token_yes", best_bid=0.06, best_ask=0.08)

        market = tracker.get_watched_markets()["token_yes"]
        assert len(market.history) == 1
        assert market.history[0].mid_price == pytest.approx(0.07)

    def test_sample_price_ask_only(self):
        """Sampling with only ask uses ask as mid-price."""
        config = make_config()
        tracker = PriceTracker(config)
        event = make_event()

        tracker.add_watch(event, event.outcomes[0])
        tracker.sample_price("token_yes", best_bid=None, best_ask=0.10)

        market = tracker.get_watched_markets()["token_yes"]
        assert market.history[0].mid_price == pytest.approx(0.10)

    def test_sample_price_rate_limiting(self):
        """Sampling is rate-limited by min_sample_interval_seconds."""
        config = make_config(min_sample_interval_seconds=10.0)
        tracker = PriceTracker(config)
        event = make_event()

        tracker.add_watch(event, event.outcomes[0])

        # First sample should succeed
        tracker.sample_price("token_yes", best_bid=0.06, best_ask=0.08)
        # Second immediate sample should be rate-limited
        tracker.sample_price("token_yes", best_bid=0.10, best_ask=0.12)

        market = tracker.get_watched_markets()["token_yes"]
        assert len(market.history) == 1  # Only first sample

    def test_get_price_change(self):
        """get_price_change returns correct change over window."""
        config = make_config()
        tracker = PriceTracker(config)
        event = make_event()

        tracker.add_watch(event, event.outcomes[0])
        market = tracker.get_watched_markets()["token_yes"]

        # Inject synthetic history: 7c -> 19.5c over 30 minutes
        now = datetime.utcnow()
        inject_history(market, make_price_history(
            prices=[0.07, 0.08, 0.10, 0.12, 0.15, 0.195],
            interval_seconds=360,  # ~6 min apart
            start_time=now - timedelta(minutes=30),
        ))

        # Check 30-minute window
        result = tracker.get_price_change("token_yes", 1800)  # 30 min

        assert result is not None
        price_before, price_now, pct_change = result
        assert price_before == pytest.approx(0.07)
        assert price_now == pytest.approx(0.195)
        assert pct_change == pytest.approx((0.195 - 0.07) / 0.07, rel=0.01)

    def test_get_price_change_insufficient_data(self):
        """Returns None with less than 2 data points."""
        config = make_config()
        tracker = PriceTracker(config)
        event = make_event()

        tracker.add_watch(event, event.outcomes[0])

        result = tracker.get_price_change("token_yes", 3600)
        assert result is None

    def test_get_price_change_unknown_token(self):
        """Returns None for unknown token."""
        config = make_config()
        tracker = PriceTracker(config)

        result = tracker.get_price_change("unknown_token", 3600)
        assert result is None

    def test_get_abs_change(self):
        """get_abs_change returns absolute price difference."""
        config = make_config()
        tracker = PriceTracker(config)
        event = make_event()

        tracker.add_watch(event, event.outcomes[0])
        market = tracker.get_watched_markets()["token_yes"]

        now = datetime.utcnow()
        inject_history(market, make_price_history(
            prices=[0.07, 0.195],
            interval_seconds=1800,
            start_time=now - timedelta(minutes=30),
        ))

        result = tracker.get_abs_change("token_yes", 3600)

        assert result is not None
        price_before, price_now, abs_change = result
        assert abs_change == pytest.approx(0.125, abs=0.001)

    def test_get_stats(self):
        """get_stats returns correct summary."""
        config = make_config()
        tracker = PriceTracker(config)
        event = make_event()

        tracker.add_watch(event, event.outcomes[0])
        tracker.sample_price("token_yes", best_bid=0.06, best_ask=0.08)

        stats = tracker.get_stats()
        assert stats["markets_watched"] == 1
        assert stats["markets_with_data"] == 1
        assert stats["total_snapshots"] == 1


# ──────────────────────────────────────────────────────────────
# AnomalyDetector Tests
# ──────────────────────────────────────────────────────────────

class TestAnomalyDetector:
    """Test anomaly detection with synthetic price patterns."""

    def _setup_iran_pattern(self, config=None):
        """
        Set up the Iran strike market pattern:
        7c -> 19.5c (+179%) in ~35 minutes at 2:15 AM PST (10:15 UTC).
        """
        if config is None:
            config = make_config()
        tracker = PriceTracker(config)
        detector = AnomalyDetector(config)
        event = make_event()

        tracker.add_watch(event, event.outcomes[0])
        market = tracker.get_watched_markets()["token_yes"]

        # Inject the Iran pattern
        # Off-hours UTC time: 10:15 UTC = 2:15 AM PST
        base_time = datetime(2026, 2, 27, 9, 40, 0)  # Start at 9:40 UTC
        history = deque([
            PriceSnapshot(timestamp=base_time, mid_price=0.07, source="websocket"),
            PriceSnapshot(timestamp=base_time + timedelta(minutes=5), mid_price=0.07, source="websocket"),
            PriceSnapshot(timestamp=base_time + timedelta(minutes=10), mid_price=0.075, source="websocket"),
            PriceSnapshot(timestamp=base_time + timedelta(minutes=15), mid_price=0.09, source="websocket"),
            PriceSnapshot(timestamp=base_time + timedelta(minutes=20), mid_price=0.12, source="websocket"),
            PriceSnapshot(timestamp=base_time + timedelta(minutes=25), mid_price=0.15, source="websocket"),
            PriceSnapshot(timestamp=base_time + timedelta(minutes=30), mid_price=0.175, source="websocket"),
            PriceSnapshot(timestamp=base_time + timedelta(minutes=35), mid_price=0.195, source="websocket"),
        ])
        inject_history(market, history)

        return tracker, detector

    def test_iran_pattern_triggers_alert(self):
        """The Iran strike pattern should trigger a high-suspicion alert."""
        tracker, detector = self._setup_iran_pattern()

        alert = detector.check_market("token_yes", tracker)

        assert alert is not None
        assert alert.pct_change > 1.0  # >100% change
        assert alert.abs_change > 0.10  # >10c absolute
        assert alert.suspicion_score >= 5.0  # High suspicion

    def test_iran_pattern_off_hours_bonus(self):
        """Off-hours detection adds to suspicion score."""
        config = make_config(off_hours_utc=(9, 11))  # 9-11 UTC covers our test time
        tracker, detector = self._setup_iran_pattern(config)

        alert = detector.check_market("token_yes", tracker)

        assert alert is not None
        assert alert.is_off_hours is True
        assert alert.suspicion_score >= 7.0  # Off-hours + magnitude + speed + low-baseline

    def test_relative_threshold_50pct_1h(self):
        """50% move in 1 hour triggers alert."""
        config = make_config()
        tracker = PriceTracker(config)
        detector = AnomalyDetector(config)
        event = make_event()

        tracker.add_watch(event, event.outcomes[0])
        market = tracker.get_watched_markets()["token_yes"]

        now = datetime.utcnow()
        inject_history(market, make_price_history(
            prices=[0.20, 0.31],  # 55% increase
            interval_seconds=1800,
            start_time=now - timedelta(minutes=30),
        ))

        alert = detector.check_market("token_yes", tracker)

        assert alert is not None
        assert alert.pct_change >= 0.50

    def test_absolute_threshold_10c_1h(self):
        """10c move in 1 hour triggers alert."""
        config = make_config()
        tracker = PriceTracker(config)
        detector = AnomalyDetector(config)
        event = make_event()

        tracker.add_watch(event, event.outcomes[0])
        market = tracker.get_watched_markets()["token_yes"]

        now = datetime.utcnow()
        inject_history(market, make_price_history(
            prices=[0.05, 0.16],  # 11c move, but 220% relative
            interval_seconds=1800,
            start_time=now - timedelta(minutes=30),
        ))

        alert = detector.check_market("token_yes", tracker)
        assert alert is not None
        assert alert.abs_change >= 0.10

    def test_no_alert_small_move(self):
        """Small price moves should not trigger alerts."""
        config = make_config()
        tracker = PriceTracker(config)
        detector = AnomalyDetector(config)
        event = make_event()

        tracker.add_watch(event, event.outcomes[0])
        market = tracker.get_watched_markets()["token_yes"]

        now = datetime.utcnow()
        inject_history(market, make_price_history(
            prices=[0.50, 0.52],  # Only 4% move, 2c absolute
            interval_seconds=1800,
            start_time=now - timedelta(minutes=30),
        ))

        alert = detector.check_market("token_yes", tracker)
        assert alert is None

    def test_downward_move_triggers_sell_side_alert(self):
        """Large downward price moves now trigger sell-side alerts."""
        config = make_config()
        tracker = PriceTracker(config)
        detector = AnomalyDetector(config)
        event = make_event()

        tracker.add_watch(event, event.outcomes[0])
        market = tracker.get_watched_markets()["token_yes"]

        now = datetime.utcnow()
        inject_history(market, make_price_history(
            prices=[0.50, 0.20],  # Big drop — sell-side insider trading signal
            interval_seconds=1800,
            start_time=now - timedelta(minutes=30),
        ))

        alert = detector.check_market("token_yes", tracker)
        assert alert is not None
        assert alert.direction == "down"
        assert alert.pct_change > 0.50  # Magnitude is positive

    def test_cooldown_dedup(self):
        """Second alert within cooldown window should be suppressed."""
        config = make_config(alert_cooldown_seconds=300)
        tracker = PriceTracker(config)
        detector = AnomalyDetector(config)
        event = make_event()

        tracker.add_watch(event, event.outcomes[0])
        market = tracker.get_watched_markets()["token_yes"]

        now = datetime.utcnow()
        inject_history(market, make_price_history(
            prices=[0.07, 0.195],
            interval_seconds=1800,
            start_time=now - timedelta(minutes=30),
        ))

        # First alert should fire
        alert1 = detector.check_market("token_yes", tracker)
        assert alert1 is not None

        # Second check within cooldown should be suppressed
        alert2 = detector.check_market("token_yes", tracker)
        assert alert2 is None

    def test_check_all_markets(self):
        """check_all_markets returns alerts for all matching markets."""
        config = make_config()
        tracker = PriceTracker(config)
        detector = AnomalyDetector(config)

        event = make_event()
        tracker.add_watch(event, event.outcomes[0])
        tracker.add_watch(event, event.outcomes[1])

        now = datetime.utcnow()

        # Token 1: spike
        m1 = tracker.get_watched_markets()["token_yes"]
        inject_history(m1, make_price_history(
            prices=[0.07, 0.195],
            interval_seconds=1800,
            start_time=now - timedelta(minutes=30),
        ))

        # Token 2: stable
        m2 = tracker.get_watched_markets()["token_no"]
        inject_history(m2, make_price_history(
            prices=[0.92, 0.92],
            interval_seconds=1800,
            start_time=now - timedelta(minutes=30),
        ))

        alerts = detector.check_all_markets(tracker)
        assert len(alerts) == 1
        assert alerts[0].token_id == "token_yes"


# ──────────────────────────────────────────────────────────────
# Suspicion Scoring Tests
# ──────────────────────────────────────────────────────────────

class TestSuspicionScoring:
    """Test the suspicion score computation."""

    def test_max_score_iran_pattern(self):
        """Iran-like pattern during off-hours scores very high."""
        config = make_config(off_hours_utc=(9, 11))
        detector = AnomalyDetector(config)

        score = detector._compute_suspicion_score(
            pct_change=1.79,        # 179%
            abs_change=0.125,       # 12.5c
            window_seconds=2100,    # 35 min
            price_before=0.07,      # 7c baseline
            now=datetime(2026, 2, 27, 10, 15),  # 10:15 UTC (off-hours)
            price_after=0.195,      # 19.5c — not near resolution
        )

        # Should score high: magnitude(3) + speed(2) + off_hours(2) + low_baseline(1) = 8
        assert score >= 7.0
        assert score <= 10.0

    def test_moderate_move_normal_hours(self):
        """Moderate move during normal hours scores lower."""
        config = make_config()
        detector = AnomalyDetector(config)

        score = detector._compute_suspicion_score(
            pct_change=0.60,        # 60%
            abs_change=0.12,        # 12c
            window_seconds=3600,    # 1 hour
            price_before=0.20,      # 20c baseline
            now=datetime(2026, 2, 27, 18, 0),  # 6 PM UTC (normal hours)
            price_after=0.32,       # 32c — not near resolution
        )

        # magnitude(~1.2) + speed(1.5) + off_hours(0) + low_baseline(0.5) = ~3.2
        assert 2.0 <= score <= 5.0

    def test_slow_large_move(self):
        """Large move over 24h scores moderate."""
        config = make_config()
        detector = AnomalyDetector(config)

        score = detector._compute_suspicion_score(
            pct_change=2.0,         # 200%
            abs_change=0.20,        # 20c
            window_seconds=86400,   # 24 hours
            price_before=0.10,      # 10c
            now=datetime(2026, 2, 27, 15, 0),
            price_after=0.30,       # 30c — not near resolution
        )

        # magnitude(3) + speed(0.5) + off_hours(0) + low_baseline(1.0) = 4.5
        assert 3.0 <= score <= 6.0

    def test_near_resolution_penalty(self):
        """Price landing near resolution gets a -3 score penalty."""
        config = make_config()
        detector = AnomalyDetector(config)

        score_normal = detector._compute_suspicion_score(
            pct_change=1.0, abs_change=0.15,
            window_seconds=3600, price_before=0.15,
            now=datetime(2026, 1, 1, 15, 0), price_after=0.30,
        )
        score_resolution = detector._compute_suspicion_score(
            pct_change=1.0, abs_change=0.15,
            window_seconds=3600, price_before=0.15,
            now=datetime(2026, 1, 1, 15, 0), price_after=0.96,
        )

        assert score_normal - score_resolution == pytest.approx(3.0)

    def test_off_hours_detection(self):
        """Off-hours flag works correctly."""
        config = make_config(off_hours_utc=(7, 11))
        detector = AnomalyDetector(config)

        assert detector._is_off_hours(datetime(2026, 1, 1, 8, 0)) is True
        assert detector._is_off_hours(datetime(2026, 1, 1, 10, 59)) is True
        assert detector._is_off_hours(datetime(2026, 1, 1, 11, 0)) is False
        assert detector._is_off_hours(datetime(2026, 1, 1, 6, 59)) is False
        assert detector._is_off_hours(datetime(2026, 1, 1, 15, 0)) is False

    def test_off_hours_wrap_midnight(self):
        """Off-hours wrapping past midnight."""
        config = make_config(off_hours_utc=(22, 6))  # 10 PM - 6 AM UTC
        detector = AnomalyDetector(config)

        assert detector._is_off_hours(datetime(2026, 1, 1, 23, 0)) is True
        assert detector._is_off_hours(datetime(2026, 1, 1, 3, 0)) is True
        assert detector._is_off_hours(datetime(2026, 1, 1, 6, 0)) is False
        assert detector._is_off_hours(datetime(2026, 1, 1, 15, 0)) is False


# ──────────────────────────────────────────────────────────────
# NewsChecker Tests
# ──────────────────────────────────────────────────────────────

class TestNewsChecker:
    """Test news headline fetching with mocked RSS."""

    @staticmethod
    def _make_sample_rss():
        """Generate RSS with dates relative to now so tests don't go stale."""
        now = datetime.utcnow()
        recent_1 = (now - timedelta(minutes=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        recent_2 = (now - timedelta(hours=1)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        old = (now - timedelta(days=7)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        return f"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>Iran strike - Google News</title>
        <item>
          <title>US strikes Iran nuclear facilities in major escalation</title>
          <pubDate>{recent_1}</pubDate>
        </item>
        <item>
          <title>Iran strikes back after US military action</title>
          <pubDate>{recent_2}</pubDate>
        </item>
        <item>
          <title>Old article from last week about Iran</title>
          <pubDate>{old}</pubDate>
        </item>
      </channel>
    </rss>"""

    @staticmethod
    def _make_rss_with_dateless():
        """RSS with one dated and one dateless item."""
        now = datetime.utcnow()
        recent = (now - timedelta(minutes=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        return f"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <item>
          <title>US strikes Iran nuclear facilities</title>
          <pubDate>{recent}</pubDate>
        </item>
        <item>
          <title>Iran conflict update with no date</title>
        </item>
      </channel>
    </rss>"""

    @staticmethod
    def _make_rss_with_post_move():
        """RSS with items before and after a known move time."""
        now = datetime.utcnow()
        before_move = (now - timedelta(hours=2)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        after_move = (now - timedelta(minutes=5)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        return f"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <item>
          <title>US strikes Iran nuclear facilities in escalation</title>
          <pubDate>{before_move}</pubDate>
        </item>
        <item>
          <title>Iran strikes back after US military action today</title>
          <pubDate>{after_move}</pubDate>
        </item>
      </channel>
    </rss>"""

    def test_extract_keywords(self):
        """Keywords are extracted and prioritized correctly."""
        config = make_config()
        checker = NewsChecker(config)

        keywords = checker._extract_keywords("US Strikes Iran by Feb 28")

        # "strike" and "attack" keywords should be prioritized
        assert "strikes" in keywords or "strike" in [k.lower() for k in config.watch_keywords]
        assert "iran" in keywords
        assert "feb" in keywords
        # Stopwords filtered
        assert "by" not in keywords
        assert "us" not in keywords  # 2-char word filtered

    def test_parse_rss(self):
        """RSS parsing extracts recent headlines, filters old ones."""
        config = make_config(news_lookback_hours=6)
        checker = NewsChecker(config)

        headlines = checker._parse_rss(self._make_sample_rss())

        # Should get 2 recent articles, old one filtered by date
        assert len(headlines) == 2
        # Headlines are now NewsHeadline objects
        assert all(isinstance(h, NewsHeadline) for h in headlines)
        assert any("strikes" in h.title.lower() for h in headlines)
        # Each headline should have a timestamp
        assert all(h.published_at is not None for h in headlines)

    def test_parse_rss_rejects_dateless(self):
        """Headlines without pubDate are rejected."""
        config = make_config(news_lookback_hours=6)
        checker = NewsChecker(config)

        headlines = checker._parse_rss(self._make_rss_with_dateless())

        # Only the dated item should survive
        assert len(headlines) == 1
        assert "nuclear" in headlines[0].title.lower()

    def test_parse_rss_move_started_at_filter(self):
        """Headlines published after price move are filtered out."""
        now = datetime.utcnow()
        config = make_config(news_lookback_hours=6)
        checker = NewsChecker(config)

        # Move started 1 hour ago — headline from 2h ago is OK,
        # headline from 5min ago is after the move and should be filtered
        move_started_at = now - timedelta(hours=1)

        headlines = checker._parse_rss(
            self._make_rss_with_post_move(),
            move_started_at=move_started_at,
        )

        # Only the pre-move headline should survive
        assert len(headlines) == 1
        assert "escalation" in headlines[0].title.lower()

    def test_parse_rss_move_grace_window(self):
        """Headlines within grace window after move start are still included."""
        now = datetime.utcnow()
        config = make_config(news_lookback_hours=6)
        checker = NewsChecker(config)

        # Make RSS with an item published 10 min after the move started
        # (within the 15-min grace window)
        move_time = now - timedelta(minutes=30)
        headline_time = (move_time + timedelta(minutes=10)).strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )
        rss = f"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <item>
          <title>US strikes Iran nuclear facilities in escalation</title>
          <pubDate>{headline_time}</pubDate>
        </item>
      </channel>
    </rss>"""

        headlines = checker._parse_rss(rss, move_started_at=move_time)

        # Within grace window — should be included
        assert len(headlines) == 1

    def test_parse_rss_empty(self):
        """Empty RSS returns empty list."""
        config = make_config()
        checker = NewsChecker(config)

        headlines = checker._parse_rss("<rss><channel></channel></rss>")
        assert headlines == []

    def test_parse_rss_invalid_xml(self):
        """Invalid XML returns empty list."""
        config = make_config()
        checker = NewsChecker(config)

        headlines = checker._parse_rss("not xml at all")
        assert headlines == []

    def test_parse_rss_date_format(self):
        """RSS date parsing handles RFC 822 format."""
        config = make_config()
        checker = NewsChecker(config)

        dt = checker._parse_rss_date("Wed, 27 Feb 2026 10:15:00 GMT")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 2
        assert dt.day == 27
        assert dt.hour == 10

    @pytest.mark.asyncio
    async def test_fetch_headlines_mocked(self):
        """fetch_headlines with mocked HTTP response returns NewsHeadline objects."""
        config = make_config(news_lookback_hours=6)
        checker = NewsChecker(config)

        # Mock the HTTP client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = self._make_sample_rss()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        checker._http_client = mock_client

        headlines = await checker.fetch_headlines("US Strikes Iran by Feb 28")

        assert len(headlines) >= 1
        assert all(isinstance(h, NewsHeadline) for h in headlines)
        assert all(h.published_at is not None for h in headlines)
        mock_client.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_fetch_headlines_with_move_started_at(self):
        """fetch_headlines passes move_started_at to RSS parser."""
        now = datetime.utcnow()
        config = make_config(news_lookback_hours=6)
        checker = NewsChecker(config)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = self._make_rss_with_post_move()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        checker._http_client = mock_client

        # Move started 1h ago — post-move headline should be filtered
        move_started_at = now - timedelta(hours=1)
        headlines = await checker.fetch_headlines(
            "US Strikes Iran by Feb 28",
            move_started_at=move_started_at,
        )

        # Only pre-move headline should survive (if it passes keyword filter)
        for h in headlines:
            assert h.published_at <= move_started_at + timedelta(minutes=15)


# ──────────────────────────────────────────────────────────────
# NewsHeadline Tests
# ──────────────────────────────────────────────────────────────

class TestNewsHeadline:
    """Test NewsHeadline model."""

    def test_to_dict(self):
        """to_dict includes title and published_at ISO string."""
        h = NewsHeadline(title="Breaking news", published_at=datetime(2026, 4, 2, 12, 30, 0))
        d = h.to_dict()
        assert d["title"] == "Breaking news"
        assert d["published_at"] == "2026-04-02T12:30:00"

    def test_to_dict_no_date(self):
        """to_dict handles None published_at."""
        h = NewsHeadline(title="Breaking news")
        d = h.to_dict()
        assert d["published_at"] is None

    def test_age_minutes(self):
        """age_minutes computes correctly."""
        h = NewsHeadline(
            title="Test",
            published_at=datetime.utcnow() - timedelta(minutes=45),
        )
        age = h.age_minutes
        assert age is not None
        assert 44 < age < 46

    def test_age_minutes_none(self):
        """age_minutes is None when no published_at."""
        h = NewsHeadline(title="Test")
        assert h.age_minutes is None


# ──────────────────────────────────────────────────────────────
# AnomalyAlert Tests
# ──────────────────────────────────────────────────────────────

class TestAnomalyAlert:
    """Test AnomalyAlert serialization."""

    def test_to_dict(self):
        """to_dict produces valid JSON-serializable dict."""
        alert = AnomalyAlert(
            alert_id="alert_abc123",
            event_id="e1",
            event_title="US Strikes Iran",
            event_slug="us-strikes-iran",
            outcome_name="Yes",
            token_id="token123",
            price_before=0.07,
            price_after=0.195,
            pct_change=1.7857,
            abs_change=0.125,
            window_seconds=2100,
            threshold_type="relative",
            suspicion_score=7.5,
            is_off_hours=True,
            event_volume_24h=89600000.0,
            news_headlines=[NewsHeadline(title="Iran tensions rise", published_at=datetime.utcnow())],
            news_driven=True,
        )

        d = alert.to_dict()

        assert d["alert_id"] == "alert_abc123"
        assert d["pct_change"] == 1.7857
        assert d["suspicion_score"] == 7.5
        assert d["is_off_hours"] is True
        assert len(d["news_headlines"]) == 1
        assert d["news_headlines"][0]["title"] == "Iran tensions rise"
        assert d["news_headlines"][0]["published_at"] is not None
        assert d["news_driven"] is True
        assert "detected_at" in d

        # Should be JSON-serializable
        import json
        json_str = json.dumps(d)
        assert "alert_abc123" in json_str

    def test_news_driven_true_when_headlines(self):
        """news_driven=True when headlines are present."""
        alert = AnomalyAlert(
            alert_id="a1", event_id="e1", event_title="Test",
            event_slug="test", outcome_name="Yes", token_id="t1",
            price_before=0.07, price_after=0.195, pct_change=1.79,
            abs_change=0.125, window_seconds=2100, threshold_type="relative",
            suspicion_score=7.5, is_off_hours=False, event_volume_24h=50000,
            news_headlines=[NewsHeadline(title="Breaking: event happened", published_at=datetime.utcnow())],
            news_driven=True,
        )

        assert alert.news_driven is True
        assert alert.to_dict()["news_driven"] is True

    def test_news_driven_false_when_no_headlines(self):
        """news_driven=False when no headlines."""
        alert = AnomalyAlert(
            alert_id="a2", event_id="e1", event_title="Test",
            event_slug="test", outcome_name="Yes", token_id="t1",
            price_before=0.07, price_after=0.195, pct_change=1.79,
            abs_change=0.125, window_seconds=2100, threshold_type="relative",
            suspicion_score=7.5, is_off_hours=False, event_volume_24h=50000,
        )

        assert alert.news_driven is False
        assert alert.to_dict()["news_driven"] is False

    def test_news_driven_default_false(self):
        """news_driven defaults to False."""
        alert = AnomalyAlert(
            alert_id="a3", event_id="e1", event_title="Test",
            event_slug="test", outcome_name="Yes", token_id="t1",
            price_before=0.10, price_after=0.20, pct_change=1.0,
            abs_change=0.10, window_seconds=3600, threshold_type="absolute",
            suspicion_score=5.0, is_off_hours=False, event_volume_24h=10000,
        )

        assert alert.news_driven is False


# ──────────────────────────────────────────────────────────────
# WatchedMarket Tests
# ──────────────────────────────────────────────────────────────

class TestWatchedMarket:
    """Test WatchedMarket properties."""

    def test_current_price(self):
        """current_price returns latest mid-price."""
        market = WatchedMarket(
            token_id="t1", event_id="e1", outcome_name="Yes",
            event_title="Test", event_slug="test", event_volume_24h=1000,
        )
        market.history.append(PriceSnapshot(
            timestamp=datetime.utcnow(), mid_price=0.50,
        ))
        assert market.current_price == pytest.approx(0.50)

    def test_current_price_empty(self):
        """current_price returns None when empty."""
        market = WatchedMarket(
            token_id="t1", event_id="e1", outcome_name="Yes",
            event_title="Test", event_slug="test", event_volume_24h=1000,
        )
        assert market.current_price is None


# ──────────────────────────────────────────────────────────────
# Gap-Aware Price Change Tests
# ──────────────────────────────────────────────────────────────

class TestGapAwarePriceChange:
    """Test that price change detection handles data gaps correctly."""

    def test_gap_detects_spike(self):
        """A 12-hour data gap followed by a price jump is detected, not invisible."""
        config = make_config()
        tracker = PriceTracker(config)
        event = make_event()
        tracker.add_watch(event, event.outcomes[0])
        market = tracker.get_watched_markets()["token_yes"]

        now = datetime.utcnow()
        # Simulate: price at 0.225 twelve hours ago, then 0.335 now (no data in between)
        history = deque([
            PriceSnapshot(timestamp=now - timedelta(hours=12), mid_price=0.225, source="websocket"),
            PriceSnapshot(timestamp=now, mid_price=0.335, source="websocket"),
        ])
        inject_history(market, history)

        # 1-hour window: should still detect the 11c move by using the 12h-old baseline
        result = tracker.get_price_change("token_yes", 3600)
        assert result is not None
        price_before, price_now, pct_change = result
        assert price_before == pytest.approx(0.225)
        assert price_now == pytest.approx(0.335)
        assert pct_change == pytest.approx((0.335 - 0.225) / 0.225, rel=0.01)

    def test_gap_abs_change_detected(self):
        """Absolute change through a data gap is correctly reported."""
        config = make_config()
        tracker = PriceTracker(config)
        event = make_event()
        tracker.add_watch(event, event.outcomes[0])
        market = tracker.get_watched_markets()["token_yes"]

        now = datetime.utcnow()
        history = deque([
            PriceSnapshot(timestamp=now - timedelta(hours=6), mid_price=0.10, source="websocket"),
            PriceSnapshot(timestamp=now, mid_price=0.22, source="websocket"),
        ])
        inject_history(market, history)

        result = tracker.get_abs_change("token_yes", 3600)
        assert result is not None
        _, _, abs_change = result
        assert abs_change == pytest.approx(0.12, abs=0.001)

    def test_no_gap_still_works(self):
        """Normal continuous data still picks the correct baseline."""
        config = make_config()
        tracker = PriceTracker(config)
        event = make_event()
        tracker.add_watch(event, event.outcomes[0])
        market = tracker.get_watched_markets()["token_yes"]

        now = datetime.utcnow()
        # 5 data points, 10 min apart, spanning 40 min
        history = make_price_history(
            prices=[0.20, 0.21, 0.22, 0.23, 0.30],
            interval_seconds=600,
            start_time=now - timedelta(minutes=40),
        )
        inject_history(market, history)

        # 30-min window: baseline should be the snapshot at/before cutoff (now-30min)
        result = tracker.get_price_change("token_yes", 1800)
        assert result is not None
        price_before, price_now, _ = result
        # Cutoff = now - 30min. Snapshot at -40min is before cutoff, -30min is at cutoff.
        # The snapshot at -30min (0.21) should be the baseline.
        assert price_before == pytest.approx(0.21, abs=0.01)
        assert price_now == pytest.approx(0.30)

    def test_gap_with_only_recent_data(self):
        """When all data is within the window, uses oldest as baseline."""
        config = make_config()
        tracker = PriceTracker(config)
        event = make_event()
        tracker.add_watch(event, event.outcomes[0])
        market = tracker.get_watched_markets()["token_yes"]

        now = datetime.utcnow()
        # Both snapshots within the last 10 minutes, window is 1 hour
        history = deque([
            PriceSnapshot(timestamp=now - timedelta(minutes=5), mid_price=0.15, source="websocket"),
            PriceSnapshot(timestamp=now, mid_price=0.25, source="websocket"),
        ])
        inject_history(market, history)

        result = tracker.get_price_change("token_yes", 3600)
        assert result is not None
        price_before, price_now, _ = result
        assert price_before == pytest.approx(0.15)
        assert price_now == pytest.approx(0.25)


# ──────────────────────────────────────────────────────────────
# Cross-Market Correlation Tests
# ──────────────────────────────────────────────────────────────

class TestCorrelatedMarkets:
    """Test cross-market correlation detection."""

    def _make_multi_outcome_event(self, event_id="ceasefire1",
                                   title="US x Iran ceasefire by",
                                   n_outcomes=5) -> NegriskEvent:
        """Create a multi-outcome event with N date outcomes."""
        outcomes = []
        for i in range(n_outcomes):
            outcomes.append(Outcome(
                outcome_id=f"o{i}",
                market_id=f"m{i}",
                condition_id=f"c{i}",
                token_id=f"token_{event_id}_{i}",
                name=f"Outcome {i}",
                status=OutcomeStatus.ACTIVE,
                bba=OutcomeBBA(best_bid=0.10, best_ask=0.12),
            ))
        return NegriskEvent(
            event_id=event_id,
            slug="ceasefire-by",
            title=title,
            condition_id="cond_ceasefire",
            outcomes=outcomes,
            volume_24h=100000.0,
        )

    def test_correlated_up_moves_detected(self):
        """3+ outcomes moving up together triggers a correlated alert."""
        config = make_config()
        tracker = PriceTracker(config)
        detector = AnomalyDetector(config)

        event = self._make_multi_outcome_event(n_outcomes=5)
        for outcome in event.outcomes:
            tracker.add_watch(event, outcome)

        now = datetime.utcnow()
        # All 5 outcomes move up by 3-5c in last hour
        for i, outcome in enumerate(event.outcomes):
            market = tracker.get_watched_markets()[outcome.token_id]
            base_price = 0.15 + i * 0.05  # 15c, 20c, 25c, 30c, 35c
            move = 0.03 + i * 0.005       # 3c, 3.5c, 4c, 4.5c, 5c
            history = deque([
                PriceSnapshot(timestamp=now - timedelta(hours=2), mid_price=base_price, source="websocket"),
                PriceSnapshot(timestamp=now, mid_price=base_price + move, source="websocket"),
            ])
            inject_history(market, history)

        alerts = detector.check_correlated_markets(tracker)
        assert len(alerts) == 1

        alert = alerts[0]
        assert alert.threshold_type == "correlated"
        assert alert.correlated_outcomes == 5
        assert alert.direction == "up"
        assert alert.suspicion_score > 0

    def test_correlated_down_moves_detected(self):
        """3+ outcomes moving down together triggers a sell-side correlated alert."""
        config = make_config()
        tracker = PriceTracker(config)
        detector = AnomalyDetector(config)

        event = self._make_multi_outcome_event(n_outcomes=4)
        for outcome in event.outcomes:
            tracker.add_watch(event, outcome)

        now = datetime.utcnow()
        for i, outcome in enumerate(event.outcomes):
            market = tracker.get_watched_markets()[outcome.token_id]
            history = deque([
                PriceSnapshot(timestamp=now - timedelta(hours=1), mid_price=0.30, source="websocket"),
                PriceSnapshot(timestamp=now, mid_price=0.25, source="websocket"),
            ])
            inject_history(market, history)

        alerts = detector.check_correlated_markets(tracker)
        assert len(alerts) == 1
        assert alerts[0].direction == "down"
        assert alerts[0].correlated_outcomes == 4

    def test_too_few_movers_no_alert(self):
        """Only 2 outcomes moving (below min_correlated=3) produces no alert."""
        config = make_config()
        tracker = PriceTracker(config)
        detector = AnomalyDetector(config)

        event = self._make_multi_outcome_event(n_outcomes=5)
        for outcome in event.outcomes:
            tracker.add_watch(event, outcome)

        now = datetime.utcnow()
        for i, outcome in enumerate(event.outcomes):
            market = tracker.get_watched_markets()[outcome.token_id]
            # Only 2 of 5 move significantly
            if i < 2:
                prices = [0.20, 0.25]  # +5c
            else:
                prices = [0.20, 0.205]  # +0.5c (below 2c threshold)
            history = deque([
                PriceSnapshot(timestamp=now - timedelta(hours=1), mid_price=prices[0], source="websocket"),
                PriceSnapshot(timestamp=now, mid_price=prices[1], source="websocket"),
            ])
            inject_history(market, history)

        alerts = detector.check_correlated_markets(tracker)
        assert len(alerts) == 0

    def test_mixed_directions_no_alert(self):
        """3 outcomes moving up but 3 down does not trigger if neither group hits min."""
        config = make_config()
        tracker = PriceTracker(config)
        detector = AnomalyDetector(config)

        event = self._make_multi_outcome_event(n_outcomes=4)
        for outcome in event.outcomes:
            tracker.add_watch(event, outcome)

        now = datetime.utcnow()
        # 2 up, 2 down — neither group >= 3
        directions = [+0.04, +0.03, -0.04, -0.03]
        for i, outcome in enumerate(event.outcomes):
            market = tracker.get_watched_markets()[outcome.token_id]
            history = deque([
                PriceSnapshot(timestamp=now - timedelta(hours=1), mid_price=0.30, source="websocket"),
                PriceSnapshot(timestamp=now, mid_price=0.30 + directions[i], source="websocket"),
            ])
            inject_history(market, history)

        alerts = detector.check_correlated_markets(tracker)
        assert len(alerts) == 0

    def test_correlated_excludes_already_alerted(self):
        """Tokens in exclude_tokens are skipped."""
        config = make_config()
        tracker = PriceTracker(config)
        detector = AnomalyDetector(config)

        event = self._make_multi_outcome_event(n_outcomes=4)
        for outcome in event.outcomes:
            tracker.add_watch(event, outcome)

        now = datetime.utcnow()
        for outcome in event.outcomes:
            market = tracker.get_watched_markets()[outcome.token_id]
            history = deque([
                PriceSnapshot(timestamp=now - timedelta(hours=1), mid_price=0.20, source="websocket"),
                PriceSnapshot(timestamp=now, mid_price=0.25, source="websocket"),
            ])
            inject_history(market, history)

        # Exclude 2 tokens — only 2 remain, below min_correlated=3
        exclude = {event.outcomes[0].token_id, event.outcomes[1].token_id}
        alerts = detector.check_correlated_markets(tracker, exclude_tokens=exclude)
        assert len(alerts) == 0

    def test_correlation_bonus_increases_score(self):
        """More correlated outcomes = higher suspicion score via bonus."""
        config = make_config()
        tracker = PriceTracker(config)

        now = datetime.utcnow()

        # Test with 3 outcomes (minimum)
        detector3 = AnomalyDetector(config)
        event3 = self._make_multi_outcome_event(event_id="e3", n_outcomes=3)
        for outcome in event3.outcomes:
            tracker.add_watch(event3, outcome)
            market = tracker.get_watched_markets()[outcome.token_id]
            history = deque([
                PriceSnapshot(timestamp=now - timedelta(hours=1), mid_price=0.20, source="websocket"),
                PriceSnapshot(timestamp=now, mid_price=0.25, source="websocket"),
            ])
            inject_history(market, history)
        alerts3 = detector3.check_correlated_markets(tracker)

        # Test with 6 outcomes (larger group) — use separate tracker to isolate
        tracker6 = PriceTracker(config)
        detector6 = AnomalyDetector(config)
        event6 = self._make_multi_outcome_event(event_id="e6", n_outcomes=6)
        for outcome in event6.outcomes:
            tracker6.add_watch(event6, outcome)
            market = tracker6.get_watched_markets()[outcome.token_id]
            history = deque([
                PriceSnapshot(timestamp=now - timedelta(hours=1), mid_price=0.20, source="websocket"),
                PriceSnapshot(timestamp=now, mid_price=0.25, source="websocket"),
            ])
            inject_history(market, history)
        alerts6 = detector6.check_correlated_markets(tracker6)

        assert len(alerts3) == 1 and len(alerts6) == 1
        # 6-outcome correlation should score higher than 3-outcome
        assert alerts6[0].suspicion_score > alerts3[0].suspicion_score


# ──────────────────────────────────────────────────────────────
# Baseline Floor Filtering Tests
# ──────────────────────────────────────────────────────────────

class TestBaselineFloorFiltering:
    """Tests that dead-market baselines (sub-3c) are filtered out."""

    def test_dead_market_wakeup_filtered(self):
        """A move from 0.1c to 50c (esports resolution) should produce no alert."""
        config = make_config()
        detector = AnomalyDetector(config)
        tracker = PriceTracker(config)

        event = make_event()
        outcome = event.outcomes[0]
        tracker.add_watch(event, outcome)

        now = datetime.utcnow()
        market = tracker.get_watched_markets()[outcome.token_id]
        history = deque([
            PriceSnapshot(timestamp=now - timedelta(hours=2), mid_price=0.001, source="websocket"),
            PriceSnapshot(timestamp=now, mid_price=0.50, source="websocket"),
        ])
        inject_history(market, history)

        alert = detector.check_market(outcome.token_id, tracker)
        assert alert is None

    def test_legitimate_low_price_not_filtered(self):
        """A move from 7c to 20c (above floor) should still produce an alert."""
        config = make_config()
        detector = AnomalyDetector(config)
        tracker = PriceTracker(config)

        event = make_event()
        outcome = event.outcomes[0]
        tracker.add_watch(event, outcome)

        now = datetime.utcnow()
        market = tracker.get_watched_markets()[outcome.token_id]
        history = deque([
            PriceSnapshot(timestamp=now - timedelta(minutes=20), mid_price=0.07, source="websocket"),
            PriceSnapshot(timestamp=now, mid_price=0.20, source="websocket"),
        ])
        inject_history(market, history)

        alert = detector.check_market(outcome.token_id, tracker)
        # 7c->20c = +186%, breaches 50%/1h relative threshold
        assert alert is not None
        assert alert.price_before == pytest.approx(0.07)
        assert alert.price_after == pytest.approx(0.20)

    def test_dead_market_filtered_in_correlation(self):
        """Sub-floor baselines should be excluded from correlated mover counts."""
        config = make_config()
        detector = AnomalyDetector(config)
        tracker = PriceTracker(config)

        now = datetime.utcnow()

        # 5 outcomes all moving from 0.001 to 0.50 (dead market wakeup)
        outcomes = []
        for i in range(5):
            outcomes.append(Outcome(
                outcome_id=f"o_{i}", market_id=f"m_{i}", condition_id=f"c_{i}",
                token_id=f"token_dead_{i}", name=f"Outcome {i}",
                status=OutcomeStatus.ACTIVE,
                bba=OutcomeBBA(best_bid=0.001, best_ask=0.50),
            ))
        event = make_event(event_id="dead_event", outcomes=outcomes)

        for outcome in outcomes:
            tracker.add_watch(event, outcome)
            market = tracker.get_watched_markets()[outcome.token_id]
            history = deque([
                PriceSnapshot(timestamp=now - timedelta(hours=1), mid_price=0.001, source="websocket"),
                PriceSnapshot(timestamp=now, mid_price=0.50, source="websocket"),
            ])
            inject_history(market, history)

        alerts = detector.check_correlated_markets(tracker)
        assert len(alerts) == 0

    def test_absolute_threshold_respects_floor(self):
        """Absolute threshold check also skips sub-floor baselines."""
        config = make_config()
        detector = AnomalyDetector(config)
        tracker = PriceTracker(config)

        event = make_event()
        outcome = event.outcomes[0]
        tracker.add_watch(event, outcome)

        now = datetime.utcnow()
        market = tracker.get_watched_markets()[outcome.token_id]
        # 0.02 -> 0.15 = 13c move (breaches 10c/1h absolute threshold)
        # but baseline is below 3c floor
        history = deque([
            PriceSnapshot(timestamp=now - timedelta(minutes=30), mid_price=0.02, source="websocket"),
            PriceSnapshot(timestamp=now, mid_price=0.15, source="websocket"),
        ])
        inject_history(market, history)

        alert = detector.check_market(outcome.token_id, tracker)
        assert alert is None


# ──────────────────────────────────────────────────────────────
# Correlation Re-alert Suppression Tests
# ──────────────────────────────────────────────────────────────

class TestCorrelationRealertSuppression(TestCorrelatedMarkets):
    """Tests that correlated alerts don't re-fire when price hasn't moved."""

    def test_correlated_suppressed_on_second_scan(self):
        """Same correlated pattern should not fire twice if prices haven't moved."""
        config = make_config(alert_cooldown_seconds=0)  # No time cooldown
        detector = AnomalyDetector(config)
        tracker = PriceTracker(config)

        now = datetime.utcnow()
        event = self._make_multi_outcome_event(event_id="esport1", n_outcomes=4)

        for outcome in event.outcomes:
            tracker.add_watch(event, outcome)
            market = tracker.get_watched_markets()[outcome.token_id]
            history = deque([
                PriceSnapshot(timestamp=now - timedelta(hours=1), mid_price=0.20, source="websocket"),
                PriceSnapshot(timestamp=now, mid_price=0.25, source="websocket"),
            ])
            inject_history(market, history)

        # First scan: should fire
        alerts1 = detector.check_correlated_markets(tracker)
        assert len(alerts1) == 1

        # Second scan: same prices, should be suppressed
        alerts2 = detector.check_correlated_markets(tracker)
        assert len(alerts2) == 0

    def test_correlated_refires_on_new_movement(self):
        """Correlated alert should re-fire if prices move further (>2c)."""
        config = make_config(alert_cooldown_seconds=0)
        detector = AnomalyDetector(config)
        tracker = PriceTracker(config)

        now = datetime.utcnow()
        event = self._make_multi_outcome_event(event_id="geo1", n_outcomes=4)

        for outcome in event.outcomes:
            tracker.add_watch(event, outcome)
            market = tracker.get_watched_markets()[outcome.token_id]
            history = deque([
                PriceSnapshot(timestamp=now - timedelta(hours=1), mid_price=0.20, source="websocket"),
                PriceSnapshot(timestamp=now, mid_price=0.25, source="websocket"),
            ])
            inject_history(market, history)

        # First scan: fires
        alerts1 = detector.check_correlated_markets(tracker)
        assert len(alerts1) == 1

        # Prices move further (+5c beyond alerted level)
        for outcome in event.outcomes:
            market = tracker.get_watched_markets()[outcome.token_id]
            market.live_history.append(
                PriceSnapshot(timestamp=now + timedelta(minutes=5), mid_price=0.31, source="websocket")
            )
            market.history.append(
                PriceSnapshot(timestamp=now + timedelta(minutes=5), mid_price=0.31, source="websocket")
            )

        # Second scan: should re-fire because price moved >5c (reversion threshold)
        alerts2 = detector.check_correlated_markets(tracker)
        assert len(alerts2) == 1


# ──────────────────────────────────────────────────────────────
# Live Sports/Esports Filtering Tests
# ──────────────────────────────────────────────────────────────

class TestLiveEventFiltering:
    """Tests that live sports/esports events are filtered from alerts."""

    def test_esports_slug_filtered(self):
        """CS2 esports match should not produce alerts."""
        config = make_config()
        detector = AnomalyDetector(config)
        tracker = PriceTracker(config)

        event = make_event(
            event_id="cs2match",
            title="Counter-Strike: Monte vs Passion UA (BO3)",
            slug="cs2-monte-passion-2026-03-31",
        )
        outcome = event.outcomes[0]
        tracker.add_watch(event, outcome)

        now = datetime.utcnow()
        market = tracker.get_watched_markets()[outcome.token_id]
        history = deque([
            PriceSnapshot(timestamp=now - timedelta(minutes=20), mid_price=0.50, source="websocket"),
            PriceSnapshot(timestamp=now, mid_price=0.99, source="websocket"),
        ])
        inject_history(market, history)

        alert = detector.check_market(outcome.token_id, tracker)
        assert alert is None

    def test_lol_slug_filtered(self):
        """League of Legends match should not produce alerts."""
        config = make_config()
        detector = AnomalyDetector(config)
        tracker = PriceTracker(config)

        event = make_event(
            event_id="lolmatch",
            title="LoL: JD Gaming vs Oh My God (BO3)",
            slug="lol-jdg-omg-2026-03-31",
        )
        outcome = event.outcomes[0]
        tracker.add_watch(event, outcome)

        now = datetime.utcnow()
        market = tracker.get_watched_markets()[outcome.token_id]
        history = deque([
            PriceSnapshot(timestamp=now - timedelta(minutes=20), mid_price=0.30, source="websocket"),
            PriceSnapshot(timestamp=now, mid_price=0.80, source="websocket"),
        ])
        inject_history(market, history)

        alert = detector.check_market(outcome.token_id, tracker)
        assert alert is None

    def test_nba_live_match_filtered(self):
        """Live NBA match should not produce alerts."""
        config = make_config()
        detector = AnomalyDetector(config)
        tracker = PriceTracker(config)

        event = make_event(
            event_id="nbagame",
            title="Cavaliers vs. Lakers",
            slug="nba-cle-lal-2026-03-31",
        )
        outcome = event.outcomes[0]
        tracker.add_watch(event, outcome)

        now = datetime.utcnow()
        market = tracker.get_watched_markets()[outcome.token_id]
        history = deque([
            PriceSnapshot(timestamp=now - timedelta(minutes=20), mid_price=0.40, source="websocket"),
            PriceSnapshot(timestamp=now, mid_price=0.85, source="websocket"),
        ])
        inject_history(market, history)

        alert = detector.check_market(outcome.token_id, tracker)
        assert alert is None

    def test_geopolitical_not_filtered(self):
        """Iran ceasefire market should NOT be filtered."""
        config = make_config()
        detector = AnomalyDetector(config)
        tracker = PriceTracker(config)

        event = make_event(
            event_id="ceasefire",
            title="US x Iran ceasefire by...?",
            slug="us-x-iran-ceasefire-by",
        )
        outcome = event.outcomes[0]
        tracker.add_watch(event, outcome)

        now = datetime.utcnow()
        market = tracker.get_watched_markets()[outcome.token_id]
        history = deque([
            PriceSnapshot(timestamp=now - timedelta(minutes=20), mid_price=0.07, source="websocket"),
            PriceSnapshot(timestamp=now, mid_price=0.20, source="websocket"),
        ])
        inject_history(market, history)

        alert = detector.check_market(outcome.token_id, tracker)
        assert alert is not None

    def test_season_long_sports_not_filtered(self):
        """Season-long NBA Champion market should NOT be filtered."""
        config = make_config()
        detector = AnomalyDetector(config)
        tracker = PriceTracker(config)

        event = make_event(
            event_id="nbachamp",
            title="2026 NBA Champion",
            slug="2026-nba-champion",
        )
        outcome = event.outcomes[0]
        tracker.add_watch(event, outcome)

        now = datetime.utcnow()
        market = tracker.get_watched_markets()[outcome.token_id]
        history = deque([
            PriceSnapshot(timestamp=now - timedelta(minutes=20), mid_price=0.10, source="websocket"),
            PriceSnapshot(timestamp=now, mid_price=0.25, source="websocket"),
        ])
        inject_history(market, history)

        alert = detector.check_market(outcome.token_id, tracker)
        assert alert is not None

    def test_esports_filtered_from_correlation(self):
        """Esports outcomes should not count toward correlated alerts."""
        config = make_config()
        detector = AnomalyDetector(config)
        tracker = PriceTracker(config)

        now = datetime.utcnow()

        # 5 esports outcomes all moving together
        outcomes = []
        for i in range(5):
            outcomes.append(Outcome(
                outcome_id=f"o_{i}", market_id=f"m_{i}", condition_id=f"c_{i}",
                token_id=f"token_cs_{i}", name=f"Outcome {i}",
                status=OutcomeStatus.ACTIVE,
                bba=OutcomeBBA(best_bid=0.20, best_ask=0.25),
            ))
        event = make_event(
            event_id="cs2corr",
            title="CS: Monte vs Passion",
            slug="cs2-monte-passion-2026-03-31",
            outcomes=outcomes,
        )

        for outcome in outcomes:
            tracker.add_watch(event, outcome)
            market = tracker.get_watched_markets()[outcome.token_id]
            history = deque([
                PriceSnapshot(timestamp=now - timedelta(hours=1), mid_price=0.20, source="websocket"),
                PriceSnapshot(timestamp=now, mid_price=0.25, source="websocket"),
            ])
            inject_history(market, history)

        alerts = detector.check_correlated_markets(tracker)
        assert len(alerts) == 0

    def test_is_live_event_helper(self):
        """Direct test of _is_live_event helper."""
        config = make_config()
        detector = AnomalyDetector(config)

        assert detector._is_live_event("cs2-monte-passion-2026-03-31") is True
        assert detector._is_live_event("lol-jdg-omg-2026-03-31") is True
        assert detector._is_live_event("nba-cle-lal-2026-03-31") is True
        assert detector._is_live_event("nhl-sea-edm-2026-03-31") is True
        assert detector._is_live_event("wta-volynet-shnaide-2026-03-31") is True
        assert detector._is_live_event("atp-etcheve-gomez-2026-03-31") is True
        assert detector._is_live_event("blast-open-rotterdam-2026") is True

        assert detector._is_live_event("us-x-iran-ceasefire-by") is False
        assert detector._is_live_event("2026-nba-champion") is False
        assert detector._is_live_event("2026-fifa-world-cup-winner-595") is False
        assert detector._is_live_event("iran-leadership-change-by") is False
        assert detector._is_live_event("") is False
