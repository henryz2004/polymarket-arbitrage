"""
Negrisk Opportunity Detector
==============================

Detects arbitrage opportunities in neg-risk events.

Buy-side: sum(asks) < $1.00 → buy all outcomes, profit when one resolves.
Sell-side: sum(bids) > $1.00 → sell all outcomes, profit = proceeds - $1.00 payout.
"""

import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

from core.negrisk.models import (
    ArbDirection,
    NegriskConfig,
    NegriskEvent,
    NegriskOpportunity,
    NegriskStats,
    OutcomeStatus,
)


logger = logging.getLogger(__name__)


def _walk_book(levels: list, target_size: float) -> tuple[float, float]:
    """
    Walk order book levels to find average fill price at target_size.

    Args:
        levels: List of PriceLevel(price, size) sorted best-to-worst
        target_size: Number of shares to fill

    Returns:
        (avg_fill_price, max_fillable_size)
        If not enough depth, returns (avg_price_of_available, total_available)
    """
    if not levels or target_size <= 0:
        return (0.0, 0.0)

    total_filled = 0.0
    total_cost = 0.0

    for level in levels:
        fill_at_level = min(level.size, target_size - total_filled)
        total_cost += fill_at_level * level.price
        total_filled += fill_at_level
        if total_filled >= target_size:
            break

    if total_filled <= 0:
        return (0.0, 0.0)

    avg_price = total_cost / total_filled
    return (avg_price, total_filled)


def _compute_fee_per_share(fee_rate_bps: float, prices: list[float], side: str) -> float:
    """
    Compute total taker fee per share across all legs using the Polymarket
    CTF Exchange formula (from CalculatorHelper.sol).

    SELL: fee_per_leg = (fee_rate_bps / 10000) * min(price, 1-price) * shares
          → per-share fee contribution = (fee_rate_bps / 10000) * min(p, 1-p)
    BUY:  fee_per_leg = (fee_rate_bps / 10000) * min(price, 1-price) * shares / price
          → per-share fee contribution = (fee_rate_bps / 10000) * min(p, 1-p) / p

    Total fee per share = sum of per-share fee contributions across all legs.

    Args:
        fee_rate_bps: The feeRateBps value (0 for fee-free markets, 1000 for fee-enabled).
        prices: List of prices for each leg (ask prices for BUY, bid prices for SELL).
        side: "BUY" or "SELL".

    Returns:
        Total fee per share across all legs.
    """
    if fee_rate_bps == 0:
        return 0.0

    base_rate = fee_rate_bps / 10000.0
    total_fee = 0.0

    for p in prices:
        if p <= 0 or p >= 1.0:
            continue
        min_p = min(p, 1.0 - p)
        if side == "BUY":
            # Fee is on outcome tokens received: base_rate * min(p, 1-p) / p per share
            total_fee += base_rate * min_p / p
        else:
            # Fee is on collateral received: base_rate * min(p, 1-p) per share
            total_fee += base_rate * min_p

    return total_fee


