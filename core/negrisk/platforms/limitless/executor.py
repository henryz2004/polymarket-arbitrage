"""
Limitless Exchange Executor
============================

Thin wrapper around the limitless-sdk for placing FOK orders on Limitless Exchange.

Architecture: NegriskEngine routes platform="limitless" opportunities here instead
of through the Polymarket ExecutionEngine. This avoids coupling to Polymarket-specific
models (Signal, Order, OrderBook).

Execution flow:
1. Pre-flight: Fresh slippage check on each leg via api_client.get_orderbook()
2. Execute: Sequential FOK orders for each leg. Abort on first failure.
3. Rollback: If legs 1-N filled but leg N+1 failed, sell legs 1-N back.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from core.negrisk.models import NegriskOpportunity
from core.negrisk.platforms.limitless.api_client import LimitlessAPIClient


logger = logging.getLogger(__name__)


@dataclass
class LegOrderResult:
    """Result of placing a single leg order."""
    success: bool
    order_id: Optional[str] = None
    token_id: str = ""
    market_slug: str = ""
    side: str = "BUY"
    price: float = 0.0
    size: float = 0.0
    filled_size: float = 0.0
    error: Optional[str] = None


@dataclass
class ExecutionResult:
    """Result of executing a full multi-leg opportunity."""
    success: bool
    reason: str = ""
    orders: list[LegOrderResult] = field(default_factory=list)
    total_cost: float = 0.0
    execution_time_ms: float = 0.0


class LimitlessExecutor:
    """
    Executor for Limitless Exchange orders.

    Wraps the limitless-sdk to place FOK (Fill-or-Kill) orders for multi-leg
    neg-risk arbitrage opportunities.

    In dry_run mode (default), simulates execution without calling the SDK.
    """

    def __init__(
        self,
        api_client: LimitlessAPIClient,
        api_key: Optional[str] = None,
        private_key: Optional[str] = None,
        dry_run: bool = True,
        slippage_tolerance: float = 0.02,
    ):
        """
        Initialize the Limitless executor.

        Args:
            api_client: LimitlessAPIClient for orderbook queries
            api_key: Limitless API key (required for live execution)
            private_key: Wallet private key for signing (required for live)
            dry_run: If True, simulate without placing real orders
            slippage_tolerance: Max allowed price movement since detection (2%)
        """
        self.api_client = api_client
        self.api_key = api_key
        self.private_key = private_key
        self.dry_run = dry_run
        self.slippage_tolerance = slippage_tolerance

        # SDK clients (initialized in initialize())
        self._order_client = None
        self._initialized = False

        # Stats
        self._stats = {
            "opportunities_received": 0,
            "dry_run_simulations": 0,
            "executions_attempted": 0,
            "executions_succeeded": 0,
            "executions_failed": 0,
            "slippage_rejections": 0,
            "rollbacks_attempted": 0,
            "total_volume_usd": 0.0,
        }

    async def initialize(self) -> None:
        """
        Initialize SDK clients for live execution.

        Creates the limitless-sdk OrderClient with wallet signing.
        Skipped in dry_run mode — SDK not required.
        """
        if self.dry_run:
            logger.info("LimitlessExecutor initialized in DRY_RUN mode (no SDK needed)")
            self._initialized = True
            return

        if not self.api_key or not self.private_key:
            raise ValueError(
                "api_key and private_key are required for live execution. "
                "Set dry_run=True for simulation mode."
            )

        try:
            from limitless_sdk.orders import OrderClient
            from limitless_sdk.api import HttpClient
            from eth_account import Account

            wallet = Account.from_key(self.private_key)
            http_client = HttpClient(api_key=self.api_key)
            self._order_client = OrderClient(http_client=http_client, wallet=wallet)
            self._initialized = True
            logger.info(f"LimitlessExecutor initialized for live execution (wallet={wallet.address})")
        except ImportError as e:
            raise ImportError(
                f"Live execution requires limitless-sdk and eth-account: {e}. "
                "Install with: pip install limitless-sdk eth-account"
            ) from e

    async def execute_opportunity(self, opportunity: NegriskOpportunity) -> ExecutionResult:
        """
        Execute a multi-leg neg-risk arbitrage opportunity.

        Flow:
        1. Pre-flight slippage check on each leg
        2. Place sequential FOK orders (abort on first failure)
        3. Rollback filled legs on partial failure

        Args:
            opportunity: Detected arbitrage opportunity with legs

        Returns:
            ExecutionResult with success/failure details
        """
        if not self._initialized:
            return ExecutionResult(success=False, reason="Executor not initialized")

        self._stats["opportunities_received"] += 1
        start_time = time.monotonic()

        # Dry-run mode: simulate and return
        if self.dry_run:
            return self._simulate_execution(opportunity)

        # Live execution
        self._stats["executions_attempted"] += 1

        # Step 1: Pre-flight slippage check on all legs
        for leg in opportunity.legs:
            slippage_ok = await self._check_leg_slippage(leg)
            if not slippage_ok:
                self._stats["slippage_rejections"] += 1
                elapsed = (time.monotonic() - start_time) * 1000
                return ExecutionResult(
                    success=False,
                    reason=f"Slippage check failed for {leg['outcome_name']}: "
                           f"price moved beyond {self.slippage_tolerance:.1%} tolerance",
                    execution_time_ms=elapsed,
                )

        # Step 2: Sequential FOK order placement
        placed_orders: list[LegOrderResult] = []
        for i, leg in enumerate(opportunity.legs):
            result = await self._place_leg_order(leg)
            placed_orders.append(result)

            if not result.success:
                # Leg failed — rollback previously filled legs
                logger.warning(
                    f"Leg {i+1}/{len(opportunity.legs)} failed for "
                    f"{leg['outcome_name']}: {result.error}"
                )

                if placed_orders[:-1]:  # Has successfully placed orders to rollback
                    await self._rollback_orders([o for o in placed_orders[:-1] if o.success])

                elapsed = (time.monotonic() - start_time) * 1000
                self._stats["executions_failed"] += 1
                return ExecutionResult(
                    success=False,
                    reason=f"Leg {i+1} failed: {result.error}",
                    orders=placed_orders,
                    execution_time_ms=elapsed,
                )

        # All legs succeeded
        elapsed = (time.monotonic() - start_time) * 1000
        total_cost = sum(o.price * o.filled_size for o in placed_orders)
        self._stats["executions_succeeded"] += 1
        self._stats["total_volume_usd"] += total_cost

        logger.info(
            f"Limitless execution SUCCESS: {len(placed_orders)} legs filled, "
            f"cost=${total_cost:.2f}, time={elapsed:.0f}ms"
        )

        return ExecutionResult(
            success=True,
            reason="All legs filled",
            orders=placed_orders,
            total_cost=total_cost,
            execution_time_ms=elapsed,
        )

    async def _check_leg_slippage(self, leg: dict) -> bool:
        """
        Check if leg price has moved beyond slippage tolerance.

        Fetches fresh orderbook and compares current best price to detected price.

        Args:
            leg: Leg dict from NegriskOpportunity.legs

        Returns:
            True if price is within tolerance, False if slipped too much
        """
        try:
            orderbook = await self.api_client.get_orderbook(leg["market_id"])

            if leg["side"] == "BUY":
                # For buying, check the best ask hasn't moved up
                asks = orderbook.get("asks", [])
                if not asks:
                    logger.warning(f"No asks for {leg['market_id']}")
                    return False
                current_price = min(float(a["price"]) for a in asks)
            else:
                # For selling, check the best bid hasn't moved down
                bids = orderbook.get("bids", [])
                if not bids:
                    logger.warning(f"No bids for {leg['market_id']}")
                    return False
                current_price = max(float(b["price"]) for b in bids)

            detected_price = leg["price"]
            slippage = abs(current_price - detected_price) / detected_price

            if slippage > self.slippage_tolerance:
                logger.info(
                    f"Slippage rejection: {leg['outcome_name']} "
                    f"detected={detected_price:.4f} current={current_price:.4f} "
                    f"slippage={slippage:.2%} > tolerance={self.slippage_tolerance:.2%}"
                )
                return False

            return True

        except Exception as e:
            logger.warning(f"Slippage check error for {leg['market_id']}: {e}")
            return False

    async def _place_leg_order(self, leg: dict) -> LegOrderResult:
        """
        Place a single FOK order for one leg via the limitless-sdk.

        Args:
            leg: Leg dict with market_id, token_id, price, size, side, outcome_name

        Returns:
            LegOrderResult with fill details
        """
        try:
            from limitless_sdk.orders import Side, OrderType

            sdk_side = Side.BUY if leg["side"] == "BUY" else Side.SELL
            maker_amount = leg["price"] * leg["size"]  # USDC to spend

            result = await self._order_client.create_order(
                token_id=leg["token_id"],
                maker_amount=maker_amount,
                side=sdk_side,
                order_type=OrderType.FOK,
                market_slug=leg["market_id"],
            )

            # Parse SDK response
            order_id = result.get("id") or result.get("order_id", "")
            filled = result.get("filled_size", leg["size"])

            return LegOrderResult(
                success=True,
                order_id=order_id,
                token_id=leg["token_id"],
                market_slug=leg["market_id"],
                side=leg["side"],
                price=leg["price"],
                size=leg["size"],
                filled_size=filled,
            )

        except Exception as e:
            return LegOrderResult(
                success=False,
                token_id=leg.get("token_id", ""),
                market_slug=leg.get("market_id", ""),
                side=leg.get("side", "BUY"),
                price=leg.get("price", 0),
                size=leg.get("size", 0),
                error=str(e),
            )

    async def _rollback_orders(self, filled_orders: list[LegOrderResult]) -> None:
        """
        Rollback filled legs by placing opposite orders.

        For FOK orders, partial fills shouldn't happen, but this is a safety net.
        Sells back any filled BUY legs, buys back any filled SELL legs.

        Args:
            filled_orders: List of successfully filled LegOrderResults
        """
        if not filled_orders:
            return

        self._stats["rollbacks_attempted"] += 1
        logger.warning(f"Rolling back {len(filled_orders)} filled legs")

        for order in filled_orders:
            try:
                # Reverse the side
                reverse_side = "SELL" if order.side == "BUY" else "BUY"
                reverse_leg = {
                    "token_id": order.token_id,
                    "market_id": order.market_slug,
                    "side": reverse_side,
                    "price": order.price,
                    "size": order.filled_size,
                    "outcome_name": f"rollback_{order.token_id[:8]}",
                }
                result = await self._place_leg_order(reverse_leg)
                if result.success:
                    logger.info(f"Rollback success: {reverse_side} {order.filled_size} @ {order.price}")
                else:
                    logger.error(f"Rollback FAILED for {order.token_id}: {result.error}")
            except Exception as e:
                logger.error(f"Rollback error for {order.token_id}: {e}")

    def _simulate_execution(self, opportunity: NegriskOpportunity) -> ExecutionResult:
        """
        Simulate execution in dry-run mode.

        Logs what would happen without calling the SDK.

        Args:
            opportunity: The opportunity to simulate

        Returns:
            ExecutionResult marked as dry-run simulation
        """
        self._stats["dry_run_simulations"] += 1

        simulated_orders = []
        total_cost = 0.0

        for leg in opportunity.legs:
            cost = leg["price"] * leg["size"]
            total_cost += cost
            simulated_orders.append(LegOrderResult(
                success=True,
                order_id=f"DRY_RUN_{leg['token_id'][:8]}",
                token_id=leg["token_id"],
                market_slug=leg["market_id"],
                side=leg["side"],
                price=leg["price"],
                size=leg["size"],
                filled_size=leg["size"],
            ))

        logger.info(
            f"DRY_RUN: Would execute {opportunity.direction.value} "
            f"on {opportunity.event.title[:50]} | "
            f"{len(opportunity.legs)} legs | "
            f"cost=${total_cost:.2f} | "
            f"net_edge={opportunity.net_edge:.4f} ({opportunity.net_edge*100:.2f}%)"
        )

        for i, leg in enumerate(opportunity.legs):
            logger.info(
                f"  DRY_RUN Leg {i+1}: {leg['side']} {leg['outcome_name'][:40]} "
                f"@ ${leg['price']:.4f} x {leg['size']:.0f}"
            )

        return ExecutionResult(
            success=True,
            reason="DRY_RUN simulation",
            orders=simulated_orders,
            total_cost=total_cost,
        )

    def get_stats(self) -> dict:
        """Get executor statistics."""
        return {
            "dry_run": self.dry_run,
            "initialized": self._initialized,
            **self._stats,
        }
