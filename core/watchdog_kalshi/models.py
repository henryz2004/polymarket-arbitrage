"""
Kalshi Watchdog Models
=======================

Extends the base WatchdogConfig with Kalshi-specific defaults, and provides
lightweight adapter classes to map Kalshi markets to the interface expected
by the shared anomaly detector / price tracker.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from core.watchdog.models import WatchdogConfig


@dataclass
class KalshiWatchdogConfig(WatchdogConfig):
    """
    Kalshi-specific watchdog configuration.

    Inherits all thresholds, off-hours, cooldowns, etc. from WatchdogConfig.
    Overrides keywords and slug patterns for Kalshi's naming conventions.
    """

    # Override: Kalshi event tickers to force-watch (analogous to Polymarket slugs)
    watch_event_tickers: list[str] = field(default_factory=list)

    # Override: Kalshi series tickers to watch all events from
    watch_series_tickers: list[str] = field(default_factory=list)

    # Override: Kalshi categories to watch
    watch_categories: list[str] = field(default_factory=lambda: [
        "politics",
        "world",
        "geopolitics",
    ])

    # Override: live sports slug patterns for Kalshi
    # Kalshi uses series tickers like "KXNBA", "KXNFL", etc.
    skip_live_event_slug_prefixes: list[str] = field(default_factory=lambda: [
        "KXNBA", "KXNFL", "KXNHL", "KXMLB",   # US sports
        "KXCS2", "KXLOL", "KXVAL", "KXDOTA",   # Esports (if they exist)
        "KXUFC", "KXMMA",                        # MMA
        "KXWTA", "KXATP",                        # Tennis
    ])

    # Kalshi-specific: minimum contracts volume (Kalshi uses contract counts, not dollars)
    # $10k equivalent ~ 10,000 contracts at ~$1 each
    min_event_volume_24h: float = 5000.0

    # WebSocket auth
    kalshi_api_key: Optional[str] = None
    kalshi_private_key_path: Optional[str] = None

    # Demo mode
    kalshi_demo: bool = False


@dataclass
class WatchedKalshiMarket:
    """
    Adapter that presents a Kalshi market in the format expected by the
    shared PriceTracker.WatchedMarket interface.

    The key mapping: Polymarket's token_id -> Kalshi's market_ticker.
    """
    # Identifiers (mapped from Kalshi)
    token_id: str        # = market_ticker (watchdog key)
    event_id: str        # = event_ticker
    outcome_name: str    # = market title / yes_sub_title
    event_title: str     # = event title
    event_slug: str      # = series_ticker (for live-event filtering)
    event_volume_24h: float

    # Kalshi-specific metadata
    series_ticker: str = ""
    category: str = ""
    close_time: Optional[datetime] = None
