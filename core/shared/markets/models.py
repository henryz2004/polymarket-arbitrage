"""
Shared Market Data Models
=========================

Common event, outcome, and BBA structures used across apps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


@dataclass
class MarketDataConfig:
    """Shared market-discovery and BBA-tracking configuration."""

    min_net_edge: float = 0.0
    min_outcomes: int = 2
    min_event_volume_24h: float = 0.0
    registry_refresh_seconds: float = 60.0
    bba_ws_reconnect_delay: float = 1.0
    staleness_ttl_ms: float = 5000.0
    staleness_ttl_relaxed_ms: float = 15000.0
    staleness_ttl_strict_ms: float = 3000.0
    ws_sequence_gap_threshold: int = 5
    min_liquidity_per_outcome: float = 0.0
    skip_augmented_placeholders: bool = True
    max_horizon_days: float = 0.0
    watchdog_mode: bool = False


@dataclass
class PriceLevel:
    """A single price level in the order book."""

    price: float
    size: float


class OutcomeStatus(Enum):
    """Status of a market outcome."""

    ACTIVE = "active"
    PLACEHOLDER = "placeholder"
    OTHER = "other"
    RESOLVED = "resolved"


@dataclass
class OutcomeBBA:
    """Best bid/ask for a single outcome."""

    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    last_updated: datetime = field(default_factory=datetime.utcnow)
    sequence_id: Optional[int] = None
    source: str = "unknown"
    ask_levels: list = field(default_factory=list)
    bid_levels: list = field(default_factory=list)

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    def is_stale(self, ttl_ms: float) -> bool:
        age_ms = (datetime.utcnow() - self.last_updated).total_seconds() * 1000
        return age_ms > ttl_ms


@dataclass
class Outcome:
    """A single outcome in a multi-outcome market."""

    outcome_id: str
    market_id: str
    condition_id: str
    token_id: str
    name: str
    status: OutcomeStatus = OutcomeStatus.ACTIVE
    bba: OutcomeBBA = field(default_factory=OutcomeBBA)
    volume_24h: float = 0.0
    liquidity: float = 0.0
    # Gamma API probability — refreshed on each registry poll, not overwritten
    # by CLOB/WS data. More accurate display price than mid of wide spreads.
    gamma_probability: Optional[float] = None

    @property
    def ask_price(self) -> Optional[float]:
        return self.bba.best_ask

    @property
    def bid_price(self) -> Optional[float]:
        return self.bba.best_bid

    def is_tradeable(self, config: MarketDataConfig) -> bool:
        if self.status == OutcomeStatus.RESOLVED:
            return False
        if config.skip_augmented_placeholders and self.status == OutcomeStatus.PLACEHOLDER:
            return False
        if self.bba.best_ask is None:
            return False
        if self.bba.ask_size is not None and self.bba.ask_size < config.min_liquidity_per_outcome:
            return False
        return True

    def is_tradeable_sell_side(self, config: MarketDataConfig) -> bool:
        if self.status == OutcomeStatus.RESOLVED:
            return False
        if config.skip_augmented_placeholders and self.status == OutcomeStatus.PLACEHOLDER:
            return False
        if self.bba.best_bid is None:
            return False
        if self.bba.bid_size is not None and self.bba.bid_size < config.min_liquidity_per_outcome:
            return False
        return True


@dataclass
class MarketEvent:
    """A multi-outcome market event shared across apps."""

    event_id: str
    slug: str
    title: str
    condition_id: str
    platform: str = "polymarket"
    category: str = ""
    outcomes: list[Outcome] = field(default_factory=list)
    neg_risk: bool = True
    neg_risk_augmented: bool = False
    volume_24h: float = 0.0
    liquidity: float = 0.0
    end_date: Optional[datetime] = None
    created_at: Optional[datetime] = None
    fee_rate_bps: float = 0.0
    priority_score: float = 0.0
    hours_to_resolution: Optional[float] = None
    spread_volatility: float = 0.0
    last_updated: datetime = field(default_factory=datetime.utcnow)

    @property
    def outcome_count(self) -> int:
        return len(self.outcomes)

    @property
    def active_outcomes(self) -> list[Outcome]:
        return [
            outcome for outcome in self.outcomes
            if outcome.status not in (OutcomeStatus.RESOLVED, OutcomeStatus.PLACEHOLDER)
        ]

    @property
    def sum_of_asks(self) -> Optional[float]:
        asks = [outcome.bba.best_ask for outcome in self.active_outcomes]
        if None in asks or not asks:
            return None
        return sum(asks)

    @property
    def sum_of_bids(self) -> Optional[float]:
        bids = [outcome.bba.best_bid for outcome in self.active_outcomes]
        if None in bids or not bids:
            return None
        return sum(bids)

    @property
    def min_ask_liquidity(self) -> Optional[float]:
        sizes = [outcome.bba.ask_size for outcome in self.active_outcomes if outcome.bba.ask_size is not None]
        if not sizes:
            return None
        return min(sizes)

    @property
    def min_bid_liquidity(self) -> Optional[float]:
        sizes = [outcome.bba.bid_size for outcome in self.active_outcomes if outcome.bba.bid_size is not None]
        if not sizes:
            return None
        return min(sizes)

    def get_token_ids(self) -> list[str]:
        return [outcome.token_id for outcome in self.outcomes if outcome.token_id]

    def get_effective_staleness_ttl(self, config: MarketDataConfig) -> float:
        category = self.category.lower()
        if category in ("crypto", "finance"):
            return config.staleness_ttl_strict_ms
        if category in ("weather", "entertainment", "culture", "science"):
            return config.staleness_ttl_relaxed_ms
        return config.staleness_ttl_ms

    def has_stale_data(self, ttl_ms: float) -> bool:
        return any(outcome.bba.is_stale(ttl_ms) for outcome in self.active_outcomes)

    def has_stale_data_adaptive(self, config: MarketDataConfig) -> bool:
        ttl = self.get_effective_staleness_ttl(config)
        return any(outcome.bba.is_stale(ttl) for outcome in self.active_outcomes)