class NegriskDetector:
    """
    Detects neg-risk arbitrage opportunities.

    Buy-side: sum(asks) + fees + gas < $1.00 → buy all, profit guaranteed.
    Sell-side: sum(bids) - fees - gas > $1.00 → sell all, profit guaranteed.
    """

    def __init__(self, config: NegriskConfig):
        self.config = config
        self.stats = NegriskStats()

        # Track recent opportunities to avoid duplicates
        self._recent_opportunities: dict[str, NegriskOpportunity] = {}
        self._opportunity_cooldown: dict[str, datetime] = {}

        # Diagnostics: top candidates from last scan
        self._last_scan_candidates: list[dict] = []

        # Diagnostic: throttle LIQ_REJECT logs (one per event per 60s)
        self._liq_reject_log_cooldown: dict[str, datetime] = {}

    def detect_opportunities(self, events: list[NegriskEvent], strategy: str = "taker") -> list[NegriskOpportunity]:
        """
        Scan all events for arbitrage opportunities (both buy-side and sell-side).

        Args:
            events: List of neg-risk events to scan
            strategy: "taker" (cross spread) or "maker" (price at mid)

        Returns:
            List of detected opportunities
        """
        opportunities = []
        # Track best candidates this scan for diagnostics
        self._last_scan_candidates: list[dict] = []

        for event in events:
            if strategy == "maker":
                # Maker mode: price at mid-price
                buy_opp = self._check_event_maker(event)
                sell_opp = self._check_event_maker_sell_side(event)
            else:
                # Taker mode: cross the spread
                buy_opp = self._check_event(event)
                sell_opp = self._check_event_sell_side(event)

            if buy_opp:
                opportunities.append(buy_opp)
            if sell_opp:
                opportunities.append(sell_opp)

        # Sort candidates by gross_edge descending, keep top 10
        self._last_scan_candidates.sort(key=lambda c: c["gross_edge"], reverse=True)
        self._last_scan_candidates = self._last_scan_candidates[:10]

        return opportunities

    def _check_event(self, event: NegriskEvent, detection_start: Optional[float] = None) -> Optional[NegriskOpportunity]:
        """
        Check a single event for arbitrage opportunity.

        Args:
            event: The event to check
            detection_start: Timestamp from time.monotonic() for latency tracking
        """
        # Get tradeable outcomes (includes OTHER, excludes PLACEHOLDER/RESOLVED)
        tradeable = [o for o in event.outcomes if o.is_tradeable(self.config)]

        if len(tradeable) < self.config.min_outcomes:
            return None

        if len(tradeable) > self.config.max_legs:
            logger.debug(f"Event {event.title} has too many legs: {len(tradeable)}")
            return None

        # Check for stale data
        if event.has_stale_data(self.config.staleness_ttl_ms):
            self.stats.stale_data_rejections += 1
            return None

        # CRITICAL FIX: Calculate sum of asks from tradeable outcomes only
        # This ensures we're pricing the same set of outcomes we're actually trading
        asks = [o.bba.best_ask for o in tradeable]
        if None in asks or len(asks) == 0:
            return None
        sum_of_asks = sum(asks)

        # Calculate gross edge first
        num_legs = len(tradeable)
        gross_edge = 1.0 - sum_of_asks

        # CRITICAL FIX: Check liquidity from tradeable outcomes only
        ask_sizes = [o.bba.ask_size for o in tradeable if o.bba.ask_size is not None]
        if not ask_sizes:
            self.stats.liquidity_rejections += 1
            # Diagnostic: compute edge from ALL priced outcomes, split by source
            all_priced = [o for o in event.outcomes
                         if o.status not in (OutcomeStatus.RESOLVED, OutcomeStatus.PLACEHOLDER)
                         and o.bba.best_ask is not None]
            if all_priced:
                clob_priced = [o for o in all_priced if o.bba.source in ("clob", "websocket")]
                gamma_only = [o for o in all_priced if o.bba.source == "gamma"]
                full_sum = sum(o.bba.best_ask for o in all_priced)
                full_gross = 1.0 - full_sum
                # Only log if CLOB-confirmed prices show positive edge, or if data is mixed
                clob_sum = sum(o.bba.best_ask for o in clob_priced) if clob_priced else 0
                gamma_sum = sum(o.bba.best_ask for o in gamma_only) if gamma_only else 0
                if full_gross > 0 or (clob_priced and gamma_only):
                    now = datetime.utcnow()
                    cooldown = self._liq_reject_log_cooldown.get(event.event_id)
                    if not cooldown or now > cooldown:
                        self._liq_reject_log_cooldown[event.event_id] = now + timedelta(seconds=60)
                        label = "PHANTOM" if gamma_only else "REAL"
                        logger.info(
                            f"LIQ_REJECT({label}): {event.title[:50]} | "
                            f"outcomes={len(all_priced)} (clob={len(clob_priced)}, gamma={len(gamma_only)}) | "
                            f"sum_asks={full_sum:.4f} (clob={clob_sum:.4f} + gamma={gamma_sum:.4f}) | "
                            f"gross_edge={full_gross:.4f} ({full_gross*100:.2f}%)"
                        )
            return None

        min_liquidity = min(ask_sizes)
        if min_liquidity < self.config.min_liquidity_per_outcome:
            self.stats.liquidity_rejections += 1
            # Diagnostic: log edge for events rejected on liquidity
            if gross_edge > 0:
                now = datetime.utcnow()
                cooldown = self._liq_reject_log_cooldown.get(event.event_id)
                if not cooldown or now > cooldown:
                    self._liq_reject_log_cooldown[event.event_id] = now + timedelta(seconds=60)
                    logger.info(
                        f"LIQ_REJECT: {event.title[:60]} | legs={num_legs} | "
                        f"sum_asks={sum_of_asks:.4f} | gross_edge={gross_edge:.4f} ({gross_edge*100:.2f}%) | "
                        f"min_ask_size={min_liquidity:.1f}"
                    )
            return None

        # Calculate sizing BEFORE calculating net edge
        # Size is constrained by:
        # 1. Minimum liquidity across all tradeable outcomes (bottleneck)
        # 2. Max position per event
        max_size_liquidity = min_liquidity
        max_size_risk = self.config.max_position_per_event / sum_of_asks if sum_of_asks > 0 else 0

        max_size = min(max_size_liquidity, max_size_risk)
        suggested_size = max_size * 0.8  # Use 80% of max for safety

        if suggested_size <= 0:
            return None

        # Apply depth scanning if enabled and depth data is available
        depth_adjusted = False
        if self.config.use_depth_scanning:
            has_depth = all(len(o.bba.ask_levels) > 0 for o in tradeable)

            if has_depth:
                # Recalculate prices using depth
                depth_adjusted_prices = []
                depth_max_sizes = []

                for outcome in tradeable:
                    avg_price, max_fill = _walk_book(outcome.bba.ask_levels, suggested_size)
                    if max_fill < suggested_size:
                        # Not enough depth — reduce size to what's available
                        suggested_size = max_fill * 0.8
                    depth_adjusted_prices.append(avg_price)
                    depth_max_sizes.append(max_fill)

                if suggested_size > 0:
                    # Recalculate all metrics with depth-adjusted prices
                    sum_of_asks = sum(depth_adjusted_prices)
                    gross_edge = 1.0 - sum_of_asks
                    asks = depth_adjusted_prices  # Use depth prices for fee calculation
                    depth_adjusted = True

        # CRITICAL FIX: Calculate gas per share (amortized over trade size)
        # Gas is a FIXED COST in dollars, not per-share
        # We need to amortize it over the number of shares to get per-share cost
        total_gas_cost = self.config.gas_per_leg * num_legs  # Total $ gas cost
        gas_per_share = total_gas_cost / suggested_size if suggested_size > 0 else total_gas_cost

        # Fee per share using Polymarket's on-chain formula:
        # BUY: fee = (fee_rate_bps / 10000) * min(p, 1-p) / p per leg
        # Most neg-risk markets are fee-free (fee_rate_bps=0).
        fee_per_share = _compute_fee_per_share(self.config.fee_rate_bps, asks, "BUY")

        # Net edge (all per-share metrics now)
        net_edge = gross_edge - fee_per_share - gas_per_share

        # Track candidate for diagnostics (all events that pass liquidity check)
        self._last_scan_candidates.append({
            "title": event.title[:60],
            "direction": "BUY",
            "legs": num_legs,
            "sum_prices": round(sum_of_asks, 4),
            "gross_edge": round(gross_edge, 4),
            "fee": round(fee_per_share, 4),
            "gas_per_share": round(gas_per_share, 6),
            "net_edge": round(net_edge, 4),
            "min_liq": round(min_liquidity, 0),
            "size": round(suggested_size, 0),
            "depth_adjusted": depth_adjusted,
        })

        # Check minimum net edge (after fees and gas)
        if net_edge < self.config.min_net_edge:
            self.stats.edge_too_low_rejections += 1
            # Log top candidates so we can sanity-check the fee math
            if gross_edge > 0:
                logger.debug(
                    f"EDGE_REJECT: {event.title[:60]} | legs={num_legs} | "
                    f"sum_asks={sum_of_asks:.4f} | gross={gross_edge:.4f} ({gross_edge*100:.2f}%) | "
                    f"fee={fee_per_share:.4f} | gas/sh={gas_per_share:.6f} | "
                    f"net={net_edge:.4f} ({net_edge*100:.2f}%) | "
                    f"min_liq={min_liquidity:.0f} | size={suggested_size:.0f}"
                )
            return None

        # Check cooldown to avoid spam
        cooldown_key = event.event_id
        if cooldown_key in self._opportunity_cooldown:
            if datetime.utcnow() < self._opportunity_cooldown[cooldown_key]:
                return None

        self._opportunity_cooldown[cooldown_key] = datetime.utcnow() + timedelta(seconds=2)

        # Build leg specifications
        legs = []
        for outcome in tradeable:
            leg = {
                "token_id": outcome.token_id,
                "market_id": outcome.market_id,
                "outcome_name": outcome.name,
                "side": "BUY",
                "price": outcome.bba.best_ask,
                "size": suggested_size,
            }
            legs.append(leg)

        # Create opportunity
        opportunity = NegriskOpportunity(
            opportunity_id=f"negrisk_{uuid.uuid4().hex[:12]}",
            event=event,
            direction=ArbDirection.BUY_ALL,
            sum_of_prices=sum_of_asks,
            gross_edge=gross_edge,
            net_edge=net_edge,
            suggested_size=suggested_size,
            max_size=max_size,
            legs=legs,
            detected_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(seconds=5),  # 5s expiry
        )

        # Track detection latency if enabled
        if detection_start is not None and self.config.detection_latency_tracking:
            latency_ms = (time.monotonic() - detection_start) * 1000
            opportunity.detection_latency_ms = latency_ms
            # Update stats
            self.stats.total_detections_timed += 1
            self.stats.max_detection_latency_ms = max(self.stats.max_detection_latency_ms, latency_ms)
            self.stats.min_detection_latency_ms = min(self.stats.min_detection_latency_ms, latency_ms)
            # Running average
            n = self.stats.total_detections_timed
            self.stats.avg_detection_latency_ms = (
                self.stats.avg_detection_latency_ms * (n - 1) + latency_ms
            ) / n

        # Track stats
        self.stats.opportunities_detected += 1
        if net_edge > self.stats.best_edge_seen:
            self.stats.best_edge_seen = net_edge
            self.stats.best_edge_event = event.title

        # Cache opportunity
        self._recent_opportunities[opportunity.opportunity_id] = opportunity

        logger.info(
            f"BUY-ALL opportunity: {event.title[:40]} | "
            f"sum_asks={sum_of_asks:.4f} | gross={gross_edge:.4f} | "
            f"fees={fee_per_share:.4f} | gas/share={gas_per_share:.6f} | "
            f"NET edge={net_edge:.4f} | legs={num_legs} | size={suggested_size:.2f}"
        )

        return opportunity

    def _check_event_maker(self, event: NegriskEvent) -> Optional[NegriskOpportunity]:
        """
        Check a single event for maker-mode buy-side opportunity.

        Prices at mid-price (between bid and ask) instead of crossing the spread.
        Maker orders pay 0% fee on Polymarket.
        """
        # Get tradeable outcomes (includes OTHER, excludes PLACEHOLDER/RESOLVED)
        tradeable = [o for o in event.outcomes if o.is_tradeable(self.config)]

        if len(tradeable) < self.config.min_outcomes:
            return None

        if len(tradeable) > self.config.max_legs:
            logger.debug(f"Event {event.title} has too many legs: {len(tradeable)}")
            return None

        # Check for stale data
        if event.has_stale_data(self.config.staleness_ttl_ms):
            self.stats.stale_data_rejections += 1
            return None

        # Calculate maker prices at mid-price with optional offset
        maker_prices = []
        for o in tradeable:
            if o.bba.best_bid is None or o.bba.best_ask is None:
                return None

            mid = (o.bba.best_bid + o.bba.best_ask) / 2
            # Apply offset (positive offset = more aggressive = closer to ask)
            offset = self.config.maker_price_offset_bps / 10000.0
            maker_price = mid + offset
            # Cap at best_ask (don't pay more than crossing)
            maker_price = min(maker_price, o.bba.best_ask)
            # Round to 2 decimal places (Polymarket uses cents)
            maker_prices.append(round(maker_price, 2))

        sum_of_prices = sum(maker_prices)
        num_legs = len(tradeable)
        gross_edge = 1.0 - sum_of_prices

        # Check liquidity from tradeable outcomes only
        ask_sizes = [o.bba.ask_size for o in tradeable if o.bba.ask_size is not None]
        if not ask_sizes:
            self.stats.liquidity_rejections += 1
            return None

        min_liquidity = min(ask_sizes)
        if min_liquidity < self.config.min_liquidity_per_outcome:
            self.stats.liquidity_rejections += 1
            return None

        # Calculate sizing
        max_size_liquidity = min_liquidity
        max_size_risk = self.config.max_position_per_event / sum_of_prices if sum_of_prices > 0 else 0
        max_size = min(max_size_liquidity, max_size_risk)
        suggested_size = max_size * 0.8  # Use 80% of max for safety

        if suggested_size <= 0:
            return None

        # Gas per share (amortized over trade size)
        total_gas_cost = self.config.gas_per_leg * num_legs
        gas_per_share = total_gas_cost / suggested_size if suggested_size > 0 else total_gas_cost

        # Maker fee = 0 on Polymarket
        fee_per_share = 0.0

        # Net edge (all per-share metrics)
        net_edge = gross_edge - fee_per_share - gas_per_share

        # Track candidate for diagnostics
        self._last_scan_candidates.append({
            "title": event.title[:60],
            "direction": "BUY(maker)",
            "legs": num_legs,
            "sum_prices": round(sum_of_prices, 4),
            "gross_edge": round(gross_edge, 4),
            "fee": round(fee_per_share, 4),
            "gas_per_share": round(gas_per_share, 6),
            "net_edge": round(net_edge, 4),
            "min_liq": round(min_liquidity, 0),
            "size": round(suggested_size, 0),
        })

        # Check minimum net edge (use maker threshold)
        if net_edge < self.config.maker_min_net_edge:
            self.stats.edge_too_low_rejections += 1
            if gross_edge > 0:
                logger.debug(
                    f"MAKER_EDGE_REJECT: {event.title[:60]} | legs={num_legs} | "
                    f"sum_prices={sum_of_prices:.4f} | gross={gross_edge:.4f} ({gross_edge*100:.2f}%) | "
                    f"fee={fee_per_share:.4f} | gas/sh={gas_per_share:.6f} | "
                    f"net={net_edge:.4f} ({net_edge*100:.2f}%) | "
                    f"min_liq={min_liquidity:.0f} | size={suggested_size:.0f}"
                )
            return None

        # Check cooldown to avoid spam
        cooldown_key = event.event_id
        if cooldown_key in self._opportunity_cooldown:
            if datetime.utcnow() < self._opportunity_cooldown[cooldown_key]:
                return None

        self._opportunity_cooldown[cooldown_key] = datetime.utcnow() + timedelta(seconds=2)

        # Build leg specifications with maker prices
        legs = []
        for i, outcome in enumerate(tradeable):
            leg = {
                "token_id": outcome.token_id,
                "market_id": outcome.market_id,
                "outcome_name": outcome.name,
                "side": "BUY",
                "price": maker_prices[i],
                "size": suggested_size,
                "order_type": "maker",  # Flag for execution engine
            }
            legs.append(leg)

        # Create opportunity
        opportunity = NegriskOpportunity(
            opportunity_id=f"negrisk_maker_{uuid.uuid4().hex[:12]}",
            event=event,
            direction=ArbDirection.BUY_ALL,
            sum_of_prices=sum_of_prices,
            gross_edge=gross_edge,
            net_edge=net_edge,
            suggested_size=suggested_size,
            max_size=max_size,
            legs=legs,
            detected_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(seconds=5),  # 5s expiry
        )

        # Track stats
        self.stats.opportunities_detected += 1
        if net_edge > self.stats.best_edge_seen:
            self.stats.best_edge_seen = net_edge
            self.stats.best_edge_event = event.title

        # Cache opportunity
        self._recent_opportunities[opportunity.opportunity_id] = opportunity

        logger.info(
            f"BUY-ALL(maker) opportunity: {event.title[:40]} | "
            f"sum_prices={sum_of_prices:.4f} | gross={gross_edge:.4f} | "
            f"fees={fee_per_share:.4f} | gas/share={gas_per_share:.6f} | "
            f"NET edge={net_edge:.4f} | legs={num_legs} | size={suggested_size:.2f}"
        )

        return opportunity

    def _check_event_sell_side(self, event: NegriskEvent, detection_start: Optional[float] = None) -> Optional[NegriskOpportunity]:
        """
        Check a single event for sell-side arbitrage opportunity.

        If sum_of_bids > $1.00 + fees + gas, selling YES on all outcomes
        guarantees profit. You receive sum_of_bids upfront and pay $1.00
        when one outcome resolves.

        Args:
            event: The event to check
            detection_start: Timestamp from time.monotonic() for latency tracking
        """
        # Get sell-tradeable outcomes (requires bid price and bid liquidity)
        tradeable = [o for o in event.outcomes if o.is_tradeable_sell_side(self.config)]

        if len(tradeable) < self.config.min_outcomes:
            return None

        if len(tradeable) > self.config.max_legs:
            return None

        # Check for stale data
        if event.has_stale_data(self.config.staleness_ttl_ms):
            # Already counted in buy-side, don't double-count
            return None

        # Calculate sum of bids from tradeable outcomes
        bids = [o.bba.best_bid for o in tradeable]
        if None in bids or len(bids) == 0:
            return None
        sum_of_bids = sum(bids)

        # Gross edge: how much bids exceed $1.00
        num_legs = len(tradeable)
        gross_edge = sum_of_bids - 1.0

        # Check bid-side liquidity
        bid_sizes = [o.bba.bid_size for o in tradeable if o.bba.bid_size is not None]
        if not bid_sizes:
            self.stats.liquidity_rejections += 1
            # Diagnostic: log sell-side phantom rejects
            all_priced = [o for o in event.outcomes
                         if o.status not in (OutcomeStatus.RESOLVED, OutcomeStatus.PLACEHOLDER)
                         and o.bba.best_bid is not None]
            if all_priced:
                clob_priced = [o for o in all_priced if o.bba.source in ("clob", "websocket")]
                gamma_only = [o for o in all_priced if o.bba.source == "gamma"]
                full_sum = sum(o.bba.best_bid for o in all_priced)
                full_gross = full_sum - 1.0
                if full_gross > 0 or (clob_priced and gamma_only):
                    now = datetime.utcnow()
                    cooldown_key = f"sell_{event.event_id}"
                    cooldown = self._liq_reject_log_cooldown.get(cooldown_key)
                    if not cooldown or now > cooldown:
                        self._liq_reject_log_cooldown[cooldown_key] = now + timedelta(seconds=60)
                        label = "PHANTOM" if gamma_only else "REAL"
                        logger.info(
                            f"SELL_LIQ_REJECT({label}): {event.title[:50]} | "
                            f"outcomes={len(all_priced)} (clob={len(clob_priced)}, gamma={len(gamma_only)}) | "
                            f"sum_bids={full_sum:.4f} | gross_edge={full_gross:.4f} ({full_gross*100:.2f}%)"
                        )
            return None

        min_liquidity = min(bid_sizes)
        if min_liquidity < self.config.min_liquidity_per_outcome:
            self.stats.liquidity_rejections += 1
            if gross_edge > 0:
                now = datetime.utcnow()
                cooldown_key = f"sell_{event.event_id}"
                cooldown = self._liq_reject_log_cooldown.get(cooldown_key)
                if not cooldown or now > cooldown:
                    self._liq_reject_log_cooldown[cooldown_key] = now + timedelta(seconds=60)
                    logger.info(
                        f"SELL_LIQ_REJECT: {event.title[:60]} | legs={num_legs} | "
                        f"sum_bids={sum_of_bids:.4f} | gross_edge={gross_edge:.4f} ({gross_edge*100:.2f}%) | "
                        f"min_bid_size={min_liquidity:.1f}"
                    )
            return None

        # Calculate sizing
        max_size_liquidity = min_liquidity
        max_size_risk = self.config.max_position_per_event / sum_of_bids if sum_of_bids > 0 else 0

        max_size = min(max_size_liquidity, max_size_risk)
        suggested_size = max_size * 0.8  # 80% of max for safety

        if suggested_size <= 0:
            return None

        # Apply depth scanning if enabled and depth data is available
        depth_adjusted = False
        if self.config.use_depth_scanning:
            has_depth = all(len(o.bba.bid_levels) > 0 for o in tradeable)

            if has_depth:
                # Recalculate prices using depth
                depth_adjusted_prices = []
                depth_max_sizes = []

                for outcome in tradeable:
                    avg_price, max_fill = _walk_book(outcome.bba.bid_levels, suggested_size)
                    if max_fill < suggested_size:
                        # Not enough depth — reduce size to what's available
                        suggested_size = max_fill * 0.8
                    depth_adjusted_prices.append(avg_price)
                    depth_max_sizes.append(max_fill)

                if suggested_size > 0:
                    # Recalculate all metrics with depth-adjusted prices
                    sum_of_bids = sum(depth_adjusted_prices)
                    gross_edge = sum_of_bids - 1.0
                    bids = depth_adjusted_prices  # Use depth prices for fee calculation
                    depth_adjusted = True

        # Gas per share (amortized)
        total_gas_cost = self.config.gas_per_leg * num_legs
        gas_per_share = total_gas_cost / suggested_size if suggested_size > 0 else total_gas_cost

        # Fee per share using Polymarket's on-chain formula:
        # SELL: fee = (fee_rate_bps / 10000) * min(p, 1-p) per leg
        # Most neg-risk markets are fee-free (fee_rate_bps=0).
        fee_per_share = _compute_fee_per_share(self.config.fee_rate_bps, bids, "SELL")

        # Net edge: proceeds - payout - fees - gas
        net_edge = gross_edge - fee_per_share - gas_per_share

        # Track candidate for diagnostics
        self._last_scan_candidates.append({
            "title": event.title[:60],
            "direction": "SELL",
            "legs": num_legs,
            "sum_prices": round(sum_of_bids, 4),
            "gross_edge": round(gross_edge, 4),
            "fee": round(fee_per_share, 4),
            "gas_per_share": round(gas_per_share, 6),
            "net_edge": round(net_edge, 4),
            "min_liq": round(min_liquidity, 0),
            "size": round(suggested_size, 0),
            "depth_adjusted": depth_adjusted,
        })

        # Check minimum net edge
        if net_edge < self.config.min_net_edge:
            self.stats.edge_too_low_rejections += 1
            if gross_edge > 0:
                logger.debug(
                    f"SELL_EDGE_REJECT: {event.title[:60]} | legs={num_legs} | "
                    f"sum_bids={sum_of_bids:.4f} | gross={gross_edge:.4f} ({gross_edge*100:.2f}%) | "
                    f"fee={fee_per_share:.4f} | gas/sh={gas_per_share:.6f} | "
                    f"net={net_edge:.4f} ({net_edge*100:.2f}%) | "
                    f"min_liq={min_liquidity:.0f} | size={suggested_size:.0f}"
                )
            return None

        # Check cooldown
        cooldown_key = f"sell_{event.event_id}"
        if cooldown_key in self._opportunity_cooldown:
            if datetime.utcnow() < self._opportunity_cooldown[cooldown_key]:
                return None

        self._opportunity_cooldown[cooldown_key] = datetime.utcnow() + timedelta(seconds=2)

        # Build sell-side leg specifications
        legs = []
        for outcome in tradeable:
            leg = {
                "token_id": outcome.token_id,
                "market_id": outcome.market_id,
                "outcome_name": outcome.name,
                "side": "SELL",
                "price": outcome.bba.best_bid,
                "size": suggested_size,
            }
            legs.append(leg)

        # Create opportunity
        opportunity = NegriskOpportunity(
            opportunity_id=f"negrisk_sell_{uuid.uuid4().hex[:12]}",
            event=event,
            direction=ArbDirection.SELL_ALL,
            sum_of_prices=sum_of_bids,
            gross_edge=gross_edge,
            net_edge=net_edge,
            suggested_size=suggested_size,
            max_size=max_size,
            legs=legs,
            detected_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(seconds=5),
        )

        # Track detection latency if enabled
        if detection_start is not None and self.config.detection_latency_tracking:
            latency_ms = (time.monotonic() - detection_start) * 1000
            opportunity.detection_latency_ms = latency_ms
            # Update stats
            self.stats.total_detections_timed += 1
            self.stats.max_detection_latency_ms = max(self.stats.max_detection_latency_ms, latency_ms)
            self.stats.min_detection_latency_ms = min(self.stats.min_detection_latency_ms, latency_ms)
            # Running average
            n = self.stats.total_detections_timed
            self.stats.avg_detection_latency_ms = (
                self.stats.avg_detection_latency_ms * (n - 1) + latency_ms
            ) / n

        # Track stats
        self.stats.opportunities_detected += 1
        if net_edge > self.stats.best_edge_seen:
            self.stats.best_edge_seen = net_edge
            self.stats.best_edge_event = f"[SELL] {event.title}"

        # Cache opportunity
        self._recent_opportunities[opportunity.opportunity_id] = opportunity

        logger.info(
            f"SELL-ALL opportunity: {event.title[:40]} | "
            f"sum_bids={sum_of_bids:.4f} | gross={gross_edge:.4f} | "
            f"fees={fee_per_share:.4f} | gas/share={gas_per_share:.6f} | "
            f"NET edge={net_edge:.4f} | legs={num_legs} | size={suggested_size:.2f}"
        )

        return opportunity

    def _check_event_maker_sell_side(self, event: NegriskEvent) -> Optional[NegriskOpportunity]:
        """
        Check a single event for maker-mode sell-side opportunity.

        Prices at mid-price (between bid and ask) instead of crossing the spread.
        Maker orders pay 0% fee on Polymarket.
        """
        # Get sell-tradeable outcomes (requires bid price and bid liquidity)
        tradeable = [o for o in event.outcomes if o.is_tradeable_sell_side(self.config)]

        if len(tradeable) < self.config.min_outcomes:
            return None

        if len(tradeable) > self.config.max_legs:
            return None

        # Check for stale data
        if event.has_stale_data(self.config.staleness_ttl_ms):
            # Already counted in buy-side, don't double-count
            return None

        # Calculate maker prices at mid-price with optional offset
        maker_prices = []
        for o in tradeable:
            if o.bba.best_bid is None or o.bba.best_ask is None:
                return None

            mid = (o.bba.best_bid + o.bba.best_ask) / 2
            # Apply offset (negative offset = more aggressive = closer to bid)
            offset = self.config.maker_price_offset_bps / 10000.0
            maker_price = mid - offset
            # Cap at best_bid (don't sell for less than crossing)
            maker_price = max(maker_price, o.bba.best_bid)
            # Round to 2 decimal places (Polymarket uses cents)
            maker_prices.append(round(maker_price, 2))

        sum_of_prices = sum(maker_prices)
        num_legs = len(tradeable)
        # Gross edge: how much bids exceed $1.00
        gross_edge = sum_of_prices - 1.0

        # Check bid-side liquidity
        bid_sizes = [o.bba.bid_size for o in tradeable if o.bba.bid_size is not None]
        if not bid_sizes:
            self.stats.liquidity_rejections += 1
            return None

        min_liquidity = min(bid_sizes)
        if min_liquidity < self.config.min_liquidity_per_outcome:
            self.stats.liquidity_rejections += 1
            return None

        # Calculate sizing
        max_size_liquidity = min_liquidity
        max_size_risk = self.config.max_position_per_event / sum_of_prices if sum_of_prices > 0 else 0
        max_size = min(max_size_liquidity, max_size_risk)
        suggested_size = max_size * 0.8  # 80% of max for safety

        if suggested_size <= 0:
            return None

        # Gas per share (amortized)
        total_gas_cost = self.config.gas_per_leg * num_legs
        gas_per_share = total_gas_cost / suggested_size if suggested_size > 0 else total_gas_cost

        # Maker fee = 0 on Polymarket
        fee_per_share = 0.0

        # Net edge: proceeds - payout - fees - gas
        net_edge = gross_edge - fee_per_share - gas_per_share

        # Track candidate for diagnostics
        self._last_scan_candidates.append({
            "title": event.title[:60],
            "direction": "SELL(maker)",
            "legs": num_legs,
            "sum_prices": round(sum_of_prices, 4),
            "gross_edge": round(gross_edge, 4),
            "fee": round(fee_per_share, 4),
            "gas_per_share": round(gas_per_share, 6),
            "net_edge": round(net_edge, 4),
            "min_liq": round(min_liquidity, 0),
            "size": round(suggested_size, 0),
        })

        # Check minimum net edge (use maker threshold)
        if net_edge < self.config.maker_min_net_edge:
            self.stats.edge_too_low_rejections += 1
            if gross_edge > 0:
                logger.debug(
                    f"SELL_MAKER_EDGE_REJECT: {event.title[:60]} | legs={num_legs} | "
                    f"sum_prices={sum_of_prices:.4f} | gross={gross_edge:.4f} ({gross_edge*100:.2f}%) | "
                    f"fee={fee_per_share:.4f} | gas/sh={gas_per_share:.6f} | "
                    f"net={net_edge:.4f} ({net_edge*100:.2f}%) | "
                    f"min_liq={min_liquidity:.0f} | size={suggested_size:.0f}"
                )
            return None

        # Check cooldown
        cooldown_key = f"sell_{event.event_id}"
        if cooldown_key in self._opportunity_cooldown:
            if datetime.utcnow() < self._opportunity_cooldown[cooldown_key]:
                return None

        self._opportunity_cooldown[cooldown_key] = datetime.utcnow() + timedelta(seconds=2)

        # Build sell-side leg specifications with maker prices
        legs = []
        for i, outcome in enumerate(tradeable):
            leg = {
                "token_id": outcome.token_id,
                "market_id": outcome.market_id,
                "outcome_name": outcome.name,
                "side": "SELL",
                "price": maker_prices[i],
                "size": suggested_size,
                "order_type": "maker",  # Flag for execution engine
            }
            legs.append(leg)

        # Create opportunity
        opportunity = NegriskOpportunity(
            opportunity_id=f"negrisk_maker_sell_{uuid.uuid4().hex[:12]}",
            event=event,
            direction=ArbDirection.SELL_ALL,
            sum_of_prices=sum_of_prices,
            gross_edge=gross_edge,
            net_edge=net_edge,
            suggested_size=suggested_size,
            max_size=max_size,
            legs=legs,
            detected_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(seconds=5),
        )

        # Track stats
        self.stats.opportunities_detected += 1
        if net_edge > self.stats.best_edge_seen:
            self.stats.best_edge_seen = net_edge
            self.stats.best_edge_event = f"[SELL-MAKER] {event.title}"

        # Cache opportunity
        self._recent_opportunities[opportunity.opportunity_id] = opportunity

        logger.info(
            f"SELL-ALL(maker) opportunity: {event.title[:40]} | "
            f"sum_prices={sum_of_prices:.4f} | gross={gross_edge:.4f} | "
            f"fees={fee_per_share:.4f} | gas/share={gas_per_share:.6f} | "
            f"NET edge={net_edge:.4f} | legs={num_legs} | size={suggested_size:.2f}"
        )

        return opportunity

    def validate_opportunity(self, opportunity: NegriskOpportunity) -> bool:
        """
        Validate an opportunity before execution.

        Re-checks all conditions to ensure opportunity is still valid.
        Supports both BUY_ALL and SELL_ALL directions.
        """
        if not opportunity.is_valid(self.config):
            return False

        # Re-check with fresh data
        event = opportunity.event
        is_sell = opportunity.direction == ArbDirection.SELL_ALL

        # Stale check
        if event.has_stale_data(self.config.staleness_ttl_ms):
            logger.warning(f"Opportunity {opportunity.opportunity_id} rejected: stale data")
            return False

        if is_sell:
            tradeable = [o for o in event.outcomes if o.is_tradeable_sell_side(self.config)]
            prices = [o.bba.best_bid for o in tradeable]
        else:
            tradeable = [o for o in event.outcomes if o.is_tradeable(self.config)]
            prices = [o.bba.best_ask for o in tradeable]

        if None in prices or len(prices) == 0:
            logger.warning(f"Opportunity {opportunity.opportunity_id} rejected: missing prices")
            return False

        sum_of_prices = sum(prices)
        num_legs = len(tradeable)

        # Amortize gas over trade size
        total_gas_cost = self.config.gas_per_leg * num_legs
        gas_per_share = total_gas_cost / opportunity.suggested_size if opportunity.suggested_size > 0 else total_gas_cost

        # Fee per share using Polymarket's on-chain formula
        side = "SELL" if is_sell else "BUY"
        fee_per_share = _compute_fee_per_share(self.config.fee_rate_bps, prices, side)

        # Net edge
        if is_sell:
            net_edge = sum_of_prices - 1.0 - fee_per_share - gas_per_share
        else:
            net_edge = 1.0 - sum_of_prices - fee_per_share - gas_per_share

        if net_edge < self.config.min_net_edge:
            logger.warning(
                f"Opportunity {opportunity.opportunity_id} rejected: "
                f"edge deteriorated to {net_edge:.4f}"
            )
            return False

        return True

    def get_recent_opportunities(self, max_age_seconds: float = 60.0) -> list[NegriskOpportunity]:
        """Get recently detected opportunities."""
        cutoff = datetime.utcnow() - timedelta(seconds=max_age_seconds)
        return [
            opp for opp in self._recent_opportunities.values()
            if opp.detected_at > cutoff
        ]

    def clear_expired_opportunities(self) -> int:
        """Clear expired opportunities from cache."""
        now = datetime.utcnow()
        expired = [
            opp_id for opp_id, opp in self._recent_opportunities.items()
            if opp.expires_at and opp.expires_at < now
        ]
        for opp_id in expired:
            del self._recent_opportunities[opp_id]
        return len(expired)

    def mark_executed(self, opportunity_id: str) -> None:
        """Mark an opportunity as executed."""
        if opportunity_id in self._recent_opportunities:
            opp = self._recent_opportunities[opportunity_id]
            opp.executed = True
            self.stats.opportunities_executed += 1
            self.stats.total_profit += opp.expected_profit
            self.stats.total_volume += opp.total_cost

    def get_last_scan_candidates(self) -> list[dict]:
        """Get top candidates from the last scan (sorted by gross_edge desc)."""
        return getattr(self, '_last_scan_candidates', [])

    def get_stats(self) -> NegriskStats:
        """Get detector statistics."""
        return self.stats

    def get_stats_dict(self) -> dict:
        """Get statistics as a dictionary."""
        return {
            "opportunities_detected": self.stats.opportunities_detected,
            "opportunities_submitted": self.stats.opportunities_submitted,
            "opportunities_executed": self.stats.opportunities_executed,
            "total_profit": round(self.stats.total_profit, 2),
            "total_volume": round(self.stats.total_volume, 2),
            "stale_data_rejections": self.stats.stale_data_rejections,
            "liquidity_rejections": self.stats.liquidity_rejections,
            "edge_too_low_rejections": self.stats.edge_too_low_rejections,
            "execution_failures": self.stats.execution_failures,
            "best_edge_seen": round(self.stats.best_edge_seen, 4),
            "best_edge_event": self.stats.best_edge_event,
            "recent_opportunities": len(self._recent_opportunities),
            "avg_detection_latency_ms": round(self.stats.avg_detection_latency_ms, 2),
            "min_detection_latency_ms": round(self.stats.min_detection_latency_ms, 2) if self.stats.min_detection_latency_ms != float('inf') else None,
            "max_detection_latency_ms": round(self.stats.max_detection_latency_ms, 2),
            "total_detections_timed": self.stats.total_detections_timed,
        }
