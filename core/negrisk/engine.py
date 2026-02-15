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
from datetime import datetime
from typing import Optional

from core.negrisk.bba_tracker import BBATracker
from core.negrisk.detector import NegriskDetector
from core.negrisk.models import NegriskConfig, NegriskEvent, NegriskOpportunity
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

        # Background tasks
        self._scan_task: Optional[asyncio.Task] = None
        self._running = False

        # Scan interval
        self._scan_interval = 1.0  # Scan every 1 second for opportunities

        # Track active event scans to prevent unbounded task spawning
        self._active_event_scans: set[str] = set()

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

        # Start opportunity scanner
        self._scan_task = asyncio.create_task(
            self._scan_loop(),
            name="negrisk_scanner"
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

        # Queue an immediate scan for this specific event
        asyncio.create_task(
            self._scan_event_for_opportunity(event_id),
            name=f"scan_event_{event_id}"
        )

    async def _scan_event_for_opportunity(self, event_id: str) -> None:
        """
        Scan a specific event for arbitrage opportunity.

        Called by price update callback for low-latency detection.
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

            # Detect opportunity
            opportunity = self.detector._check_event(event)
            if opportunity:
                await self._execute_opportunity(opportunity)

        except Exception as e:
            logger.debug(f"Event scan error for {event_id}: {e}")
        finally:
            # Always remove from active scans when done
            self._active_event_scans.discard(event_id)

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

    async def _scan_for_opportunities(self) -> None:
        """Scan all events for arbitrage opportunities."""
        # Get tradeable events
        events = self.registry.get_tradeable_events()

        if not events:
            return

        # Detect opportunities
        opportunities = self.detector.detect_opportunities(events)

        # Execute each opportunity
        for opportunity in opportunities:
            await self._execute_opportunity(opportunity)

    async def _execute_opportunity(self, opportunity: NegriskOpportunity) -> None:
        """
        Execute a neg-risk arbitrage opportunity.

        This involves:
        1. Validating the opportunity is still valid
        2. Fetching fresh prices from CLOB
        3. Creating a bundle signal with all legs
        4. Submitting to execution engine

        Note: We don't mark as "executed" here because submit_signal only queues.
        The opportunity should be marked executed when orders actually fill.
        """
        try:
            # Fetch fresh prices from CLOB for all outcomes
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

            logger.info(f"Neg-risk opportunity submitted: {opportunity.opportunity_id}")

        except Exception as e:
            logger.error(f"Failed to execute opportunity {opportunity.opportunity_id}: {e}")
            self.detector.stats.execution_failures += 1

    def _create_negrisk_signal(self, opportunity: NegriskOpportunity) -> Signal:
        """
        Create a trading signal for a neg-risk opportunity.

        This creates a bundle signal with BUY orders for all outcomes.
        Each order includes its specific market_id.
        """
        orders = []

        for leg in opportunity.legs:
            # CRITICAL FIX: Each leg must include its market_id
            # The execution engine needs this to route the order correctly
            order_spec = {
                "market_id": leg["market_id"],  # Per-outcome market ID
                "token_type": TokenType.YES,    # Each outcome has a YES token
                "side": OrderSide.BUY,
                "price": leg["price"],
                "size": leg["size"],
                "strategy_tag": "negrisk_arb",
            }
            orders.append(order_spec)

        # Create a standard Opportunity object for slippage checking
        # Use the first outcome's market as the primary market_id
        primary_market_id = opportunity.legs[0]["market_id"] if opportunity.legs else opportunity.event.event_id

        # Create a minimal Opportunity for slippage validation
        # We'll use the event data to populate the required fields
        from polymarket_client.models import Opportunity as StdOpportunity, OpportunityType

        std_opportunity = StdOpportunity(
            opportunity_id=opportunity.opportunity_id,
            opportunity_type=OpportunityType.BUNDLE_LONG,  # Buying all outcomes
            market_id=primary_market_id,
            edge=opportunity.net_edge,
            # For neg-risk, we don't have simple YES/NO bid/ask
            # Set to None to avoid stale slippage checks - we'll use fresh checks
            best_bid_yes=None,
            best_ask_yes=None,
            best_bid_no=None,
            best_ask_no=None,
            suggested_size=opportunity.suggested_size,
            max_size=opportunity.max_size,
            expires_at=opportunity.expires_at,
        )

        signal = Signal(
            signal_id=opportunity.opportunity_id,
            action="place_orders",
            market_id=primary_market_id,  # Primary market for signal
            opportunity=std_opportunity,  # Include opportunity for slippage checks
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
                    "sum_asks": round(opp.sum_of_asks, 4),
                    "net_edge": round(opp.net_edge, 4),
                    "legs": opp.num_legs,
                    "size": round(opp.suggested_size, 2),
                    "profit": round(opp.expected_profit, 2),
                    "detected": opp.detected_at.isoformat(),
                    "executed": opp.executed,
                }
                for opp in recent_opps[:10]
            ],
        }
