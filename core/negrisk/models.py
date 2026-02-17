"""
Negrisk Data Models
====================

Data structures for neg-risk arbitrage detection and execution.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


@dataclass
class PriceLevel:
    """A single price level in the order book."""
    price: float
    size: float


class OutcomeStatus(Enum):
    """Status of an outcome in a neg-risk event."""
    ACTIVE = "active"           # Normal tradeable outcome
    PLACEHOLDER = "placeholder"  # Unnamed placeholder (augmented neg-risk)
    OTHER = "other"             # "Other" category
    RESOLVED = "resolved"       # Already resolved


class ArbDirection(Enum):
    """Direction of a neg-risk arbitrage trade."""
    BUY_ALL = "buy_all"    # Buy YES on all outcomes (sum_asks < $1.00)
    SELL_ALL = "sell_all"  # Sell YES on all outcomes (sum_bids > $1.00)
    PARTIAL_BUY = "partial_buy"    # Buy YES on subset of outcomes (+EV, not riskless)
    PARTIAL_SELL = "partial_sell"   # Sell YES on subset of outcomes (+EV, not riskless)
    BUY_BINARY = "buy_binary"    # Buy YES+NO on single market (sum_asks < $1.00)
    SELL_BINARY = "sell_binary"  # Sell YES+NO on single market (sum_bids > $1.00)


@dataclass
class NegriskConfig:
    """Configuration for neg-risk arbitrage detection and execution."""

    # Detection parameters
    min_net_edge: float = 0.025          # 2.5% minimum NET edge (after fees & gas)
    min_outcomes: int = 3                 # Minimum outcomes for neg-risk event
    max_legs: int = 15                    # Maximum outcomes to trade

    # Staleness parameters
    staleness_ttl_ms: float = 2000.0      # 2 seconds max staleness
    ws_sequence_gap_threshold: int = 5    # Max allowed sequence gaps

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
    min_liquidity_per_outcome: float = 100.0   # Min $ liquidity per outcome
    min_event_volume_24h: float = 10000.0      # Min event 24h volume
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


@dataclass
class OutcomeBBA:
    """Best Bid/Ask for a single outcome."""
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    last_updated: datetime = field(default_factory=datetime.utcnow)
    sequence_id: Optional[int] = None
    source: str = "unknown"  # "gamma", "clob", "websocket" — tracks data provenance

    # Full order book depth
    ask_levels: list = field(default_factory=list)  # list[PriceLevel], full ask depth
    bid_levels: list = field(default_factory=list)  # list[PriceLevel], full bid depth

    @property
    def spread(self) -> Optional[float]:
        """Get bid-ask spread."""
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    @property
    def mid_price(self) -> Optional[float]:
        """Get mid price."""
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    def is_stale(self, ttl_ms: float) -> bool:
        """Check if this BBA data is stale."""
        age_ms = (datetime.utcnow() - self.last_updated).total_seconds() * 1000
        return age_ms > ttl_ms


@dataclass
class Outcome:
    """
    A single outcome in a neg-risk event.

    For example, in "Who wins the 2024 election?":
    - Outcome 1: "Trump" with token_id "123..."
    - Outcome 2: "Biden" with token_id "456..."
    - Outcome 3: "Other" with token_id "789..."
    """
    outcome_id: str               # Unique identifier
    market_id: str                # Polymarket market ID for this outcome
    condition_id: str             # Condition ID
    token_id: str                 # CLOB token ID for YES shares
    name: str                     # Display name (e.g., "Trump")
    status: OutcomeStatus = OutcomeStatus.ACTIVE

    # Current BBA data
    bba: OutcomeBBA = field(default_factory=OutcomeBBA)

    # Metadata
    volume_24h: float = 0.0
    liquidity: float = 0.0

    @property
    def ask_price(self) -> Optional[float]:
        """Get current ask price (cost to buy YES)."""
        return self.bba.best_ask

    @property
    def bid_price(self) -> Optional[float]:
        """Get current bid price."""
        return self.bba.best_bid

    def is_tradeable(self, config: NegriskConfig) -> bool:
        """
        Check if this outcome is tradeable (buy-side).

        CRITICAL: For neg-risk arb, we MUST include "Other" outcomes.
        Skipping "Other" means we don't hold all outcomes and can lose principal.
        """
        # CRITICAL FIX: Include OTHER status in tradeable outcomes
        # Only skip PLACEHOLDER (unnamed in augmented neg-risk) and RESOLVED
        if self.status == OutcomeStatus.RESOLVED:
            return False

        # Skip placeholders only if configured (augmented neg-risk)
        if config.skip_augmented_placeholders and self.status == OutcomeStatus.PLACEHOLDER:
            return False

        # Must have an ask price
        if self.bba.best_ask is None:
            return False

        # Must meet minimum liquidity
        if self.bba.ask_size is not None and self.bba.ask_size < config.min_liquidity_per_outcome:
            return False

        return True

    def is_tradeable_sell_side(self, config: NegriskConfig) -> bool:
        """
        Check if this outcome is tradeable for sell-side arb.

        Same status rules as buy-side, but requires bid price and bid liquidity.
        """
        if self.status == OutcomeStatus.RESOLVED:
            return False

        if config.skip_augmented_placeholders and self.status == OutcomeStatus.PLACEHOLDER:
            return False

        # Must have a bid price (someone willing to buy our YES shares)
        if self.bba.best_bid is None:
            return False

        # Must meet minimum bid-side liquidity
        if self.bba.bid_size is not None and self.bba.bid_size < config.min_liquidity_per_outcome:
            return False

        return True


@dataclass
class NegriskEvent:
    """
    A neg-risk event containing multiple mutually exclusive outcomes.

    For example: "2024 Presidential Election Winner"
    - Contains outcomes for each candidate
    - Only ONE outcome can win (winner-take-all)
    - NegRisk adapter enables capital-efficient trading
    """
    event_id: str                 # Gamma API event ID
    slug: str                     # URL slug
    title: str                    # Event title/question
    condition_id: str             # CTF condition ID

    # Outcomes
    outcomes: list[Outcome] = field(default_factory=list)

    # Neg-risk flags
    neg_risk: bool = True
    neg_risk_augmented: bool = False  # Has placeholder outcomes

    # Metadata
    volume_24h: float = 0.0
    liquidity: float = 0.0
    end_date: Optional[datetime] = None

    # Tracking
    last_updated: datetime = field(default_factory=datetime.utcnow)

    @property
    def outcome_count(self) -> int:
        """Get number of outcomes."""
        return len(self.outcomes)

    @property
    def active_outcomes(self) -> list[Outcome]:
        """
        Get tradeable outcomes (includes OTHER, excludes PLACEHOLDER/RESOLVED).

        CRITICAL: This must include "Other" outcomes for neg-risk arb to work.
        """
        return [
            o for o in self.outcomes
            if o.status not in (OutcomeStatus.RESOLVED, OutcomeStatus.PLACEHOLDER)
        ]

    @property
    def sum_of_asks(self) -> Optional[float]:
        """
        Calculate sum of all ask prices.

        If < 1.0, there's a potential arbitrage opportunity.
        Returns None if any ask price is unavailable.
        """
        asks = [o.bba.best_ask for o in self.active_outcomes]
        if None in asks or len(asks) == 0:
            return None
        return sum(asks)

    @property
    def sum_of_bids(self) -> Optional[float]:
        """Calculate sum of all bid prices."""
        bids = [o.bba.best_bid for o in self.active_outcomes]
        if None in bids or len(bids) == 0:
            return None
        return sum(bids)

    @property
    def min_ask_liquidity(self) -> Optional[float]:
        """Get minimum liquidity across all asks (bottleneck for sizing)."""
        sizes = [o.bba.ask_size for o in self.active_outcomes if o.bba.ask_size is not None]
        if not sizes:
            return None
        return min(sizes)

    @property
    def min_bid_liquidity(self) -> Optional[float]:
        """Get minimum liquidity across all bids (bottleneck for sell-side sizing)."""
        sizes = [o.bba.bid_size for o in self.active_outcomes if o.bba.bid_size is not None]
        if not sizes:
            return None
        return min(sizes)

    def get_token_ids(self) -> list[str]:
        """Get all token IDs for WebSocket subscription."""
        return [o.token_id for o in self.outcomes if o.token_id]

    def has_stale_data(self, ttl_ms: float) -> bool:
        """Check if any outcome has stale BBA data."""
        return any(o.bba.is_stale(ttl_ms) for o in self.active_outcomes)


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
        if self.event.has_stale_data(config.staleness_ttl_ms):
            return False
        return True


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
    stale_data_rejections: int = 0
    liquidity_rejections: int = 0
    edge_too_low_rejections: int = 0
    execution_failures: int = 0

    # Best opportunity seen
    best_edge_seen: float = 0.0
    best_edge_event: str = ""
