"""
Polymarket Watchdog Provider
============================

Bridges watchdog market discovery to the shared Polymarket/neg-risk registry
and BBA tracker without making watchdog.engine import neg-risk modules directly.
"""

from __future__ import annotations

from typing import Callable, Optional

class PolymarketWatchdogMarketData:
    """Platform adapter for Polymarket watchdog discovery + BBA tracking."""

    def __init__(self, min_outcomes: int, min_event_volume_24h: float,
                 registry_refresh_seconds: float, bba_ws_reconnect_delay: float,
                 staleness_ttl_ms: float):
        from core.negrisk.models import NegriskConfig

        # The watchdog reuses the Polymarket neg-risk registry and BBA tracker.
        # Those components expect the richer NegriskConfig surface even when
        # watchdog_mode disables the trading-specific behavior.
        self.config = NegriskConfig(
            min_outcomes=min_outcomes,
            min_event_volume_24h=min_event_volume_24h,
            registry_refresh_seconds=registry_refresh_seconds,
            bba_ws_reconnect_delay=bba_ws_reconnect_delay,
            staleness_ttl_ms=staleness_ttl_ms,
            min_liquidity_per_outcome=0.0,
            min_net_edge=0.0,
            watchdog_mode=True,
        )

        from core.negrisk.registry import NegriskRegistry

        self.registry = NegriskRegistry(self.config)

    def build_tracker(
        self,
        on_price_update: Callable[[str, str], None],
        token_filter: Optional[Callable[[], list[str]]] = None,
    ):
        from core.negrisk.bba_tracker import BBATracker

        return BBATracker(
            registry=self.registry,
            config=self.config,
            on_price_update=on_price_update,
            token_filter=token_filter,
        )
