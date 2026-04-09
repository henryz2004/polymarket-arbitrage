"""
Polymarket Neg-Risk Executor
================================

Thin wrapper around py-clob-client for placing FOK orders on Polymarket.

Architecture: NegriskEngine routes platform="polymarket" opportunities here
when a PolymarketExecutor is provided, bypassing the older ExecutionEngine
(which routes through Signal/Order/OrderBook abstractions).

Execution flow:
1. Pre-flight: Kill switch check, max trade size cap, USDC.e balance check
2. Slippage check on each leg via py-clob-client.get_order_book() (parallel)
3. Batch FOK orders via post_orders() (single HTTP call for all legs)
4. Rollback: If some legs filled but others failed, sell filled legs back.
   Falls back to sequential placement if batch API fails.

Pre-flight:
  1. Get a Polygon wallet with USDC.e
  2. Export env vars:
       export POLYMARKET_PRIVATE_KEY="0xyour_private_key"
       export POLYMARKET_FUNDER="0xyour_wallet_address"
  3. Approve contracts (one-time):
       - USDC.e → CTF Exchange, Neg Risk CTF Exchange, Neg Risk Adapter
       - Conditional Tokens → same three contracts
  4. Test with small trade:
       python negrisk_long_test.py --platform polymarket --duration 0.1 --edge 3 --execute --max-size 20
  5. Kill switch: touch KILL_SWITCH to immediately halt all execution
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.negrisk.models import NegriskOpportunity


logger = logging.getLogger(__name__)


# Polymarket contract addresses (Polygon, chain_id=137)
POLYGON_CHAIN_ID = 137
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
CLOB_API_URL = "https://clob.polymarket.com"
DEFAULT_RPC_URL = "https://polygon-rpc.com"


@dataclass
class LegOrderResult:
    """Result of placing a single leg order."""
    success: bool
    order_id: Optional[str] = None
    token_id: str = ""
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


class PolymarketExecutor:
    """
    Executor for Polymarket neg-risk orders via py-clob-client.

    Places FOK (Fill-or-Kill) market orders for multi-leg neg-risk arbitrage.
    All orders use negRisk=True in options.

    In dry_run mode (default), simulates execution without calling the SDK.
    """

    def __init__(
        self,
        private_key: Optional[str] = None,
        funder: Optional[str] = None,
        dry_run: bool = True,
        slippage_tolerance: float = 0.02,
        max_trade_usd: float = 50.0,
        kill_switch_path: str = "KILL_SWITCH",
        rpc_url: str = DEFAULT_RPC_URL,
        tick_size: str = "0.01",
    ):
        """
        Initialize the Polymarket executor.

        Args:
            private_key: EOA wallet private key for signing orders
            funder: Wallet address that funds the orders (often same as signer)
            dry_run: If True, simulate without placing real orders
            slippage_tolerance: Max allowed price movement since detection (2%)
            max_trade_usd: Hard cap on total cost per opportunity (default $50)
            kill_switch_path: If this file exists, refuse to execute
            rpc_url: Polygon RPC URL for balance checks
            tick_size: Tick size for orders (usually "0.01")
        """
        self.private_key = private_key
        self.funder = funder
        self.dry_run = dry_run
        self.slippage_tolerance = slippage_tolerance
        self.max_trade_usd = max_trade_usd
        self.kill_switch_path = kill_switch_path
        self.rpc_url = rpc_url
        self.tick_size = tick_size

        # py-clob-client ClobClient (initialized in initialize())
        self._clob_client = None
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
        Initialize the ClobClient for order placement.

        In dry_run mode, creates a read-only client for orderbook queries.
        In live mode, creates a fully authenticated trading client.
        """
        if self.dry_run:
            try:
                from py_clob_client.client import ClobClient

                # Read-only client for orderbook queries during dry-run
                self._clob_client = ClobClient(CLOB_API_URL)
                logger.info("PolymarketExecutor initialized in DRY_RUN mode (read-only client)")
            except ImportError:
                logger.info(
                    "PolymarketExecutor initialized in DRY_RUN mode "
                    "(py-clob-client not installed, orderbook queries disabled)"
                )
            self._initialized = True
            return

        if not self.private_key:
            raise ValueError(
                "private_key is required for live execution. "
                "Set dry_run=True for simulation mode."
            )

        try:
            from py_clob_client.client import ClobClient

            self._clob_client = ClobClient(
                CLOB_API_URL,
                key=self.private_key,
                chain_id=POLYGON_CHAIN_ID,
                signature_type=0,  # EOA wallet
                funder=self.funder,
            )
            # Derive API credentials (L2 auth)
            self._clob_client.set_api_creds(
                self._clob_client.create_or_derive_api_creds()
            )
            self._initialized = True
            logger.info(
                f"PolymarketExecutor initialized for LIVE execution "
                f"(funder={self.funder})"
            )
        except ImportError as e:
            raise ImportError(
                f"Live execution requires py-clob-client: {e}. "
                "Install with: pip install py-clob-client"
            ) from e

    async def execute_opportunity(self, opportunity: NegriskOpportunity) -> ExecutionResult:
        """
        Execute a multi-leg neg-risk arbitrage opportunity.

        Flow:
        1. Pre-flight checks (kill switch, trade cap, balance)
        2. Pre-flight slippage check on each leg
        3. Place sequential FOK orders (abort on first failure)
        4. Rollback filled legs on partial failure

        Args:
            opportunity: Detected arbitrage opportunity with legs

        Returns:
            ExecutionResult with success/failure details
        """
        if not self._initialized:
            return ExecutionResult(success=False, reason="Executor not initialized")

        self._stats["opportunities_received"] += 1
        start_time = time.monotonic()

        # Kill switch check
        if Path(self.kill_switch_path).exists():
            return ExecutionResult(
                success=False, reason="KILL SWITCH ACTIVE - execution halted"
            )

        # Max trade size cap
        total_cost_estimate = sum(
            leg["price"] * leg["size"] for leg in opportunity.legs
        )
        if total_cost_estimate > self.max_trade_usd:
            return ExecutionResult(
                success=False,
                reason=(
                    f"Trade cost ${total_cost_estimate:.2f} exceeds "
                    f"max ${self.max_trade_usd:.2f}"
                ),
            )

        # Dry-run mode: simulate and return
        if self.dry_run:
            return self._simulate_execution(opportunity, start_time)

        # Live execution
        self._stats["executions_attempted"] += 1

        # USDC.e balance pre-check
        try:
            balance = await self._check_balance()
            if balance < total_cost_estimate * 1.05:  # 5% buffer
                return ExecutionResult(
                    success=False,
                    reason=(
                        f"Insufficient USDC.e: have ${balance:.2f}, "
                        f"need ${total_cost_estimate:.2f}"
                    ),
                )
        except Exception as e:
            logger.warning(f"Balance check failed (proceeding anyway): {e}")

        logger.info(
            f"LIVE EXECUTION: {opportunity.direction.value} on "
            f"{opportunity.event.title[:60]} | "
            f"{len(opportunity.legs)} legs | "
            f"estimated_cost=${total_cost_estimate:.2f} | "
            f"net_edge={opportunity.net_edge:.4f} "
            f"({opportunity.net_edge * 100:.2f}%)"
        )

        # Step 1: Pre-flight slippage check on all legs (parallel for lower latency)
        slippage_tasks = [self._check_leg_slippage(leg) for leg in opportunity.legs]
        slippage_results = await asyncio.gather(*slippage_tasks, return_exceptions=True)

        for i, result in enumerate(slippage_results):
            if isinstance(result, Exception):
                logger.warning(f"Slippage check exception for leg {i}: {result}")
                slippage_ok = False
            else:
                slippage_ok = result

            if not slippage_ok:
                self._stats["slippage_rejections"] += 1
                elapsed = (time.monotonic() - start_time) * 1000
                leg = opportunity.legs[i]
                return ExecutionResult(
                    success=False,
                    reason=(
                        f"Slippage check failed for {leg['outcome_name']}: "
                        f"price moved beyond {self.slippage_tolerance:.1%} tolerance"
                    ),
                    execution_time_ms=elapsed,
                )

        # Step 2: Batch FOK order placement (single HTTP call for all legs)
        placed_orders = await self._place_legs_batch(opportunity.legs)

        filled = [o for o in placed_orders if o.success]
        failed = [o for o in placed_orders if not o.success]

        if failed:
            # Partial or full failure — rollback any filled legs
            if filled:
                logger.warning(
                    f"{len(filled)}/{len(placed_orders)} legs filled, "
                    f"{len(failed)} failed — rolling back"
                )
                await self._rollback_orders(filled)

            elapsed = (time.monotonic() - start_time) * 1000
            self._stats["executions_failed"] += 1
            first_error = failed[0].error or "unknown"
            return ExecutionResult(
                success=False,
                reason=f"{len(failed)}/{len(placed_orders)} legs failed: {first_error}",
                orders=placed_orders,
                execution_time_ms=elapsed,
            )

        # All legs succeeded
        elapsed = (time.monotonic() - start_time) * 1000
        total_cost = sum(o.price * o.filled_size for o in placed_orders)
        self._stats["executions_succeeded"] += 1
        self._stats["total_volume_usd"] += total_cost

        logger.info(
            f"Polymarket execution SUCCESS: {len(placed_orders)} legs filled "
            f"(batch), cost=${total_cost:.2f}, time={elapsed:.0f}ms"
        )

        return ExecutionResult(
            success=True,
            reason="All legs filled (batch)",
            orders=placed_orders,
            total_cost=total_cost,
            execution_time_ms=elapsed,
        )

    async def _check_balance(self) -> float:
        """
        Read wallet USDC.e balance on Polygon via eth_call.

        Returns:
            USDC.e balance in dollars (6 decimal token)
        """
        import aiohttp

        if not self.funder:
            raise RuntimeError("Funder address not set")

        # balanceOf(address) selector = 0x70a08231
        addr_padded = self.funder[2:].lower().zfill(64)
        data = f"0x70a08231{addr_padded}"

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": USDC_E_ADDRESS, "data": data}, "latest"],
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(self.rpc_url, json=payload) as resp:
                result = await resp.json()
                hex_val = result.get("result", "0x0")
                raw_balance = int(hex_val, 16)
                return raw_balance / 10**6  # USDC.e has 6 decimals

    async def _check_leg_slippage(self, leg: dict) -> bool:
        """
        Check if leg price has moved beyond slippage tolerance.

        Fetches fresh orderbook from CLOB and compares to detected price.

        Args:
            leg: Leg dict from NegriskOpportunity.legs

        Returns:
            True if price is within tolerance, False if slipped too much
        """
        if not self._clob_client:
            logger.warning("No CLOB client for slippage check, skipping")
            return True

        try:
            book = self._clob_client.get_order_book(leg["token_id"])

            if leg["side"] == "BUY":
                asks = book.get("asks", [])
                if not asks:
                    logger.warning(f"No asks for {leg['token_id'][:16]}...")
                    return False
                current_price = min(float(a["price"]) for a in asks)
            else:
                bids = book.get("bids", [])
                if not bids:
                    logger.warning(f"No bids for {leg['token_id'][:16]}...")
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
            logger.warning(f"Slippage check error for {leg['token_id'][:16]}: {e}")
            return False

    async def _place_legs_batch(self, legs: list[dict]) -> list[LegOrderResult]:
        """
        Place all leg orders in a single batch via post_orders().

        Creates signed orders locally (~1ms each, no network) then submits
        all in one HTTP call instead of N sequential calls. This reduces
        execution latency from ~150ms × N legs to ~150ms total.

        Falls back to sequential placement if batch API fails.

        Args:
            legs: List of leg dicts from NegriskOpportunity.legs

        Returns:
            List of LegOrderResult, one per leg
        """
        try:
            from py_clob_client.clob_types import (
                OrderArgs, PostOrdersArgs, OrderType, CreateOrderOptions
            )
            from py_clob_client.order_builder.constants import BUY, SELL

            # Step 1: Create all signed orders locally (fast, no network)
            tick = float(self.tick_size)
            batch_args = []
            for leg in legs:
                sdk_side = BUY if leg["side"] == "BUY" else SELL

                # Worst-price limit with slippage tolerance, rounded to tick size
                if leg["side"] == "BUY":
                    raw_price = min(
                        leg["price"] * (1 + self.slippage_tolerance), 0.99
                    )
                    # Round UP to nearest tick for BUY (willing to pay more)
                    worst_price = round(
                        min(round(raw_price / tick) * tick + tick, 0.99),
                        len(self.tick_size.split(".")[-1]) if "." in self.tick_size else 1,
                    )
                else:
                    raw_price = max(
                        leg["price"] * (1 - self.slippage_tolerance), 0.01
                    )
                    # Round DOWN to nearest tick for SELL (willing to accept less)
                    worst_price = round(
                        max(round(raw_price / tick) * tick, 0.01),
                        len(self.tick_size.split(".")[-1]) if "." in self.tick_size else 1,
                    )

                options = CreateOrderOptions(
                    tick_size=self.tick_size,
                    neg_risk=True,
                )

                signed_order = self._clob_client.create_order(
                    OrderArgs(
                        token_id=leg["token_id"],
                        price=worst_price,
                        size=leg["size"],
                        side=sdk_side,
                    ),
                    options=options,
                )

                batch_args.append(PostOrdersArgs(
                    order=signed_order,
                    orderType=OrderType.FOK,
                ))

            logger.info(
                f"Placing batch of {len(batch_args)} FOK orders via post_orders()"
            )

            # Step 2: Submit all orders in one HTTP call
            resp = self._clob_client.post_orders(batch_args)

            # Step 3: Parse batch response
            results = []
            # Response format may vary — handle list and dict formats
            if isinstance(resp, list):
                order_responses = resp
            elif isinstance(resp, dict):
                order_responses = resp.get("orders", resp.get("results", [resp]))
            else:
                order_responses = [{}] * len(legs)

            for i, leg in enumerate(legs):
                if i < len(order_responses):
                    order_resp = order_responses[i] if isinstance(order_responses[i], dict) else {}
                    order_id = order_resp.get("orderID", f"batch_{i}")
                    status = order_resp.get("status", "unknown")
                    success = status in ("matched", "live")

                    logger.info(
                        f"Batch leg {i + 1}/{len(legs)}: "
                        f"{leg['outcome_name'][:30]} | "
                        f"status={status} | id={order_id}"
                    )

                    results.append(LegOrderResult(
                        success=success,
                        order_id=order_id,
                        token_id=leg["token_id"],
                        side=leg["side"],
                        price=leg["price"],
                        size=leg["size"],
                        filled_size=leg["size"] if success else 0.0,
                        error=None if success else f"Batch order not filled: status={status}",
                    ))
                else:
                    results.append(LegOrderResult(
                        success=False,
                        token_id=leg["token_id"],
                        side=leg["side"],
                        price=leg["price"],
                        size=leg["size"],
                        error="No response for this leg in batch",
                    ))

            return results

        except Exception as e:
            logger.warning(f"Batch placement failed ({e}), falling back to sequential")
            # Fall back to sequential placement
            results = []
            for leg in legs:
                result = await self._place_leg_order(leg)
                results.append(result)
            return results

    async def _place_leg_order(self, leg: dict) -> LegOrderResult:
        """
        Place a single FOK market order for one leg via py-clob-client.

        For BUY legs: amount = dollar amount to spend (price * size)
        For SELL legs: amount = number of shares to sell
        price param = worst-price slippage limit

        Args:
            leg: Leg dict with token_id, price, size, side, outcome_name

        Returns:
            LegOrderResult with fill details
        """
        try:
            from py_clob_client.order_builder.constants import BUY, SELL

            sdk_side = BUY if leg["side"] == "BUY" else SELL

            # For BUY: amount is the dollar amount to spend
            # For SELL: amount is the number of shares to sell
            if leg["side"] == "BUY":
                amount = leg["price"] * leg["size"]
            else:
                amount = leg["size"]

            # Worst-price limit: allow slippage_tolerance above/below detected price
            if leg["side"] == "BUY":
                worst_price = min(
                    leg["price"] * (1 + self.slippage_tolerance), 0.99
                )
            else:
                worst_price = max(
                    leg["price"] * (1 - self.slippage_tolerance), 0.01
                )

            options = {
                "tick_size": self.tick_size,
                "neg_risk": True,
            }

            logger.info(
                f"Placing FOK {leg['side']} order: "
                f"{leg['outcome_name'][:40]} | "
                f"amount=${amount:.2f} | "
                f"worst_price={worst_price:.4f} | "
                f"token={leg['token_id'][:16]}..."
            )

            from py_clob_client.clob_types import OrderType

            signed_order = self._clob_client.create_market_order(
                token_id=leg["token_id"],
                side=sdk_side,
                amount=amount,
                price=worst_price,
                options=options,
            )

            resp = self._clob_client.post_order(signed_order, OrderType.FOK)

            # Parse response
            order_id = resp.get("orderID", "unknown")
            status = resp.get("status", "unknown")

            logger.info(
                f"Order response: id={order_id} status={status} "
                f"for {leg['outcome_name'][:30]}"
            )

            # Check if the order was matched/filled
            # FOK orders either fill completely or are cancelled
            if status in ("matched", "live"):
                return LegOrderResult(
                    success=True,
                    order_id=order_id,
                    token_id=leg["token_id"],
                    side=leg["side"],
                    price=leg["price"],
                    size=leg["size"],
                    filled_size=leg["size"],  # FOK = all or nothing
                )
            else:
                return LegOrderResult(
                    success=False,
                    order_id=order_id,
                    token_id=leg["token_id"],
                    side=leg["side"],
                    price=leg["price"],
                    size=leg["size"],
                    filled_size=0.0,
                    error=f"Order not filled: status={status}",
                )

        except Exception as e:
            return LegOrderResult(
                success=False,
                token_id=leg.get("token_id", ""),
                side=leg.get("side", "BUY"),
                price=leg.get("price", 0),
                size=leg.get("size", 0),
                error=str(e),
            )

    async def _rollback_orders(self, filled_orders: list[LegOrderResult]) -> None:
        """
        Rollback filled legs by placing opposite-side FOK orders.

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
                reverse_side = "SELL" if order.side == "BUY" else "BUY"
                reverse_leg = {
                    "token_id": order.token_id,
                    "side": reverse_side,
                    "price": order.price,
                    "size": order.filled_size,
                    "outcome_name": f"rollback_{order.token_id[:8]}",
                }
                result = await self._place_leg_order(reverse_leg)
                if result.success:
                    logger.info(
                        f"Rollback success: {reverse_side} "
                        f"{order.filled_size} @ {order.price}"
                    )
                else:
                    logger.error(
                        f"Rollback FAILED for {order.token_id[:16]}: "
                        f"{result.error}"
                    )
            except Exception as e:
                logger.error(f"Rollback error for {order.token_id[:16]}: {e}")

    def _simulate_execution(
        self, opportunity: NegriskOpportunity, start_time: float
    ) -> ExecutionResult:
        """
        Simulate execution in dry-run mode.

        Logs what would happen without calling the SDK.

        Args:
            opportunity: The opportunity to simulate
            start_time: monotonic start time for elapsed calculation

        Returns:
            ExecutionResult marked as dry-run simulation
        """
        self._stats["dry_run_simulations"] += 1

        simulated_orders = []
        total_cost = 0.0

        for leg in opportunity.legs:
            cost = leg["price"] * leg["size"]
            total_cost += cost
            simulated_orders.append(
                LegOrderResult(
                    success=True,
                    order_id=f"DRY_RUN_{leg['token_id'][:8]}",
                    token_id=leg["token_id"],
                    side=leg["side"],
                    price=leg["price"],
                    size=leg["size"],
                    filled_size=leg["size"],
                )
            )

        elapsed = (time.monotonic() - start_time) * 1000

        logger.info(
            f"DRY_RUN: Would execute {opportunity.direction.value} "
            f"on {opportunity.event.title[:50]} | "
            f"{len(opportunity.legs)} legs | "
            f"cost=${total_cost:.2f} | "
            f"net_edge={opportunity.net_edge:.4f} "
            f"({opportunity.net_edge * 100:.2f}%)"
        )

        for i, leg in enumerate(opportunity.legs):
            logger.info(
                f"  DRY_RUN Leg {i + 1}: {leg['side']} "
                f"{leg['outcome_name'][:40]} "
                f"@ ${leg['price']:.4f} x {leg['size']:.0f}"
            )

        return ExecutionResult(
            success=True,
            reason="DRY_RUN simulation",
            orders=simulated_orders,
            total_cost=total_cost,
            execution_time_ms=elapsed,
        )

    def get_stats(self) -> dict:
        """Get executor statistics."""
        return {
            "platform": "polymarket",
            "dry_run": self.dry_run,
            "initialized": self._initialized,
            **self._stats,
        }
