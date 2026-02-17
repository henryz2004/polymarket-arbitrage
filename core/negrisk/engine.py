"""
Negrisk Arbitrage Engine
=========================

Main orchestrator for neg-risk arbitrage operations.

Coordinates:
1. NegriskRegistry - discovers events
2. BBATracker - maintains real-time prices
3. NegriskDetector - finds opportunities
4. ExecutionEngine - executes trades
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

from core.negrisk.bba_tracker import BBATracker
from core.negrisk.binary_detector import BinaryBundleDetector, BinaryMarket
from core.negrisk.detector import NegriskDetector
from core.negrisk.models import ArbDirection, NegriskConfig, NegriskEvent, NegriskOpportunity
from core.negrisk.partial_detector import PartialPositionDetector
from core.negrisk.registry import NegriskRegistry
from core.execution import ExecutionEngine
from core.risk_manager import RiskManager
from polymarket_client.models import OrderSide, Signal, TokenType


logger = logging.getLogger(__name__)


class NegriskEngine:
    """
    Main neg-risk arbitrage engine.

    Orchestrates event discovery, price tracking, opportunity detection,
    and trade execution for neg-risk arbitrage.
    """

    def __init__(
        self,
        config: NegriskConfig,
        execution_engine: ExecutionEngine,
        risk_manager: RiskManager,
    ):
        """
        Initialize the neg-risk engine.

        Args:
            config: Neg-risk configuration
            execution_engine: Execution engine for order placement
            risk_manager: Risk manager for position limits
        """
        self.config = config
        self.execution_engine = execution_engine
        self.risk_manager = risk_manager

        # Core components
        self.registry = NegriskRegistry(config)
        self.tracker: Optional[BBATracker] = None
        self.detector = NegriskDetector(config)
        self.partial_detector = PartialPositionDetector(config)
        self.binary_detector = BinaryBundleDetector(config)

        # Background tasks
        self._scan_task: Optional[asyncio.Task] = None
        self._reseed_task: Optional[asyncio.Task] = None
        self._running = False

        # Scan interval
        self._scan_interval = 1.0  # Scan every 1 second for opportunities

        # Track active event scans to prevent unbounded task spawning
        self._active_event_scans: set[str] = set()

        # Per-event execution cooldown to prevent double-execution
        self._execution_cooldown: dict[str, datetime] = {}

        logger.info("NegriskEngine initialized")

    async def start(self) -> None:
        """Start the neg-risk arbitrage engine."""
        if self._running:
            return

        self._running = True

        # Start registry
        await self.registry.start()

        # Wait a bit for initial registry fetch
        await asyncio.sleep(2)

        # Start BBA tracker with price update callback
        self.tracker = BBATracker(
            registry=self.registry,
            config=self.config,
            on_price_update=self._on_price_update,
        )
        await self.tracker.start()

        # Seed initial CLOB data for all tracked tokens
        await self._seed_bba_data()

        # Adjust scan interval for ws_only_mode
        if self.config.ws_only_mode:
            self._scan_interval = 30.0  # 30s safety fallback
            logger.info("WS-only mode: primary detection via WebSocket callbacks")

        # Start opportunity scanner
        self._scan_task = asyncio.create_task(
            self._scan_loop(),
            name="negrisk_scanner"
        )

        # Start periodic re-seeding for gamma-only tokens
        self._reseed_task = asyncio.create_task(
            self._reseed_loop(),
            name="negrisk_reseed"
        )

        logger.info("NegriskEngine started")

    async def stop(self) -> None:
        """Stop the neg-risk arbitrage engine."""
        self._running = False

        if self._scan_task:
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass

        if self._reseed_task:
            self._reseed_task.cancel()
            try:
                await self._reseed_task
            except asyncio.CancelledError:
                pass

        if self.tracker:
            await self.tracker.stop()

        await self.registry.stop()

        logger.info("NegriskEngine stopped")

    def _on_price_update(self, event_id: str, token_id: str) -> None:
        """
        Callback when a price updates.

        PERFORMANCE FIX: Trigger immediate opportunity scan for the event
        instead of waiting for the periodic scan. This reduces latency.

        DEDUPLICATION: Only spawn one scan task per event at a time to
        prevent unbounded task creation on high-frequency price updates.
        """
        # Don't spawn a new task if one is already running for this event
        if event_id in self._active_event_scans:
            return

        # Record timestamp for latency tracking
        detection_start = time.monotonic()

        # Queue an immediate scan for this specific event
        asyncio.create_task(
            self._scan_event_for_opportunity(event_id, detection_start=detection_start),
            name=f"scan_event_{event_id}"
        )

    async def _scan_event_for_opportunity(self, event_id: str, detection_start: Optional[float] = None) -> None:
        """
        Scan a specific event for arbitrage opportunity (buy-side and sell-side).

        Called by price update callback for low-latency detection.

        Args:
            event_id: The event to scan
            detection_start: Timestamp from time.monotonic() for latency tracking
        """
        # Mark this event as being scanned
        self._active_event_scans.add(event_id)

        try:
            # Get the event
            event = self.registry.get_event(event_id)
            if not event:
                return

            # Check if tradeable
            if not self._is_event_tradeable(event):
                return

            # Detect opportunities based on order strategy
            if self.config.order_strategy == "maker":
                buy_opp = self.detector._check_event_maker(event)
                sell_opp = self.detector._check_event_maker_sell_side(event)
            else:
                buy_opp = self.detector._check_event(event, detection_start=detection_start)
                sell_opp = self.detector._check_event_sell_side(event, detection_start=detection_start)

            if buy_opp:
                await self._execute_opportunity(buy_opp)
            if sell_opp:
                await self._execute_opportunity(sell_opp)

            # If no riskless arb found, check for +EV partial positions
            if not buy_opp and not sell_opp and self.config.enable_partial_positions:
                partial_opp = self.partial_detector.check_event(event)
                if partial_opp:
                    await self._execute_opportunity(partial_opp)

        except Exception as e:
            logger.debug(f"Event scan error for {event_id}: {e}")
        finally:
            # Keep event in active scans for 500ms after completion
            # to prevent immediate re-scan from the next WS tick
            async def _clear_after_delay(eid: str = event_id) -> None:
                await asyncio.sleep(0.5)
                self._active_event_scans.discard(eid)
            asyncio.create_task(_clear_after_delay())

    def _is_event_tradeable(self, event: NegriskEvent) -> bool:
        """Check if an event meets basic tradability criteria."""
        # Check outcome count
        tradeable = [o for o in event.outcomes if o.is_tradeable(self.config)]
        if len(tradeable) < self.config.min_outcomes:
            return False
        if len(tradeable) > self.config.max_legs:
            return False

        # Check volume
        if event.volume_24h < self.config.min_event_volume_24h:
            return False

        return True

    def _event_to_binary_market(self, event: NegriskEvent) -> Optional[BinaryMarket]:
        """Convert a 2-outcome NegriskEvent to a BinaryMarket for binary bundle detection."""
        tradeable = [o for o in event.outcomes if o.is_tradeable(self.config)]
        if len(tradeable) != 2:
            return None

        return BinaryMarket(
            market_id=event.event_id,
            question=event.title,
            yes_token_id=tradeable[0].token_id,
            no_token_id=tradeable[1].token_id,
            yes_bba=tradeable[0].bba,
            no_bba=tradeable[1].bba,
            volume_24h=event.volume_24h,
            fee_rate_bps=self.config.taker_fee_bps,
        )

    async def _scan_loop(self) -> None:
        """Main scanning loop - periodically check for opportunities."""
        while self._running:
            try:
                await self._scan_for_opportunities()
                await asyncio.sleep(self._scan_interval)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Scan loop error: {e}")
                await asyncio.sleep(5)

    async def _seed_bba_data(self) -> None:
        """
        Seed initial CLOB data for all tracked tokens.

        Fetches order books from CLOB API in batches, sorted by event volume
        (highest first) for fastest coverage of high-value events.
        """
        if not self.tracker:
            return

        events = self.registry.get_tradeable_events()
        sorted_events = sorted(events, key=lambda e: e.volume_24h, reverse=True)

        if not sorted_events:
            logger.warning("No tradeable events found for CLOB seeding")
            return

        total_tokens = sum(len([o for o in e.active_outcomes if o.token_id]) for e in sorted_events)
        logger.info(f"Seeding CLOB data for {len(sorted_events)} events ({total_tokens} tokens)")

        batch_size = 10
        total_seeded = 0
        total_empty = 0
        total_failed = 0

        for i in range(0, len(sorted_events), batch_size):
            batch = sorted_events[i:i + batch_size]

            for event in batch:
                try:
                    stats = await self.tracker.fetch_all_prices(event)
                    total_seeded += stats["seeded"]
                    total_empty += stats["empty"]
                    total_failed += stats["failed"]
                except Exception as e:
                    logger.debug(f"CLOB seed error for {event.event_id}: {e}")

            # Rate limiting between batches
            if i + batch_size < len(sorted_events):
                await asyncio.sleep(0.5)

        logger.info(
            f"CLOB seeding complete: {total_seeded} tokens with books, "
            f"{total_empty} empty, {total_failed} failed"
        )

    async def _reseed_loop(self) -> None:
        """Periodically re-seed CLOB data for tokens with gamma-only prices."""
        # Wait for initial seeding to settle
        await asyncio.sleep(30)

        while self._running:
            try:
                if self.tracker:
                    stats = await self.tracker.reseed_gamma_tokens()
                    if stats["checked"] > 0:
                        logger.info(
                            f"Re-seed: checked={stats['checked']}, "
                            f"reseeded={stats['reseeded']}, "
                            f"still_empty={stats['still_empty']}, "
                            f"failed={stats['failed']}"
                        )
                await asyncio.sleep(self.config.reseed_interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Re-seed loop error: {e}")
                await asyncio.sleep(30)

    async def _scan_for_opportunities(self) -> None:
        """Scan all events for arbitrage opportunities."""
        # Get tradeable events
        events = self.registry.get_tradeable_events()

        if not events:
            return

        # Sort by priority (highest first) for faster detection of high-value opportunities
        if self.config.prioritize_near_resolution:
            events.sort(key=lambda e: e.priority_score, reverse=True)

        # Detect opportunities using configured strategy
        opportunities = self.detector.detect_opportunities(
            events,
            strategy=self.config.order_strategy
        )

        # Execute each opportunity
        for opportunity in opportunities:
            await self._execute_opportunity(opportunity)

        # Check binary markets (2-outcome events) for YES+NO bundle arb
        if self.config.binary_bundle_enabled:
            for event in events:
                binary_market = self._event_to_binary_market(event)
                if binary_market:
                    buy_opp = self.binary_detector.check_market_buy(binary_market)
                    if buy_opp:
                        await self._execute_opportunity(buy_opp)
                    sell_opp = self.binary_detector.check_market_sell(binary_market)
                    if sell_opp:
                        await self._execute_opportunity(sell_opp)

        # Check for +EV partial positions on events with no riskless arb
        if self.config.enable_partial_positions:
            arb_event_ids = {opp.event.event_id for opp in opportunities}
            for event in events:
                if event.event_id not in arb_event_ids:
                    partial_opp = self.partial_detector.check_event(event)
                    if partial_opp:
                        await self._execute_opportunity(partial_opp)

    async def _execute_opportunity(self, opportunity: NegriskOpportunity) -> None:
        """
        Execute a neg-risk arbitrage opportunity.

        This involves:
        1. Validating the opportunity is still valid
        2. Fetching fresh prices from CLOB (unless ws_only_mode)
        3. Creating a bundle signal with all legs
        4. Submitting to execution engine

        Note: We don't mark as "executed" here because submit_signal only queues.
        The opportunity should be marked executed when orders actually fill.
        """
        try:
            # Per-event execution cooldown to prevent double-execution
            cooldown_key = f"{opportunity.event.event_id}_{opportunity.direction.value}"
            now = datetime.utcnow()
            if cooldown_key in self._execution_cooldown:
                if now < self._execution_cooldown[cooldown_key]:
                    logger.debug(f"Execution cooldown active for {cooldown_key}")
                    return
            self._execution_cooldown[cooldown_key] = now + timedelta(seconds=5)

            if self.config.ws_only_mode:
                # WS-only mode: skip CLOB fetch but STILL validate
                if self.tracker and not self.tracker.ws_connected:
                    logger.warning(f"WS disconnected, skipping execution of {opportunity.opportunity_id}")
                    return

                if not self.detector.validate_opportunity(opportunity):
                    logger.debug(f"WS-only: opportunity {opportunity.opportunity_id} failed validation")
                    return

                # Warn if any outcome has gamma-sourced data (not WebSocket-confirmed)
                for outcome in opportunity.event.active_outcomes:
                    if outcome.bba.source == "gamma":
                        logger.warning(
                            f"WS-only mode: outcome {outcome.name} has gamma-sourced BBA, "
                            f"not WebSocket-confirmed"
                        )
            else:
                # Standard mode: fetch fresh prices from CLOB for all outcomes
                if self.tracker:
                    await self.tracker.fetch_all_prices(opportunity.event)

                # Re-validate with fresh data
                if not self.detector.validate_opportunity(opportunity):
                    logger.debug(f"Opportunity {opportunity.opportunity_id} failed validation")
                    return

            # Create bundle signal
            signal = self._create_negrisk_signal(opportunity)

            # Submit to execution engine (queues signal, doesn't execute immediately)
            await self.execution_engine.submit_signal(signal)

            # Track submission (not execution - that requires fill confirmation)
            self.detector.stats.opportunities_submitted += 1

            # CRITICAL FIX: Don't mark as executed yet - that should happen
            # when the orders are actually filled.
            # TODO: Add callback mechanism from execution engine to mark executed on fill.

            # Log maker orders differently (passive, not immediate fill)
            if any(leg.get("order_type") == "maker" for leg in opportunity.legs):
                logger.info(f"MAKER opportunity submitted (passive): {opportunity.opportunity_id}")
            else:
                logger.info(f"Neg-risk opportunity submitted: {opportunity.opportunity_id}")

        except Exception as e:
            logger.error(f"Failed to execute opportunity {opportunity.opportunity_id}: {e}")
            self.detector.stats.execution_failures += 1

    def _create_negrisk_signal(self, opportunity: NegriskOpportunity) -> Signal:
        """
        Create a trading signal for a neg-risk opportunity.

        Supports both BUY_ALL and SELL_ALL directions.
        Each order includes its specific market_id and side from the leg.
        """
        is_sell = opportunity.direction == ArbDirection.SELL_ALL
        orders = []

        for leg in opportunity.legs:
            # Read side from leg - supports both BUY and SELL
            leg_side = OrderSide.SELL if leg["side"] == "SELL" else OrderSide.BUY

            order_spec = {
                "market_id": leg["market_id"],  # Per-outcome market ID
                "token_type": TokenType.YES,    # Each outcome has a YES token
                "side": leg_side,
                "price": leg["price"],
                "size": leg["size"],
                "strategy_tag": "negrisk_arb",
            }
            orders.append(order_spec)

        # Create a standard Opportunity object for slippage checking
        primary_market_id = opportunity.legs[0]["market_id"] if opportunity.legs else opportunity.event.event_id

        from polymarket_client.models import Opportunity as StdOpportunity, OpportunityType

        # SELL_ALL is conceptually a BUNDLE_SHORT (selling all outcomes)
        opp_type = OpportunityType.BUNDLE_SHORT if is_sell else OpportunityType.BUNDLE_LONG

        std_opportunity = StdOpportunity(
            opportunity_id=opportunity.opportunity_id,
            opportunity_type=opp_type,
            market_id=primary_market_id,
            edge=opportunity.net_edge,
            best_bid_yes=None,
            best_ask_yes=None,
            best_bid_no=None,
            best_ask_no=None,
            suggested_size=opportunity.suggested_size,
            max_size=opportunity.max_size,
            expires_at=opportunity.expires_at,
        )

        direction_label = "SELL-ALL" if is_sell else "BUY-ALL"
        signal = Signal(
            signal_id=opportunity.opportunity_id,
            action="place_orders",
            market_id=primary_market_id,
            opportunity=std_opportunity,
            orders=orders,
            priority=15,  # Highest priority - neg-risk arb is time-sensitive
        )

        return signal

    def get_active_events(self) -> list:
        """Get all active neg-risk events."""
        return self.registry.get_tradeable_events()

    def get_recent_opportunities(self) -> list[NegriskOpportunity]:
        """Get recently detected opportunities."""
        return self.detector.get_recent_opportunities()

    def get_stats(self) -> dict:
        """Get comprehensive statistics."""
        registry_stats = self.registry.get_stats()
        detector_stats = self.detector.get_stats_dict()
        tracker_stats = self.tracker.get_stats() if self.tracker else {}

        return {
            "registry": registry_stats,
            "detector": detector_stats,
            "tracker": tracker_stats,
            "engine": {
                "running": self._running,
                "scan_interval": self._scan_interval,
            },
        }

    def get_summary(self) -> dict:
        """Get a summary for dashboard display."""
        stats = self.get_stats()
        recent_opps = self.get_recent_opportunities()

        return {
            "events_tracked": stats["registry"]["events_tracked"],
            "opportunities_detected": stats["detector"]["opportunities_detected"],
            "opportunities_submitted": stats["detector"]["opportunities_submitted"],
            "opportunities_executed": stats["detector"]["opportunities_executed"],
            "total_profit": stats["detector"]["total_profit"],
            "best_edge_seen": stats["detector"]["best_edge_seen"],
            "best_edge_event": stats["detector"]["best_edge_event"],
            "recent_opportunities": [
                {
                    "event": opp.event.title,
                    "direction": opp.direction.value,
                    "sum_prices": round(opp.sum_of_prices, 4),
                    "net_edge": round(opp.net_edge, 4),
                    "legs": opp.num_legs,
                    "size": round(opp.suggested_size, 2),
                    "profit": round(opp.expected_profit, 2),
                    "detected": opp.detected_at.isoformat(),
                    "executed": opp.executed,
                    "priority": round(opp.event.priority_score, 3),
                    "hours_to_resolution": round(opp.event.hours_to_resolution, 1) if opp.event.hours_to_resolution is not None else None,
                }
                for opp in recent_opps[:10]
            ],
        }
