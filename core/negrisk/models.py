"""
Negrisk Data Models
====================

Data structures for neg-risk arbitrage detection and execution.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from core.shared.markets.models import (
    MarketDataConfig,
    MarketEvent,
    Outcome,
    OutcomeBBA,
    OutcomeStatus,
    PriceLevel,
)


class ArbDirection(Enum):
    """Direction of a neg-risk arbitrage trade."""
    BUY_ALL = "buy_all"    # Buy YES on all outcomes (sum_asks < $1.00)
    SELL_ALL = "sell_all"  # Sell YES on all outcomes (sum_bids > $1.00)
    PARTIAL_BUY = "partial_buy"    # Buy YES on subset of outcomes (+EV, not riskless)
    PARTIAL_SELL = "partial_sell"   # Sell YES on subset of outcomes (+EV, not riskless)
    BUY_BINARY = "buy_binary"    # Buy YES+NO on single market (sum_asks < $1.00)
    SELL_BINARY = "sell_binary"  # Sell YES+NO on single market (sum_bids > $1.00)


@dataclass
class NegriskConfig(MarketDataConfig):
    """Configuration for neg-risk arbitrage detection and execution."""

    # Detection parameters
    min_net_edge: float = 0.015          # 1.5% minimum NET edge (after fees & gas)
    min_outcomes: int = 3                 # Minimum outcomes for neg-risk event
    max_legs: int = 15                    # Maximum outcomes to trade

    # Fee parameters (Polymarket)
    # fee_rate_bps: The feeRateBps value from the CLOB API.
    # Most neg-risk markets are fee-free (0). Fee-enabled markets (e.g. 15-min crypto) use 1000.
    # Per-leg fee formula (from CTF Exchange contract):
    #   SELL: fee = (fee_rate_bps / 10000) * min(price, 1-price) * shares
    #   BUY:  fee = (fee_rate_bps / 10000) * min(price, 1-price) * shares / price
    # At p=0.50 with fee_rate_bps=1000: fee = 0.1 * 0.5 = 0.05/share (5%)
    # Should be fetched dynamically per token in production.
    fee_rate_bps: float = 0               # Most neg-risk markets are fee-free
    gas_per_leg: float = 0.0              # Polymarket covers gas on Polygon

    # Execution parameters
    min_liquidity_per_outcome: float = 50.0     # Min $ liquidity per outcome
    min_event_volume_24h: float = 5000.0       # Min event 24h volume
    use_fok_orders: bool = True                # Use Fill-or-Kill orders

    # Risk parameters
    max_position_per_event: float = 500.0      # Max $ per event
    skip_augmented_placeholders: bool = True   # Skip unnamed outcomes

    # Partial position parameters (NOT riskless arb, +EV only)
    enable_partial_positions: bool = False      # Disabled by default
    min_partial_ev: float = 0.05               # 5% minimum expected value
    max_excluded_probability: float = 0.15     # Don't exclude outcomes with >15% implied prob
    partial_kelly_fraction: float = 0.25       # Quarter-Kelly for safety

    # Refresh intervals
    registry_refresh_seconds: float = 30.0     # How often to refresh event list
    bba_ws_reconnect_delay: float = 1.0        # WebSocket reconnect delay

    # Binary bundle arbitrage
    binary_bundle_enabled: bool = False        # Enable YES+NO bundle arb on binary markets

    # Order book depth scanning
    use_depth_scanning: bool = True            # Use full book depth for edge calculation
    max_book_levels: int = 10                  # Max depth levels to store

    # WebSocket-only mode (Improvement 5)
    ws_only_mode: bool = False              # Skip CLOB fetch, trust WebSocket data
    detection_latency_tracking: bool = True  # Track detection latency stats

    # Order strategy (maker vs taker)
    order_strategy: str = "taker"              # "taker" or "maker"
    maker_price_offset_bps: float = 0          # Offset from mid-price in bps (0 = at mid)
    maker_timeout_seconds: float = 30.0        # Cancel unfilled maker orders after this
    maker_min_net_edge: float = 0.015          # Lower threshold for maker (no fee)

    # Partial-CLOB tolerance: allow some gamma-only legs
    max_gamma_only_legs: int = 0          # Max outcomes allowed with gamma-only prices (0 = conservative)
    gamma_max_spread: float = 0.05        # Max gamma spread to tolerate (5 cents)
    gamma_max_probability: float = 0.20   # Max implied probability for gamma-only legs (20%)

    # CLOB re-seeding
    reseed_interval_seconds: float = 300.0  # Re-seed gamma-only tokens every 5 minutes

    # Event prioritization
    prioritize_near_resolution: bool = True
    resolution_window_hours: float = 24.0      # Events resolving within this window get priority
    priority_edge_discount: float = 0.5        # Multiply min_net_edge by this for high-priority events
    volume_spike_threshold: float = 2.0        # 2x average volume = spike
NegriskEvent = MarketEvent


@dataclass
class NegriskOpportunity:
    """
    A detected neg-risk arbitrage opportunity.

    BUY_ALL: Buy YES on all outcomes when sum_asks < $1.00.
    SELL_ALL: Sell YES on all outcomes when sum_bids > $1.00.

    In both cases, exactly one outcome resolves to $1.00, guaranteeing profit.
    """
    opportunity_id: str
    event: NegriskEvent
    platform: str = "polymarket"  # Platform identifier

    # Direction
    direction: ArbDirection = ArbDirection.BUY_ALL

    # Pricing
    sum_of_prices: float = 0.0    # sum_of_asks (BUY) or sum_of_bids (SELL)
    gross_edge: float = 0.0       # |1.0 - sum_of_prices| before fees
    net_edge: float = 0.0         # After fees and gas

    # Sizing
    suggested_size: float = 0.0   # Shares per outcome
    max_size: float = 0.0         # Maximum based on liquidity

    # Execution details
    legs: list[dict] = field(default_factory=list)  # Order specs for each leg

    # Timing
    detected_at: datetime = field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = None
    executed: bool = False
    detection_latency_ms: float = 0.0  # Time from price update to opportunity detection

    # Backward compat alias
    @property
    def sum_of_asks(self) -> float:
        """Backward compat: returns sum_of_prices."""
        return self.sum_of_prices

    @property
    def total_cost(self) -> float:
        """Total cost to execute this opportunity at suggested size."""
        return self.sum_of_prices * self.suggested_size

    @property
    def expected_profit(self) -> float:
        """Expected profit from this opportunity."""
        return self.net_edge * self.suggested_size

    @property
    def num_legs(self) -> int:
        """Number of orders needed."""
        return len(self.event.active_outcomes)

    def is_valid(self, config: NegriskConfig) -> bool:
        """Check if opportunity is still valid."""
        if self.executed:
            return False
        if self.expires_at and datetime.utcnow() > self.expires_at:
            return False
        if self.net_edge < config.min_net_edge:
            return False
        # Use category-adaptive staleness to match detection logic
        if self.event.has_stale_data_adaptive(config):
            return False
        return True


@dataclass
class MakerOrderState:
    """Tracks state of a pending maker order leg."""
    opportunity_id: str
    leg_index: int
    token_id: str
    market_id: str
    side: str                    # "BUY" or "SELL"
    price: float
    size: float
    order_id: Optional[str] = None
    filled: bool = False
    filled_size: float = 0.0
    placed_at: Optional[datetime] = None
    cancelled: bool = False


@dataclass
class NegriskStats:
    """Statistics for neg-risk arbitrage operations."""
    events_tracked: int = 0
    opportunities_detected: int = 0
    opportunities_submitted: int = 0  # Submitted to execution engine
    opportunities_executed: int = 0   # Actually filled (requires callback)
    total_profit: float = 0.0
    total_volume: float = 0.0

    # Timing stats
    avg_detection_latency_ms: float = 0.0
    min_detection_latency_ms: float = float('inf')
    max_detection_latency_ms: float = 0.0
    total_detections_timed: int = 0
    avg_execution_latency_ms: float = 0.0

    # Error tracking
    stale_data_rejections: int = 0           # Buy-side stale rejections
    stale_data_rejections_sell: int = 0      # Sell-side stale rejections (tracked separately)
    incomplete_coverage_rejections: int = 0
    liquidity_rejections: int = 0
    edge_too_low_rejections: int = 0
    execution_failures: int = 0

    # Pre-filter metrics (performance optimization tracking)
    prefilter_events_skipped: int = 0     # Events skipped by sum proximity pre-filter
    prefilter_events_passed: int = 0      # Events that passed pre-filter to detector
    prefilter_callbacks_skipped: int = 0  # WS callbacks skipped by pre-filter

    # Best opportunity seen
    best_edge_seen: float = 0.0
    best_edge_event: str = ""
