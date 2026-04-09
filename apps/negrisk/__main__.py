"""
CLI entrypoint for the negrisk app.
"""

from __future__ import annotations

import argparse
import asyncio
import sys


def _run_scan(argv: list[str]) -> None:
    from apps.negrisk import scan

    sys.argv = ["apps.negrisk.scan", *argv]
    scan.main()


def _run_dashboard(argv: list[str]) -> None:
    from apps.negrisk import dashboard

    sys.argv = ["apps.negrisk.dashboard", *argv]
    dashboard.main()


def _run_long_test(argv: list[str]) -> None:
    from apps.negrisk import long_test

    sys.argv = ["apps.negrisk.long_test", *argv]
    asyncio.run(long_test.main())


def main() -> None:
    parser = argparse.ArgumentParser(description="Negrisk application CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="Run negrisk/arbitrage scan or trading bot")
    scan_parser.add_argument("-c", "--config", default="config/negrisk.yaml")
    scan_parser.add_argument("--live", action="store_true")
    scan_parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    scan_parser.add_argument("--backtest", action="store_true")
    scan_parser.add_argument("--backtest-duration", type=float)
    scan_parser.add_argument("-v", "--verbose", action="store_true")

    dashboard_parser = subparsers.add_parser("dashboard", help="Run negrisk dashboard")
    dashboard_parser.add_argument("-c", "--config", default="config/negrisk.yaml")
    dashboard_parser.add_argument("--port", type=int, default=8888)
    dashboard_parser.add_argument("--live", action="store_true")
    dashboard_parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    dashboard_parser.add_argument("-v", "--verbose", action="store_true")

    long_test_parser = subparsers.add_parser("long-test", help="Run long-running negrisk scanner")
    long_test_parser.add_argument("args", nargs=argparse.REMAINDER)

    args, unknown = parser.parse_known_args()

    if args.command == "scan":
        forwarded = ["-c", args.config, *unknown]
        if args.live:
            forwarded.append("--live")
        if args.dry_run:
            forwarded.append("--dry-run")
        if args.backtest:
            forwarded.append("--backtest")
        if args.backtest_duration is not None:
            forwarded.extend(["--backtest-duration", str(args.backtest_duration)])
        if args.verbose:
            forwarded.append("-v")
        _run_scan(forwarded)
        return

    if args.command == "dashboard":
        forwarded = ["-c", args.config, "--port", str(args.port), *unknown]
        if args.live:
            forwarded.append("--live")
        if args.dry_run:
            forwarded.append("--dry-run")
        if args.verbose:
            forwarded.append("-v")
        _run_dashboard(forwarded)
        return

    _run_long_test(args.args)


if __name__ == "__main__":
    main()
