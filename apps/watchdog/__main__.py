"""
CLI entrypoint for the watchdog app.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from apps.watchdog.config import load_cli_defaults


def _append_flag(forwarded: list[str], flag: str, value) -> None:
    if value is None:
        return
    if isinstance(value, bool):
        if value:
            forwarded.append(flag)
        return
    if isinstance(value, list):
        if value:
            forwarded.extend([flag, ",".join(str(item) for item in value)])
        return
    forwarded.extend([flag, str(value)])


def _run_polymarket_scan(config_path: str, user_args: list[str]) -> None:
    from apps.watchdog import polymarket_runner

    defaults = load_cli_defaults(config_path)
    forwarded: list[str] = []
    _append_flag(forwarded, "--duration", defaults.get("duration"))
    _append_flag(forwarded, "--keywords", defaults.get("keywords"))
    _append_flag(forwarded, "--watch-slugs", defaults.get("watch_slugs"))
    _append_flag(forwarded, "--min-volume", defaults.get("min_volume"))
    _append_flag(forwarded, "--poll-interval", defaults.get("poll_interval"))
    _append_flag(forwarded, "--cooldown", defaults.get("cooldown"))
    if defaults.get("news_check_enabled") is False:
        forwarded.append("--no-news")
    forwarded.extend(user_args)
    sys.argv = ["apps.watchdog.polymarket_runner", *forwarded]
    asyncio.run(polymarket_runner.main())


def _run_kalshi_scan(config_path: str, user_args: list[str]) -> None:
    from apps.watchdog import kalshi_runner

    defaults = load_cli_defaults(config_path)
    forwarded: list[str] = []
    _append_flag(forwarded, "--duration", defaults.get("duration"))
    _append_flag(forwarded, "--keywords", defaults.get("keywords"))
    _append_flag(forwarded, "--watch-events", defaults.get("watch_events"))
    _append_flag(forwarded, "--watch-series", defaults.get("watch_series"))
    _append_flag(forwarded, "--categories", defaults.get("categories"))
    _append_flag(forwarded, "--min-volume", defaults.get("min_volume"))
    _append_flag(forwarded, "--poll-interval", defaults.get("poll_interval"))
    _append_flag(forwarded, "--cooldown", defaults.get("cooldown"))
    if defaults.get("news_check_enabled") is False:
        forwarded.append("--no-news")
    if defaults.get("demo"):
        forwarded.append("--demo")
    if defaults.get("no_ws"):
        forwarded.append("--no-ws")
    forwarded.extend(user_args)
    sys.argv = ["apps.watchdog.kalshi_runner", *forwarded]
    asyncio.run(kalshi_runner.main())


def _run_backtest(user_args: list[str]) -> None:
    from apps.watchdog import backtest

    sys.argv = ["apps.watchdog.backtest", *user_args]
    asyncio.run(backtest.main())


def main() -> None:
    parser = argparse.ArgumentParser(description="Watchdog application CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="Run suspicious-activity watchdog")
    scan_parser.add_argument("--platform", choices=("polymarket", "kalshi"), default="polymarket")
    scan_parser.add_argument("--config", default=None)
    backtest_parser = subparsers.add_parser("backtest", help="Run watchdog backtests")

    args, unknown = parser.parse_known_args()

    if args.command == "backtest":
        _run_backtest(unknown)
        return

    if args.platform == "kalshi":
        config_path = args.config or "config/watchdog.kalshi.yaml"
        _run_kalshi_scan(config_path, unknown)
        return

    config_path = args.config or "config/watchdog.polymarket.yaml"
    _run_polymarket_scan(config_path, unknown)


if __name__ == "__main__":
    main()
