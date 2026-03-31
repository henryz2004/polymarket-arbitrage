#!/usr/bin/env python3
"""
Watchdog Backtest Runner
=========================

Replays historical price data through the watchdog anomaly detector
to verify it would have caught known insider trading events.

Usage:
    # Run all built-in Iran scenarios
    python backtest_runner.py

    # Run specific scenario by name
    python backtest_runner.py --scenario "Iran strike"

    # Run against a custom slug
    python backtest_runner.py --slug us-x-iran-ceasefire-by --start 2026-03-01 --end 2026-03-15

    # Use cached data (no API calls after first run)
    python backtest_runner.py --cache

    # Save results to JSONL
    python backtest_runner.py --output results.jsonl

    # Custom thresholds (tune sensitivity)
    python backtest_runner.py --abs-threshold "0.03,1800" --rel-threshold "0.30,3600"
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from core.watchdog.backtester import (
    ALL_SCENARIOS,
    IRAN_SCENARIOS,
    OTHER_SCENARIOS,
    BacktestScenario,
    WatchdogBacktester,
)
from core.watchdog.models import WatchdogConfig


def setup_logging():
    """Setup console logging."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s", datefmt="%H:%M:%S"
    ))
    root.addHandler(handler)


def parse_date(s: str) -> datetime:
    """Parse a date string (YYYY-MM-DD or YYYY-MM-DD HH:MM)."""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Invalid date format: {s} (use YYYY-MM-DD or 'YYYY-MM-DD HH:MM')")


def print_report(results, verbose=False):
    """Print a human-readable backtest report."""
    print()
    print("=" * 80)
    print("WATCHDOG BACKTEST REPORT")
    print("=" * 80)
    print()

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    total_alerts = sum(len(r.alerts) for r in results)

    print(f"Scenarios: {len(results)} | Passed: {passed} | Failed: {failed} | Total alerts: {total_alerts}")
    print()

    for r in results:
        print("-" * 80)
        print(r.summary())
        print()

        if verbose and r.alerts:
            print("  Detailed alerts:")
            for a in r.alerts:
                print(f"    Time: {a.simulated_time:%Y-%m-%d %H:%M:%S}")
                print(f"    Outcome: {a.alert.outcome_name}")
                print(f"    Move: {a.alert.price_before:.4f} -> {a.alert.price_after:.4f}")
                print(f"    Change: {a.alert.pct_change:.1%} ({a.alert.abs_change:.4f} abs)")
                print(f"    Window: {a.alert.window_seconds}s ({a.alert.threshold_type})")
                print(f"    Score: {a.alert.suspicion_score:.1f}/10")
                print(f"    Off-hours: {a.alert.is_off_hours}")
                print()

    print("=" * 80)

    # Verdict
    if failed == 0:
        print("ALL SCENARIOS PASSED — detector would have caught all known events")
    else:
        print(f"WARNING: {failed} scenario(s) FAILED — detector needs tuning")
        for r in results:
            if not r.passed:
                print(f"  FAILED: {r.scenario.name}")
                if r.scenario.expect_alert and not r.caught:
                    print(f"    Expected alert but none fired. Consider:")
                    print(f"    - Lowering absolute thresholds (current: {r.scenario.name})")
                    print(f"    - Increasing price_history_window_hours")
                    print(f"    - Check if CLOB has data for this time range")

    print("=" * 80)
    print()


async def main():
    parser = argparse.ArgumentParser(
        description="Watchdog Backtest Runner — verify detector against known insider events",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python backtest_runner.py                       # All Iran scenarios
  python backtest_runner.py --scenario "strike"   # Filter scenarios by name
  python backtest_runner.py --slug my-event-slug  # Custom slug
  python backtest_runner.py --cache               # Use cached API data
  python backtest_runner.py -v                    # Verbose output
        """,
    )

    parser.add_argument(
        "--scenario", type=str, default=None,
        help="Filter built-in scenarios by name substring"
    )
    parser.add_argument(
        "--slug", type=str, default=None,
        help="Run against a custom event slug (creates ad-hoc scenario)"
    )
    parser.add_argument(
        "--start", type=str, default=None,
        help="Start date for custom slug (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--end", type=str, default=None,
        help="End date for custom slug (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--cache", action="store_true", default=False,
        help="Use cached price data (faster, reproducible)"
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Save results to JSONL file"
    )
    parser.add_argument(
        "--all", action="store_true", default=False,
        help="Run ALL scenarios (Iran + non-Iran)"
    )
    parser.add_argument(
        "--abs-threshold", type=str, default=None,
        help="Override absolute threshold: 'cents,seconds' (e.g. '0.03,1800')"
    )
    parser.add_argument(
        "--rel-threshold", type=str, default=None,
        help="Override relative threshold: 'pct,seconds' (e.g. '0.30,3600')"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", default=False,
        help="Verbose output (show all alert details)"
    )

    args = parser.parse_args()
    setup_logging()

    # Build config with optional overrides
    config = WatchdogConfig()

    if args.abs_threshold:
        parts = args.abs_threshold.split(",")
        cents = float(parts[0])
        seconds = int(parts[1])
        config.absolute_thresholds = [(cents, seconds)] + config.absolute_thresholds
        print(f"Added absolute threshold: {cents*100:.0f}c in {seconds}s")

    if args.rel_threshold:
        parts = args.rel_threshold.split(",")
        pct = float(parts[0])
        seconds = int(parts[1])
        config.relative_thresholds = [(pct, seconds)] + config.relative_thresholds
        print(f"Added relative threshold: {pct*100:.0f}% in {seconds}s")

    # Select scenarios
    if args.slug:
        scenarios = [BacktestScenario(
            name=f"Custom: {args.slug}",
            slug=args.slug,
            description="Custom backtest scenario",
            start_time=parse_date(args.start) if args.start else None,
            end_time=parse_date(args.end) if args.end else None,
            expect_alert=True,
        )]
    elif args.all:
        scenarios = ALL_SCENARIOS
    elif args.scenario:
        scenarios = [
            s for s in ALL_SCENARIOS
            if args.scenario.lower() in s.name.lower()
        ]
        if not scenarios:
            print(f"No scenarios match '{args.scenario}'")
            print("Available scenarios:")
            for s in ALL_SCENARIOS:
                print(f"  - {s.name}")
            sys.exit(1)
    else:
        scenarios = IRAN_SCENARIOS

    print(f"\nRunning {len(scenarios)} scenario(s)...")

    # Run
    backtester = WatchdogBacktester(config)

    if args.cache:
        results = await backtester.run_and_cache(scenarios)
    else:
        results = await backtester.run(scenarios)

    # Report
    print_report(results, verbose=args.verbose)

    # Save to file
    if args.output:
        output_path = Path(args.output)
        with open(output_path, "w") as f:
            for r in results:
                record = {
                    "scenario": r.scenario.name,
                    "slug": r.scenario.slug,
                    "passed": r.passed,
                    "caught": r.caught,
                    "alerts_count": len(r.alerts),
                    "max_score": r.max_score,
                    "first_alert": r.first_alert_time.isoformat() if r.first_alert_time else None,
                    "caught_during_insider_window": r.caught_during_insider_window,
                    "tokens": r.tokens_fetched,
                    "price_points": r.price_points_total,
                    "time_range": [
                        r.time_range[0].isoformat() if r.time_range else None,
                        r.time_range[1].isoformat() if r.time_range else None,
                    ],
                    "alerts": [a.to_dict() for a in r.alerts],
                }
                f.write(json.dumps(record, default=str) + "\n")
        print(f"Results saved to {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
