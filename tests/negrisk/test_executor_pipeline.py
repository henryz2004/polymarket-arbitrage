#!/usr/bin/env python3
"""
Pipelined Executor Integration Test
======================================

Runs the LimitlessExecutor through multiple simulated market periods:
1. Normal market — tight spreads, all legs fill
2. Volatile market — slippage on some legs, rejections expected
3. Illiquid market — empty orderbooks, graceful failure
4. Partial failure — some legs fill, rollback triggered
5. High-frequency burst — rapid-fire opportunities to test stats

Each period feeds synthetic opportunities through the executor and validates
behavior, stats, and edge cases.
"""

import asyncio
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

from core.negrisk.models import (
    ArbDirection,
    NegriskConfig,
    NegriskEvent,
    NegriskOpportunity,
    Outcome,
    OutcomeBBA,
)
from core.negrisk.platforms.limitless.executor import (
    ExecutionResult,
    LegOrderResult,
    LimitlessExecutor,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ── Helpers ─────────────────────────────────────────────────────────────────

def make_opportunity(
    num_legs: int = 3,
    direction: ArbDirection = ArbDirection.BUY_ALL,
    net_edge: float = 0.05,
    size: float = 100.0,
    base_price: float = 0.30,
    opp_id: str = "opp_test",
    event_title: str = "Test Event",
) -> NegriskOpportunity:
    """Create a synthetic opportunity with N legs."""
    outcomes = []
    legs = []
    for i in range(num_legs):
        price = round(base_price + i * 0.01, 4)
        outcomes.append(Outcome(
            outcome_id=f"o{i}",
            market_id=f"market-{i}",
            condition_id="c1",
            token_id=f"token_{i}",
            name=f"Outcome {i}",
            bba=OutcomeBBA(best_ask=price, ask_size=200.0,
                           best_bid=price - 0.02, bid_size=200.0),
        ))
        legs.append({
            "market_id": f"market-{i}",
            "token_id": f"token_{i}",
            "outcome_name": f"Outcome {i}",
            "price": price,
            "size": size,
            "side": "BUY" if direction == ArbDirection.BUY_ALL else "SELL",
        })

    event = NegriskEvent(
        event_id="e_test",
        slug="test-event",
        title=event_title,
        condition_id="c1",
        platform="limitless",
        volume_24h=10000.0,
        outcomes=outcomes,
    )

    return NegriskOpportunity(
        opportunity_id=opp_id,
        event=event,
        platform="limitless",
        direction=direction,
        sum_of_prices=sum(leg["price"] for leg in legs),
        gross_edge=net_edge + 0.01,
        net_edge=net_edge,
        suggested_size=size,
        max_size=size * 2,
        legs=legs,
    )


def install_sdk_mocks():
    """Install mock limitless-sdk and eth-account modules."""
    mock_sdk_orders = ModuleType("limitless_sdk.orders")
    mock_sdk_orders.Side = MagicMock()
    mock_sdk_orders.Side.BUY = "BUY"
    mock_sdk_orders.Side.SELL = "SELL"
    mock_sdk_orders.OrderType = MagicMock()
    mock_sdk_orders.OrderType.FOK = "FOK"
    mock_sdk_orders.OrderClient = MagicMock()

    mock_sdk_api = ModuleType("limitless_sdk.api")
    mock_sdk_api.HttpClient = MagicMock()

    mock_sdk = ModuleType("limitless_sdk")

    mock_eth = ModuleType("eth_account")
    mock_account = MagicMock()
    mock_account.from_key.return_value = MagicMock(address="0xTestWallet")
    mock_eth.Account = mock_account

    sys.modules["limitless_sdk"] = mock_sdk
    sys.modules["limitless_sdk.orders"] = mock_sdk_orders
    sys.modules["limitless_sdk.api"] = mock_sdk_api
    sys.modules["eth_account"] = mock_eth


def cleanup_sdk_mocks():
    """Remove mock SDK modules."""
    for mod in ["limitless_sdk", "limitless_sdk.orders",
                "limitless_sdk.api", "eth_account"]:
        sys.modules.pop(mod, None)


# ── Period Tests ────────────────────────────────────────────────────────────

async def period_1_dry_run_normal():
    """
    Period 1: Dry-Run — Normal Market
    Multiple opportunities with varying legs and edges.
    Executor should simulate all successfully.
    """
    logger.info("=" * 70)
    logger.info("PERIOD 1: Dry-Run Normal Market")
    logger.info("=" * 70)

    executor = LimitlessExecutor(api_client=MagicMock(), dry_run=True)
    await executor.initialize()

    results = []
    for i in range(5):
        opp = make_opportunity(
            num_legs=3 + (i % 4),  # 3-6 legs
            net_edge=0.03 + i * 0.01,
            size=50 + i * 25,
            opp_id=f"p1_opp_{i}",
            event_title=f"Period 1 Event {i}",
        )
        result = await executor.execute_opportunity(opp)
        results.append(result)

    stats = executor.get_stats()
    assert all(r.success for r in results), "All dry-run should succeed"
    assert stats["dry_run_simulations"] == 5
    assert stats["opportunities_received"] == 5
    assert stats["executions_attempted"] == 0
    logger.info(f"  PASS: 5/5 dry-run simulations OK | stats={stats}")
    return stats


async def period_2_dry_run_sell_side():
    """
    Period 2: Dry-Run — Sell-Side Opportunities
    Validates SELL_ALL direction works in dry-run mode.
    """
    logger.info("=" * 70)
    logger.info("PERIOD 2: Dry-Run Sell-Side")
    logger.info("=" * 70)

    executor = LimitlessExecutor(api_client=MagicMock(), dry_run=True)
    await executor.initialize()

    results = []
    for i in range(3):
        opp = make_opportunity(
            num_legs=3,
            direction=ArbDirection.SELL_ALL,
            net_edge=0.05 + i * 0.02,
            size=100,
            base_price=0.40,
            opp_id=f"p2_sell_{i}",
            event_title=f"Period 2 Sell Event {i}",
        )
        result = await executor.execute_opportunity(opp)
        results.append(result)

    assert all(r.success for r in results), "All sell-side dry-runs should succeed"
    # Verify legs have SELL side
    for r in results:
        assert all(o.side == "SELL" for o in r.orders)

    stats = executor.get_stats()
    logger.info(f"  PASS: 3/3 sell-side dry-runs OK | stats={stats}")
    return stats


async def period_3_live_slippage_rejection():
    """
    Period 3: Live Sim — Volatile Market (Slippage Rejections)
    Orderbook prices have moved significantly from detection time.
    """
    logger.info("=" * 70)
    logger.info("PERIOD 3: Live Sim — Volatile Market (Slippage)")
    logger.info("=" * 70)

    install_sdk_mocks()
    try:
        # Orderbook returns prices 10% higher than detected
        class VolatileAPI:
            async def get_orderbook(self, slug):
                return {
                    "asks": [{"price": 0.50, "size": 100}],
                    "bids": [{"price": 0.48, "size": 100}],
                }

        executor = LimitlessExecutor(
            api_client=VolatileAPI(),
            api_key="test", private_key="0x" + "a" * 64,
            dry_run=False, slippage_tolerance=0.02,
        )
        await executor.initialize()

        results = []
        for i in range(4):
            opp = make_opportunity(
                net_edge=0.05,
                opp_id=f"p3_volatile_{i}",
                event_title=f"Period 3 Volatile {i}",
            )
            result = await executor.execute_opportunity(opp)
            results.append(result)

        assert all(not r.success for r in results), "All should be rejected (slippage)"
        stats = executor.get_stats()
        assert stats["slippage_rejections"] == 4
        assert stats["executions_attempted"] == 4
        assert stats["executions_succeeded"] == 0
        logger.info(f"  PASS: 4/4 slippage rejections OK | stats={stats}")
        return stats
    finally:
        cleanup_sdk_mocks()


async def period_4_live_all_fills():
    """
    Period 4: Live Sim — Normal Market (All Legs Fill)
    Prices are stable, SDK returns successful fills.
    """
    logger.info("=" * 70)
    logger.info("PERIOD 4: Live Sim — Normal Market (All Fill)")
    logger.info("=" * 70)

    install_sdk_mocks()
    try:
        # Return prices matching detected
        class StableAPI:
            PRICES = {f"market-{i}": 0.30 + i * 0.01 for i in range(10)}
            async def get_orderbook(self, slug):
                p = self.PRICES.get(slug, 0.30)
                return {
                    "asks": [{"price": p, "size": 500}],
                    "bids": [{"price": p - 0.02, "size": 500}],
                }

        executor = LimitlessExecutor(
            api_client=StableAPI(),
            api_key="test", private_key="0x" + "a" * 64,
            dry_run=False,
        )
        await executor.initialize()

        # Mock SDK to always fill
        order_counter = 0
        async def mock_fill(**kwargs):
            nonlocal order_counter
            order_counter += 1
            return {"id": f"order_{order_counter}", "filled_size": 100.0}

        executor._order_client.create_order = mock_fill

        results = []
        for i in range(3):
            opp = make_opportunity(
                num_legs=3 + i,  # 3, 4, 5 legs
                size=100,
                opp_id=f"p4_fill_{i}",
                event_title=f"Period 4 Fill Event {i}",
            )
            result = await executor.execute_opportunity(opp)
            results.append(result)

        assert all(r.success for r in results), "All should fill successfully"
        assert results[0].total_cost > 0
        assert len(results[0].orders) == 3
        assert len(results[1].orders) == 4
        assert len(results[2].orders) == 5

        stats = executor.get_stats()
        assert stats["executions_succeeded"] == 3
        assert stats["total_volume_usd"] > 0
        assert order_counter == 12  # 3 + 4 + 5 = 12 legs total
        logger.info(f"  PASS: 3/3 full executions OK ({order_counter} legs) | stats={stats}")
        return stats
    finally:
        cleanup_sdk_mocks()


async def period_5_live_partial_failure_rollback():
    """
    Period 5: Live Sim — Partial Failure + Rollback
    First N-1 legs fill, last leg fails. Rollback should trigger.
    """
    logger.info("=" * 70)
    logger.info("PERIOD 5: Live Sim — Partial Failure + Rollback")
    logger.info("=" * 70)

    install_sdk_mocks()
    try:
        class StableAPI:
            PRICES = {f"market-{i}": 0.30 + i * 0.01 for i in range(10)}
            async def get_orderbook(self, slug):
                p = self.PRICES.get(slug, 0.30)
                return {
                    "asks": [{"price": p, "size": 500}],
                    "bids": [{"price": p - 0.02, "size": 500}],
                }

        executor = LimitlessExecutor(
            api_client=StableAPI(),
            api_key="test", private_key="0x" + "a" * 64,
            dry_run=False,
        )
        await executor.initialize()

        # Test with different failure points
        test_cases = [
            (4, 3, "leg 3 of 4"),   # 4 legs, fail at leg 3 → rollback 2
            (5, 4, "leg 4 of 5"),   # 5 legs, fail at leg 4 → rollback 3
            (3, 1, "leg 1 of 3"),   # 3 legs, fail at leg 1 → rollback 0 (nothing to rollback)
        ]

        for num_legs, fail_at, desc in test_cases:
            call_count = 0

            async def make_mock_create(fail_point):
                counter = 0
                async def mock_create(**kwargs):
                    nonlocal counter
                    counter += 1
                    if counter == fail_point:
                        raise Exception(f"FOK rejected at leg {fail_point}")
                    return {"id": f"order_{counter}", "filled_size": 100.0}
                return mock_create

            executor._order_client.create_order = await make_mock_create(fail_at)

            opp = make_opportunity(
                num_legs=num_legs,
                size=100,
                opp_id=f"p5_fail_{desc.replace(' ', '_')}",
                event_title=f"Period 5 Fail at {desc}",
            )
            result = await executor.execute_opportunity(opp)

            assert not result.success, f"Should fail ({desc})"
            assert f"Leg {fail_at} failed" in result.reason
            logger.info(f"  PASS: {desc} — failed correctly, reason='{result.reason}'")

        stats = executor.get_stats()
        assert stats["executions_failed"] == 3
        # Rollback happens for first 2 cases (legs 1-2 and legs 1-3), not the 3rd (fails on leg 1)
        assert stats["rollbacks_attempted"] == 2
        logger.info(f"  PASS: 3/3 partial failures handled | rollbacks={stats['rollbacks_attempted']}")
        return stats
    finally:
        cleanup_sdk_mocks()


async def period_6_live_empty_orderbook():
    """
    Period 6: Live Sim — Illiquid Market (Empty Orderbooks)
    Some markets have no asks/bids — executor should reject gracefully.
    """
    logger.info("=" * 70)
    logger.info("PERIOD 6: Live Sim — Illiquid Market (Empty Books)")
    logger.info("=" * 70)

    install_sdk_mocks()
    try:
        class IlliquidAPI:
            async def get_orderbook(self, slug):
                # market-0 has no asks
                if slug == "market-0":
                    return {"asks": [], "bids": []}
                return {
                    "asks": [{"price": 0.31, "size": 500}],
                    "bids": [{"price": 0.29, "size": 500}],
                }

        executor = LimitlessExecutor(
            api_client=IlliquidAPI(),
            api_key="test", private_key="0x" + "a" * 64,
            dry_run=False,
        )
        await executor.initialize()

        opp = make_opportunity(opp_id="p6_illiquid", event_title="Illiquid Event")
        result = await executor.execute_opportunity(opp)

        assert not result.success
        assert "slippage" in result.reason.lower() or "Slippage" in result.reason
        logger.info(f"  PASS: Illiquid market rejected — reason='{result.reason}'")

        stats = executor.get_stats()
        return stats
    finally:
        cleanup_sdk_mocks()


async def period_7_high_frequency_burst():
    """
    Period 7: Dry-Run — High-Frequency Burst
    20 opportunities in rapid succession. Validates stats accumulate correctly.
    """
    logger.info("=" * 70)
    logger.info("PERIOD 7: Dry-Run — High-Frequency Burst (20 opps)")
    logger.info("=" * 70)

    executor = LimitlessExecutor(api_client=MagicMock(), dry_run=True)
    await executor.initialize()

    tasks = []
    for i in range(20):
        opp = make_opportunity(
            num_legs=3,
            net_edge=0.03 + (i % 5) * 0.01,
            size=50 + i * 5,
            opp_id=f"p7_burst_{i}",
            event_title=f"Burst {i}",
        )
        tasks.append(executor.execute_opportunity(opp))

    results = await asyncio.gather(*tasks)

    assert all(r.success for r in results)
    stats = executor.get_stats()
    assert stats["dry_run_simulations"] == 20
    assert stats["opportunities_received"] == 20

    # Verify total cost varies per opportunity
    costs = [r.total_cost for r in results]
    assert len(set(round(c, 2) for c in costs)) > 1, "Costs should vary with size"

    logger.info(f"  PASS: 20/20 burst simulations OK | stats={stats}")
    return stats


# ── Pipeline Runner ─────────────────────────────────────────────────────────

async def run_pipeline():
    """Run all periods sequentially and report summary."""
    logger.info("\n" + "=" * 70)
    logger.info("LIMITLESS EXECUTOR PIPELINE TEST")
    logger.info("=" * 70 + "\n")

    periods = [
        ("Period 1: Dry-Run Normal", period_1_dry_run_normal),
        ("Period 2: Dry-Run Sell-Side", period_2_dry_run_sell_side),
        ("Period 3: Slippage Rejections", period_3_live_slippage_rejection),
        ("Period 4: All Legs Fill", period_4_live_all_fills),
        ("Period 5: Partial Failure + Rollback", period_5_live_partial_failure_rollback),
        ("Period 6: Illiquid Market", period_6_live_empty_orderbook),
        ("Period 7: High-Frequency Burst", period_7_high_frequency_burst),
    ]

    results = {}
    passed = 0
    failed = 0

    for name, fn in periods:
        try:
            stats = await fn()
            results[name] = {"status": "PASS", "stats": stats}
            passed += 1
        except AssertionError as e:
            results[name] = {"status": "FAIL", "error": str(e)}
            failed += 1
            logger.error(f"  FAIL: {name} — {e}")
        except Exception as e:
            results[name] = {"status": "ERROR", "error": str(e)}
            failed += 1
            logger.error(f"  ERROR: {name} — {e}")
        logger.info("")

    # Summary
    logger.info("=" * 70)
    logger.info("PIPELINE SUMMARY")
    logger.info("=" * 70)
    for name, result in results.items():
        status = result["status"]
        icon = "OK" if status == "PASS" else "XX"
        extra = ""
        if status == "PASS" and "stats" in result:
            s = result["stats"]
            extra = (f" | received={s['opportunities_received']}, "
                     f"dry_run={s['dry_run_simulations']}, "
                     f"succeeded={s['executions_succeeded']}, "
                     f"failed={s['executions_failed']}, "
                     f"slippage={s['slippage_rejections']}")
        elif status != "PASS":
            extra = f" | {result.get('error', '')}"
        logger.info(f"  [{icon}] {name}{extra}")

    logger.info("")
    logger.info(f"Result: {passed}/{passed + failed} periods passed")
    logger.info("=" * 70)

    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(run_pipeline())
    sys.exit(0 if success else 1)
