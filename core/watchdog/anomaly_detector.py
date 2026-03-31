"""
Anomaly Detector
=================

Detects suspicious price spikes and computes suspicion scores.

Scoring components (0-10):
- Magnitude (0-3): how large the move was relative to threshold
- Speed (0-2): how fast the move happened
- Off-hours (0-2): bonus if during quiet hours
- Low baseline (0-1): bonus if price started very low (e.g. <10c)
- Near-resolution penalty (0 to -2): reduces score when price lands near
  resolution (>=95c or <=5c) since it may be normal market resolution
- Volume anomaly (0-2): reserved for future volume spike detection
"""

import logging
import uuid
from datetime import datetime
from typing import Optional

from core.watchdog.models import AnomalyAlert, WatchdogConfig
from core.watchdog.price_tracker import PriceTracker, WatchedMarket

logger = logging.getLogger(__name__)


class AnomalyDetector:
    """Detects suspicious price movements across watched markets."""

    def __init__(self, config: WatchdogConfig):
        self.config = config
        self._alert_cooldowns: dict[str, datetime] = {}  # token_id -> last alert time
        self._alerted_prices: dict[str, float] = {}  # token_id -> price at last alert
        self.stats = AnomalyDetectorStats()

    def check_market(self, token_id: str, tracker: PriceTracker) -> Optional[AnomalyAlert]:
        """
        Check a single market for anomalous price movement.

        Returns an AnomalyAlert if a threshold is breached and cooldown has expired,
        otherwise None.
        """
        market = tracker.get_watched_markets().get(token_id)
        if not market or len(market.history) < 2:
            return None

        # Skip outcomes below minimum price floor (sub-penny noise)
        current_price = market.current_price
        if current_price is not None and current_price < self.config.min_price_floor:
            return None

        # NOTE: We intentionally do NOT hard-filter near-resolution prices
        # (>=95c or <=5c) here. A market can spike from 7c to 96c due to
        # insider trading — filtering on price_after would suppress the exact
        # signal we want. Instead, _compute_suspicion_score applies a -2 point
        # penalty when price lands near resolution, and the news_driven check
        # handles the real disambiguation (news = legitimate resolution,
        # no news = suspicious).

        # Check cooldown
        now = datetime.utcnow()
        if token_id in self._alert_cooldowns:
            if now < self._alert_cooldowns[token_id]:
                return None

        # Suppress re-alerts if price hasn't moved beyond the already-alerted level.
        # Once a spike is reported (e.g. 50c -> 99.9c), don't keep firing every
        # cooldown period while the price sits at 99.9c. Only re-alert if the
        # price moves further (>2c beyond the last alerted price in either
        # direction). Also reset the alerted price if price reverts >5c from
        # the alerted level — a new independent spike may be starting.
        if token_id in self._alerted_prices:
            last_alerted = self._alerted_prices[token_id]
            if current_price is not None:
                # Reset if price has reverted significantly from alerted level
                if abs(current_price - last_alerted) > 0.05:
                    del self._alerted_prices[token_id]
                # Otherwise suppress if price hasn't moved meaningfully
                elif abs(current_price - last_alerted) <= 0.02:
                    return None

        self.stats.checks_performed += 1

        # Use the latest snapshot timestamp for off-hours detection
        # (more accurate than wall-clock — reflects when the move happened)
        move_time = market.history[-1].timestamp if market.history else now

        # Check both upward and downward spikes across relative + absolute thresholds
        best_alert = None
        best_score = 0.0

        for pct_threshold, window_seconds in self.config.relative_thresholds:
            result = tracker.get_price_change(token_id, window_seconds)
            if result is None:
                continue

            price_before, price_now, pct_change = result
            abs_chg = abs(price_now - price_before)

            # Upward spike (buy-side insider trading)
            if pct_change >= pct_threshold:
                alert = self._make_alert(
                    market, token_id, price_before, price_now,
                    pct_change, abs_chg, window_seconds,
                    "relative", "up", move_time, now,
                )
                if alert and alert.suspicion_score > best_score:
                    best_score = alert.suspicion_score
                    best_alert = alert

            # Downward spike (sell-side insider trading)
            # Use absolute pct_change for comparison, invert for scoring
            if pct_change <= -pct_threshold and price_before > 0:
                alert = self._make_alert(
                    market, token_id, price_before, price_now,
                    abs(pct_change), abs_chg, window_seconds,
                    "relative", "down", move_time, now,
                )
                if alert and alert.suspicion_score > best_score:
                    best_score = alert.suspicion_score
                    best_alert = alert

        # Check absolute thresholds (both directions)
        for cent_threshold, window_seconds in self.config.absolute_thresholds:
            result = tracker.get_abs_change(token_id, window_seconds)
            if result is None:
                continue

            price_before, price_now, abs_change = result

            if abs_change >= cent_threshold:
                direction = "up" if price_now > price_before else "down"
                pct_change = abs(price_now - price_before) / price_before if price_before > 0 else 0
                alert = self._make_alert(
                    market, token_id, price_before, price_now,
                    pct_change, abs_change, window_seconds,
                    "absolute", direction, move_time, now,
                )
                if alert and alert.suspicion_score > best_score:
                    best_score = alert.suspicion_score
                    best_alert = alert

        if best_alert:
            # Set cooldown and record the alerted price level
            from datetime import timedelta
            self._alert_cooldowns[token_id] = now + timedelta(
                seconds=self.config.alert_cooldown_seconds
            )
            self._alerted_prices[token_id] = best_alert.price_after
            self.stats.alerts_fired += 1
            if best_score > self.stats.highest_score:
                self.stats.highest_score = best_score

        return best_alert

    def _make_alert(self, market: WatchedMarket, token_id: str,
                    price_before: float, price_now: float,
                    pct_change: float, abs_change: float,
                    window_seconds: int, threshold_type: str,
                    direction: str, move_time: datetime,
                    now: datetime) -> Optional[AnomalyAlert]:
        """
        Build an AnomalyAlert with suspicion scoring.

        pct_change and abs_change should be positive magnitudes regardless of direction.
        """
        # For downward spikes, use price_before as the "high baseline" for scoring
        # (a drop from 80c is more meaningful than from 10c)
        score_price_before = price_before if direction == "up" else price_now
        score = self._compute_suspicion_score(
            pct_change=pct_change,
            abs_change=abs_change,
            window_seconds=window_seconds,
            price_before=score_price_before,
            now=move_time,
            price_after=price_now if direction == "up" else price_before,
        )
        if score <= 0:
            return None

        return AnomalyAlert(
            alert_id=f"alert_{uuid.uuid4().hex[:12]}",
            event_id=market.event_id,
            event_title=market.event_title,
            event_slug=market.event_slug,
            outcome_name=market.outcome_name,
            token_id=token_id,
            price_before=price_before,
            price_after=price_now,
            pct_change=pct_change,
            abs_change=abs_change,
            window_seconds=window_seconds,
            threshold_type=threshold_type,
            direction=direction,
            suspicion_score=score,
            is_off_hours=self._is_off_hours(move_time),
            event_volume_24h=market.event_volume_24h,
            detected_at=now,
        )

    def check_all_markets(self, tracker: PriceTracker) -> list[AnomalyAlert]:
        """Check all watched markets and return any alerts."""
        alerts = []
        for token_id in list(tracker.get_watched_markets().keys()):
            alert = self.check_market(token_id, tracker)
            if alert:
                alerts.append(alert)

        # Periodically prune expired cooldowns to prevent unbounded dict growth
        # on long-running watchdog sessions (every ~100 scan cycles).
        self.stats.checks_performed  # Just a counter check
        if len(self._alert_cooldowns) > 500:
            self._prune_expired()

        return alerts

    def _prune_expired(self) -> None:
        """Remove expired cooldown entries and stale alerted prices."""
        now = datetime.utcnow()
        expired = [tid for tid, t in self._alert_cooldowns.items() if t <= now]
        for tid in expired:
            del self._alert_cooldowns[tid]
            # Also clean up alerted prices for tokens whose cooldown has expired,
            # since the re-alert suppression resets on >5c reversion anyway.
            self._alerted_prices.pop(tid, None)

    def _compute_suspicion_score(self, pct_change: float, abs_change: float,
                                  window_seconds: int, price_before: float,
                                  now: datetime, price_after: float = 0.0) -> float:
        """
        Compute composite suspicion score (0-10).

        Components:
        - Magnitude (0-3): size of the move
        - Speed (0-2): faster moves are more suspicious
        - Off-hours (0-2): moves during quiet hours
        - Low baseline (0-1): moves from low prices (e.g. 7c)
        - Near-resolution penalty (0 to -2): when price lands at >=95c or <=5c,
          may be normal resolution rather than insider trading. Penalty is applied
          softly so alerts still fire (for JSONL logging) — the news_driven check
          provides the real disambiguation.
        - Volume anomaly (0-2): reserved (currently 0)
        """
        score = 0.0

        # 1. Magnitude (0-3)
        # Scale: 50% = 1.0, 100% = 2.0, 200%+ = 3.0
        magnitude = min(pct_change / 0.5, 3.0) if pct_change > 0 else 0
        # Also consider absolute change for low-prob outcomes
        abs_magnitude = min(abs_change / 0.10, 2.0)  # 10c = 2.0
        magnitude = max(magnitude, abs_magnitude)
        magnitude = min(magnitude, 3.0)
        score += magnitude

        # 2. Speed (0-2)
        # Faster moves are more suspicious
        # Under 30 min = 2.0, under 1h = 1.5, under 4h = 1.0, under 24h = 0.5
        if window_seconds <= 1800:
            speed = 2.0
        elif window_seconds <= 3600:
            speed = 1.5
        elif window_seconds <= 14400:
            speed = 1.0
        else:
            speed = 0.5
        score += speed

        # 3. Off-hours (0-2)
        if self._is_off_hours(now):
            score += 2.0

        # 4. Low baseline (0-1)
        # Moves from very low prices (<10c) are more suspicious — suggests informed trading
        if price_before < 0.10:
            score += 1.0
        elif price_before < 0.20:
            score += 0.5

        # 5. Near-resolution penalty (0 to -2)
        # When price lands near resolution (>=95c or <=5c), it may be a normal
        # market resolution rather than insider trading. We penalize rather than
        # hard-filter because: (a) insider spikes CAN land at resolution prices
        # (7c -> 96c), and (b) the news_driven flag is the real signal.
        ceiling = self.config.resolution_price_ceiling  # default 0.95
        floor = 1.0 - ceiling  # default 0.05
        if price_after >= ceiling or price_after <= floor:
            score -= 2.0

        # 6. Volume anomaly (0-2) — reserved for future
        # Could check if trade volume spiked relative to historical average

        return max(min(score, 10.0), 0.0)

    def _is_off_hours(self, now: datetime) -> bool:
        """Check if current time is during off-hours (quiet period)."""
        start_hour, end_hour = self.config.off_hours_utc
        hour = now.hour
        if start_hour <= end_hour:
            return start_hour <= hour < end_hour
        else:
            # Wraps around midnight
            return hour >= start_hour or hour < end_hour

    def get_stats(self) -> dict:
        """Get detector statistics."""
        return {
            "checks_performed": self.stats.checks_performed,
            "alerts_fired": self.stats.alerts_fired,
            "highest_score": round(self.stats.highest_score, 2),
            "active_cooldowns": sum(
                1 for t in self._alert_cooldowns.values()
                if t > datetime.utcnow()
            ),
        }


class AnomalyDetectorStats:
    """Statistics for the anomaly detector."""

    def __init__(self):
        self.checks_performed: int = 0
        self.alerts_fired: int = 0
        self.highest_score: float = 0.0
