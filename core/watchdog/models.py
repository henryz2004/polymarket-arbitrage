"""
Watchdog Data Models
=====================

Data structures for suspicious activity detection.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class WatchdogConfig:
    """Configuration for the suspicious activity watchdog."""

    # Event filtering
    watch_keywords: list[str] = field(default_factory=lambda: [
        "strike", "war", "attack", "sanctions", "invasion", "military",
        "election", "assassination", "bomb", "nuclear", "missile",
        "conflict", "coup", "ceasefire", "troops", "airstrikes",
        "tariff", "embargo", "hostage", "martial law",
    ])
    watch_slugs: list[str] = field(default_factory=lambda: [
        # High-profile Iran conflict markets (non-neg-risk, multi-outcome)
        # These are the primary targets for insider trading detection
        "us-x-iran-ceasefire-by",
        "us-forces-enter-iran-by",
        "usisrael-strikes-iran-by",
        "usisrael-strikes-iran-on",
        "iran-x-israelus-conflict-ends-by",
        "will-us-or-israel-strike-iran-first",
        "iran-leadership-change-or-us-x-iran-ceasefire-first",
        "us-x-iran-ceasefire-before-trump-visits-china",
        "trump-announces-end-of-military-operations-against-iran-by",
    ])
    min_event_volume_24h: float = 10_000.0

    # Spike thresholds (relative): list of (pct_change, window_seconds)
    # 50% in 1h, 100% in 4h, 200% in 24h
    relative_thresholds: list[tuple[float, int]] = field(default_factory=lambda: [
        (0.50, 3600),
        (1.00, 14400),
        (2.00, 86400),
    ])

    # Spike thresholds (absolute): list of (cent_move, window_seconds)
    # 5c in 30min, 10c in 1h, 15c in 4h — catches low-prob moves like 7c->20c
    absolute_thresholds: list[tuple[float, int]] = field(default_factory=lambda: [
        (0.05, 1800),
        (0.10, 3600),
        (0.15, 14400),
    ])

    # Off-hours window (UTC) — default 7-11 UTC = 2-6 AM EST
    off_hours_utc: tuple[int, int] = (7, 11)

    # Scanning
    price_poll_interval_seconds: float = 60.0
    price_history_window_hours: float = 48.0
    min_sample_interval_seconds: float = 10.0  # Rate-limit WS samples per token

    # Noise filtering
    min_price_floor: float = 0.03  # Ignore outcomes priced below 3c (too noisy)
    resolution_price_ceiling: float = 0.95  # Ignore outcomes at/above 95c (resolution, not insider)
    warmup_seconds: float = 300.0  # Don't fire alerts until 5min of live data

    # Live sports/esports slug prefixes to skip — these resolve in real-time
    # during gameplay and produce constant large swings that aren't insider
    # trading. Season-long futures (e.g. "2026-nba-champion") are NOT filtered.
    skip_live_event_slug_prefixes: list[str] = field(default_factory=lambda: [
        "cs2-", "lol-", "val-", "dota2-",          # Esports
        "nba-", "nhl-", "mlb-", "nfl-",            # US sports (live matches)
        "wta-", "atp-",                             # Tennis
        "blast-open-",                              # CS2 tournament events
    ])

    # Alert dedup
    alert_cooldown_seconds: float = 300.0

    # News
    news_check_enabled: bool = True
    news_lookback_hours: float = 2.0  # Only match headlines from last 2h (was 6h)

    # Registry (reuse NegriskConfig defaults for discovery)
    registry_refresh_seconds: float = 60.0
    bba_ws_reconnect_delay: float = 1.0
    min_outcomes: int = 2  # Watch binary events too
    staleness_ttl_ms: float = 60000.0


@dataclass
class PriceSnapshot:
    """A single price observation for an outcome."""
    timestamp: datetime
    mid_price: Optional[float] = None
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    source: str = "unknown"  # "clob_history", "clob", "websocket"


@dataclass
class NewsHeadline:
    """A news headline with publication timestamp for temporal correlation."""
    title: str
    published_at: Optional[datetime] = None  # UTC; None = unknown pub time

    @property
    def age_minutes(self) -> Optional[float]:
        """Minutes since publication (None if pub time unknown)."""
        if self.published_at is None:
            return None
        return (datetime.utcnow() - self.published_at).total_seconds() / 60

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "published_at": self.published_at.isoformat() if self.published_at else None,
        }


@dataclass
class AnomalyAlert:
    """A detected suspicious price anomaly."""
    # Identifiers
    alert_id: str
    event_id: str
    event_title: str
    event_slug: str
    outcome_name: str
    token_id: str

    # Price movement
    price_before: float
    price_after: float
    pct_change: float        # e.g. 1.79 for 179% (always positive magnitude)
    abs_change: float        # e.g. 0.125 for 12.5c (always positive magnitude)
    window_seconds: int      # Time window of the move
    threshold_type: str      # "relative" or "absolute"

    # Scoring
    suspicion_score: float   # 0-10 composite score
    is_off_hours: bool

    # Context
    event_volume_24h: float

    # Fields with defaults
    direction: str = "up"    # "up" (buy-side) or "down" (sell-side)
    correlated_outcomes: int = 0  # >0 if detected via cross-market correlation
    news_headlines: list[NewsHeadline] = field(default_factory=list)
    news_driven: bool = False
    detected_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        """Serialize for JSONL output."""
        return {
            "alert_id": self.alert_id,
            "event_id": self.event_id,
            "event_title": self.event_title,
            "event_slug": self.event_slug,
            "outcome_name": self.outcome_name,
            "token_id": self.token_id,
            "price_before": self.price_before,
            "price_after": self.price_after,
            "pct_change": round(self.pct_change, 4),
            "abs_change": round(self.abs_change, 4),
            "window_seconds": self.window_seconds,
            "threshold_type": self.threshold_type,
            "direction": self.direction,
            "correlated_outcomes": self.correlated_outcomes,
            "suspicion_score": round(self.suspicion_score, 2),
            "is_off_hours": self.is_off_hours,
            "event_volume_24h": self.event_volume_24h,
            "news_headlines": [h.to_dict() for h in self.news_headlines],
            "news_driven": self.news_driven,
            "detected_at": self.detected_at.isoformat(),
        }
