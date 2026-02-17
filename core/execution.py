"""
Execution Engine Module
========================

Handles order placement, cancellation, and management.
Consumes signals from the ArbEngine and interfaces with the API.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from polymarket_client.api import PolymarketClient
from polymarket_client.models import (
    Order,
    OrderBook,
    OrderSide,
    OrderStatus,
    Signal,
    TokenType,
    Trade,
)
from core.risk_manager import RiskManager
from core.portfolio import Portfolio


logger = logging.getLogger(__name__)


@dataclass
class ExecutionConfig:
    """Configuration for the execution engine."""
    slippage_tolerance: float = 0.02  # Max allowed price slippage
    order_timeout_seconds: float = 60.0  # Cancel unfilled orders after this time
    max_retries: int = 3
    retry_delay: float = 0.5
    enable_slippage_check: bool = True
    dry_run: bool = True


@dataclass
class ExecutionStats:
    """Statistics for the execution engine."""
    orders_placed: int = 0
    orders_filled: int = 0
    orders_cancelled: int = 0
    orders_rejected: int = 0
    total_notional: float = 0.0
    signals_processed: int = 0
    signals_rejected: int = 0
    slippage_rejections: int = 0


class ExecutionEngine:
    """
    Order execution engine.
    
    Consumes trading signals and places/manages orders through the
    Polymarket API. Enforces risk limits and handles slippage checks.
    """
    
    def __init__(
        self,
        client: PolymarketClient,
        risk_manager: RiskManager,
        portfolio: Portfolio,
        config: ExecutionConfig,
    ):
        self.client = client
        self.risk_manager = risk_manager
        self.portfolio = portfolio
        self.config = config
        self.stats = ExecutionStats()
        
        # Track open orders
        self._open_orders: dict[str, Order] = {}
        self._order_timestamps: dict[str, datetime] = {}
        
        # Order tracking by market and strategy
        self._orders_by_market: dict[str, list[str]] = {}
        self._orders_by_strategy: dict[str, list[str]] = {}

        # Fill idempotency tracking - prevents duplicate trade processing
        self._processed_trade_ids: set[str] = set()

        # Signal deduplication - prevents duplicate signal submission
        self._recent_signal_ids: dict[str, datetime] = {}

        # Signal queue
        self._signal_queue: asyncio.Queue[Signal] = asyncio.Queue()
        self._processing_task: Optional[asyncio.Task] = None
        self._running = False
        
        logger.info(f"ExecutionEngine initialized (dry_run={config.dry_run})")
    
    async def start(self) -> None:
        """Start the execution engine."""
        if self._running:
            return
        
        self._running = True
        self._processing_task = asyncio.create_task(
            self._process_signals(),
            name="signal_processor"
        )
        
        # Start order timeout monitor
        asyncio.create_task(self._monitor_order_timeouts(), name="order_timeout_monitor")
        
        logger.info("ExecutionEngine started")
    
    async def stop(self) -> None:
        """Stop the execution engine."""
        self._running = False
        
        if self._processing_task:
            self._processing_task.cancel()
            try:
                await self._processing_task
            except asyncio.CancelledError:
                pass
        
        # Cancel all open orders
        await self.cancel_all_orders()
        
        logger.info("ExecutionEngine stopped")
    
    async def submit_signal(self, signal: Signal) -> None:
        """Submit a signal for processing with deduplication."""
        # Dedup check
        if signal.signal_id in self._recent_signal_ids:
            logger.warning(f"Duplicate signal rejected: {signal.signal_id}")
            return
        self._recent_signal_ids[signal.signal_id] = datetime.utcnow()
        self._cleanup_recent_signals()
        await self._signal_queue.put(signal)
        logger.debug(f"Signal queued: {signal.signal_id}")

    def _cleanup_recent_signals(self) -> None:
        """Remove signal IDs older than 60 seconds."""
        cutoff = datetime.utcnow() - timedelta(seconds=60)
        expired = [sid for sid, ts in self._recent_signal_ids.items() if ts < cutoff]
        for sid in expired:
            del self._recent_signal_ids[sid]
    
    async def _process_signals(self) -> None:
        """Main signal processing loop."""
        while self._running:
            try:
                # Get next signal with timeout
                try:
                    signal = await asyncio.wait_for(
                        self._signal_queue.get(),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                
                await self._execute_signal(signal)
                self.stats.signals_processed += 1
                
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Signal processing error: {e}")
    
    async def _execute_signal(self, signal: Signal) -> None:
        """Execute a single trading signal."""
        logger.info(f"Executing signal: {signal.signal_id} ({signal.action})")
        
        if signal.is_place:
            await self._handle_place_orders(signal)
        elif signal.is_cancel:
            await self._handle_cancel_orders(signal)
        else:
            logger.warning(f"Unknown signal action: {signal.action}")
    
    async def _handle_place_orders(self, signal: Signal) -> None:
        """Handle a place_orders signal with atomic bundle support."""
        # Detect if this is a bundle order (multiple orders with bundle strategy)
        is_bundle = (
            len(signal.orders) > 1 and
            any("bundle" in order_spec.get("strategy_tag", "").lower()
                for order_spec in signal.orders)
        )

        if is_bundle:
            await self._handle_bundle_orders(signal)
        else:
            await self._handle_single_orders(signal)

    async def _handle_bundle_orders(self, signal: Signal) -> None:
        """
        Handle bundle orders atomically - all orders must succeed or none.

        For bundle arbitrage (buy YES + buy NO) or neg-risk (buy all outcomes),
        all legs must fill or we roll back to avoid directional exposure.
        """
        logger.info(f"Processing bundle signal with {len(signal.orders)} orders")

        # Phase 1: Validate all orders before placing any
        validated_orders = []
        for order_spec in signal.orders:
            try:
                # CRITICAL FIX: Use per-leg market_id if provided (for neg-risk)
                market_id = order_spec.get("market_id", signal.market_id)
                token_type = order_spec["token_type"]
                side = order_spec["side"]
                price = order_spec["price"]
                size = order_spec["size"]
                strategy_tag = order_spec.get("strategy_tag", "")

                # Check slippage
                if self.config.enable_slippage_check and signal.opportunity:
                    if not await self._check_slippage_fresh(market_id, order_spec):
                        self.stats.slippage_rejections += 1
                        logger.warning(f"Bundle rejected: slippage on {token_type} in {market_id}")
                        return  # Abort entire bundle

                # Check risk limits
                proposed_order = Order(
                    order_id="temp",
                    market_id=market_id,
                    token_type=token_type,
                    side=side,
                    price=price,
                    size=size,
                    strategy_tag=strategy_tag,
                )

                if not self.risk_manager.check_order(proposed_order):
                    self.stats.signals_rejected += 1
                    logger.warning(f"Bundle rejected: risk limits on {token_type}")
                    return  # Abort entire bundle

                validated_orders.append(order_spec)

            except Exception as e:
                logger.error(f"Bundle validation failed: {e}")
                return  # Abort entire bundle

        if len(validated_orders) != len(signal.orders):
            logger.warning("Bundle incomplete after validation, aborting")
            return

        # Phase 2: Place all orders
        placed_orders: list[Order] = []
        placement_failed = False

        for order_spec in validated_orders:
            try:
                # CRITICAL FIX: Use per-leg market_id
                market_id = order_spec.get("market_id", signal.market_id)

                order = await self._place_order(
                    market_id=market_id,
                    token_type=order_spec["token_type"],
                    side=order_spec["side"],
                    price=order_spec["price"],
                    size=order_spec["size"],
                    strategy_tag=order_spec.get("strategy_tag", ""),
                )

                if order:
                    placed_orders.append(order)
                else:
                    placement_failed = True
                    break

            except Exception as e:
                logger.error(f"Bundle order placement failed: {e}")
                placement_failed = True
                break

        # Phase 3: Rollback if any order failed
        if placement_failed or len(placed_orders) != len(validated_orders):
            logger.warning(
                f"Bundle placement incomplete ({len(placed_orders)}/{len(validated_orders)}), "
                f"rolling back..."
            )
            for order in placed_orders:
                try:
                    await self.cancel_order(order.order_id)
                    logger.info(f"Rolled back order: {order.order_id}")
                except Exception as e:
                    logger.error(f"Rollback failed for {order.order_id}: {e}")
            self.stats.orders_rejected += len(validated_orders)
            return

        # Phase 4: Success - track all orders
        for order in placed_orders:
            self._track_order(order)
            self.stats.orders_placed += 1
            self.stats.total_notional += order.notional

        logger.info(f"Bundle executed successfully: {len(placed_orders)} orders placed")

    async def _handle_single_orders(self, signal: Signal) -> None:
        """Handle non-bundle orders (original behavior)."""
        for order_spec in signal.orders:
            try:
                # Extract order parameters
                token_type = order_spec["token_type"]
                side = order_spec["side"]
                price = order_spec["price"]
                size = order_spec["size"]
                strategy_tag = order_spec.get("strategy_tag", "")

                # Check slippage if enabled
                if self.config.enable_slippage_check and signal.opportunity:
                    if not self._check_slippage(signal.opportunity, order_spec):
                        self.stats.slippage_rejections += 1
                        logger.warning(f"Order rejected due to slippage: {order_spec}")
                        continue

                # Check risk limits
                proposed_order = Order(
                    order_id="temp",
                    market_id=signal.market_id,
                    token_type=token_type,
                    side=side,
                    price=price,
                    size=size,
                    strategy_tag=strategy_tag,
                )

                if not self.risk_manager.check_order(proposed_order):
                    self.stats.signals_rejected += 1
                    logger.warning(f"Order rejected by risk manager: {order_spec}")
                    continue

                # Place the order
                order = await self._place_order(
                    market_id=signal.market_id,
                    token_type=token_type,
                    side=side,
                    price=price,
                    size=size,
                    strategy_tag=strategy_tag,
                )

                if order:
                    self._track_order(order)
                    self.stats.orders_placed += 1
                    self.stats.total_notional += order.notional

            except Exception as e:
                logger.error(f"Failed to place order: {e}")
                self.stats.orders_rejected += 1
    
    async def _handle_cancel_orders(self, signal: Signal) -> None:
        """Handle a cancel_orders signal."""
        for order_id in signal.cancel_order_ids:
            try:
                await self.cancel_order(order_id)
            except Exception as e:
                logger.error(f"Failed to cancel order {order_id}: {e}")
    
    def _check_slippage(self, opportunity, order_spec: dict) -> bool:
        """
        Check if current prices have slipped too far from signal generation.

        Note: This uses stale opportunity snapshot prices. For bundle orders,
        use _check_slippage_fresh() instead.

        Returns True if within tolerance, False if slippage exceeded.
        """
        # Compare intended price vs opportunity snapshot
        intended_price = order_spec["price"]
        side = order_spec["side"]
        token_type = order_spec["token_type"]

        if token_type == TokenType.YES:
            snapshot_bid = opportunity.best_bid_yes
            snapshot_ask = opportunity.best_ask_yes
        else:
            snapshot_bid = opportunity.best_bid_no
            snapshot_ask = opportunity.best_ask_no

        if snapshot_bid is None or snapshot_ask is None:
            return True  # Can't check, allow

        if side == OrderSide.BUY:
            # For buys, check if ask hasn't moved up too much
            slippage = (intended_price - snapshot_ask) / snapshot_ask if snapshot_ask > 0 else 0
        else:
            # For sells, check if bid hasn't moved down too much
            slippage = (snapshot_bid - intended_price) / snapshot_bid if snapshot_bid > 0 else 0

        return abs(slippage) <= self.config.slippage_tolerance

    async def _check_slippage_fresh(self, market_id: str, order_spec: dict) -> bool:
        """
        Check slippage using fresh CLOB prices (not stale snapshot).

        For critical bundle orders, we fetch the current execution price
        from the CLOB API to ensure we're not trading on stale data.

        Returns True if within tolerance, False if slippage exceeded.
        """
        intended_price = order_spec["price"]
        side = order_spec["side"]
        token_type = order_spec["token_type"]

        try:
            # Fetch current order book for the market (contains both YES and NO)
            order_book = await self.client.get_orderbook(market_id)

            if not order_book:
                logger.warning(f"Could not fetch order book for slippage check, allowing order")
                return True

            # Get the token-specific book
            if token_type == TokenType.YES:
                token_book = order_book.yes
            else:
                token_book = order_book.no

            if not token_book:
                return True  # Can't check, allow

            current_bid = token_book.best_bid
            current_ask = token_book.best_ask

            if current_bid is None or current_ask is None:
                return True  # Can't check, allow

            if side == OrderSide.BUY:
                # For buys, check if current ask hasn't moved up too much from our intended price
                slippage = (current_ask - intended_price) / intended_price if intended_price > 0 else 0
            else:
                # For sells, check if current bid hasn't moved down too much
                slippage = (intended_price - current_bid) / intended_price if intended_price > 0 else 0

            within_tolerance = slippage <= self.config.slippage_tolerance

            if not within_tolerance:
                logger.warning(
                    f"Fresh slippage check failed: intended={intended_price:.4f}, "
                    f"current_{'ask' if side == OrderSide.BUY else 'bid'}="
                    f"{current_ask if side == OrderSide.BUY else current_bid:.4f}, "
                    f"slippage={slippage:.2%}"
                )

            return within_tolerance

        except Exception as e:
            logger.warning(f"Fresh slippage check error: {e}, allowing order")
            return True  # On error, allow (fail open)
    
    async def _place_order(
        self,
        market_id: str,
        token_type: TokenType,
        side: OrderSide,
        price: float,
        size: float,
        strategy_tag: str = "",
    ) -> Optional[Order]:
        """Place an order through the API with retry logic."""
        last_error = None
        
        for attempt in range(self.config.max_retries):
            try:
                order = await self.client.place_order(
                    market_id=market_id,
                    token_type=token_type,
                    side=side,
                    price=price,
                    size=size,
                    strategy_tag=strategy_tag,
                )
                
                logger.info(
                    f"Order placed: {order.order_id} | "
                    f"{side.value} {size:.2f} {token_type.value} @ {price:.4f}"
                )
                
                return order
                
            except Exception as e:
                last_error = e
                logger.warning(f"Order placement attempt {attempt + 1} failed: {e}")
                if attempt < self.config.max_retries - 1:
                    await asyncio.sleep(self.config.retry_delay)
        
        logger.error(f"Order placement failed after {self.config.max_retries} attempts: {last_error}")
        return None
    
    def _track_order(self, order: Order) -> None:
        """Add order to tracking structures."""
        self._open_orders[order.order_id] = order
        self._order_timestamps[order.order_id] = datetime.utcnow()
        
        # Track by market
        if order.market_id not in self._orders_by_market:
            self._orders_by_market[order.market_id] = []
        self._orders_by_market[order.market_id].append(order.order_id)
        
        # Track by strategy
        if order.strategy_tag:
            if order.strategy_tag not in self._orders_by_strategy:
                self._orders_by_strategy[order.strategy_tag] = []
            self._orders_by_strategy[order.strategy_tag].append(order.order_id)
    
    def _untrack_order(self, order_id: str) -> None:
        """Remove order from tracking structures."""
        if order_id in self._open_orders:
            order = self._open_orders[order_id]
            del self._open_orders[order_id]
            
            if order_id in self._order_timestamps:
                del self._order_timestamps[order_id]
            
            # Remove from market tracking
            if order.market_id in self._orders_by_market:
                if order_id in self._orders_by_market[order.market_id]:
                    self._orders_by_market[order.market_id].remove(order_id)
            
            # Remove from strategy tracking
            if order.strategy_tag and order.strategy_tag in self._orders_by_strategy:
                if order_id in self._orders_by_strategy[order.strategy_tag]:
                    self._orders_by_strategy[order.strategy_tag].remove(order_id)
    
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a specific order."""
        try:
            await self.client.cancel_order(order_id)
            self._untrack_order(order_id)
            self.stats.orders_cancelled += 1
            logger.info(f"Order cancelled: {order_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False
    
    async def cancel_all_orders(self, market_id: Optional[str] = None) -> int:
        """Cancel all open orders, optionally for a specific market."""
        if market_id:
            order_ids = list(self._orders_by_market.get(market_id, []))
        else:
            order_ids = list(self._open_orders.keys())
        
        cancelled = 0
        for order_id in order_ids:
            if await self.cancel_order(order_id):
                cancelled += 1
        
        logger.info(f"Cancelled {cancelled} orders")
        return cancelled
    
    async def cancel_orders_by_strategy(self, strategy_tag: str) -> int:
        """Cancel all orders for a specific strategy."""
        order_ids = list(self._orders_by_strategy.get(strategy_tag, []))
        
        cancelled = 0
        for order_id in order_ids:
            if await self.cancel_order(order_id):
                cancelled += 1
        
        return cancelled
    
    async def _monitor_order_timeouts(self) -> None:
        """Monitor and cancel orders that have timed out."""
        while self._running:
            try:
                await asyncio.sleep(10)  # Check every 10 seconds
                
                now = datetime.utcnow()
                timeout_delta = timedelta(seconds=self.config.order_timeout_seconds)
                
                timed_out = [
                    order_id for order_id, timestamp in self._order_timestamps.items()
                    if now - timestamp > timeout_delta
                ]
                
                for order_id in timed_out:
                    logger.info(f"Order timed out: {order_id}")
                    await self.cancel_order(order_id)
                    
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Order timeout monitor error: {e}")
    
    def handle_fill(self, trade: Trade) -> None:
        """Handle a trade fill notification with idempotency check."""
        # Idempotency check - prevent duplicate fill processing
        if trade.trade_id in self._processed_trade_ids:
            logger.debug(f"Ignoring duplicate fill: {trade.trade_id}")
            return

        # Mark as processed BEFORE updating state to prevent race conditions
        self._processed_trade_ids.add(trade.trade_id)

        # Limit memory usage - remove old trade IDs if set gets too large
        if len(self._processed_trade_ids) > 10000:
            # Keep only the most recent 5000 (approximate, since sets are unordered)
            # In production, use an LRU cache or time-based expiry
            logger.debug("Trimming processed trade IDs set")
            self._processed_trade_ids = set(list(self._processed_trade_ids)[-5000:])

        order_id = trade.order_id

        if order_id in self._open_orders:
            order = self._open_orders[order_id]
            order.filled_size += trade.size
            order.updated_at = datetime.utcnow()

            if order.remaining_size <= 0:
                order.status = OrderStatus.FILLED
                self._untrack_order(order_id)
                self.stats.orders_filled += 1
            else:
                order.status = OrderStatus.PARTIALLY_FILLED

        # Update portfolio
        self.portfolio.update_from_fill(trade)

        # Update risk manager
        self.risk_manager.update_from_fill(trade)

        logger.info(
            f"Fill: {trade.trade_id} | "
            f"{trade.side.value} {trade.size:.2f} {trade.token_type.value} @ {trade.price:.4f}"
        )
    
    def get_open_orders(self, market_id: Optional[str] = None) -> list[Order]:
        """Get all open orders, optionally filtered by market."""
        if market_id:
            order_ids = self._orders_by_market.get(market_id, [])
            return [self._open_orders[oid] for oid in order_ids if oid in self._open_orders]
        return list(self._open_orders.values())
    
    def get_stats(self) -> ExecutionStats:
        """Get execution statistics."""
        return self.stats
    
    @property
    def open_order_count(self) -> int:
        """Get number of open orders."""
        return len(self._open_orders)

