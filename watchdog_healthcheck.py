#!/usr/bin/env python3
"""
Watchdog Health Check
======================

Comprehensive health check for the watchdog monitor. Outputs a structured
report suitable for both human reading and automated parsing.

Exit codes:
    0 = healthy
    1 = degraded (warnings but running)
    2 = critical (not running or major issues)

Usage:
    python watchdog_healthcheck.py               # Human-readable report
    python watchdog_healthcheck.py --json         # JSON report (for automation)
    python watchdog_healthcheck.py --quiet        # Exit code only
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


LOGS_DIR = Path("logs/watchdog")
PROJECT_DIR = Path(__file__).parent


def get_watchdog_pids() -> list[dict]:
    """Find running watchdog processes."""
    try:
        result = subprocess.run(
            ["pgrep", "-fl", "watchdog_runner"],
            capture_output=True, text=True, timeout=5,
        )
        pids = []
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                parts = line.strip().split(None, 1)
                pids.append({"pid": int(parts[0]), "cmd": parts[1] if len(parts) > 1 else ""})
        return pids
    except Exception:
        return []


def get_latest_log_file() -> Optional[Path]:
    """Find the most recently modified watchdog log file."""
    logs = sorted(LOGS_DIR.glob("watchdog_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


def get_latest_stats_file() -> Optional[Path]:
    """Find the most recently modified stats file."""
    stats = sorted(LOGS_DIR.glob("watchdog_stats_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return stats[0] if stats else None


def parse_latest_stats(stats_file: Path) -> Optional[dict]:
    """Parse the last line of the stats JSONL file."""
    try:
        with open(stats_file) as f:
            lines = f.readlines()
        if lines:
            return json.loads(lines[-1])
    except Exception:
        pass
    return None


def count_alerts_today() -> tuple[int, list[dict]]:
    """Count alerts from today's alert file and return recent ones."""
    today = datetime.utcnow().strftime("%Y%m%d")
    alert_file = LOGS_DIR / f"alerts_{today}.jsonl"
    alerts = []
    if alert_file.exists():
        with open(alert_file) as f:
            for line in f:
                try:
                    alerts.append(json.loads(line))
                except Exception:
                    pass
    return len(alerts), alerts[-5:]  # Last 5 alerts


def count_total_alerts() -> int:
    """Count total alerts across all files."""
    total = 0
    for f in LOGS_DIR.glob("alerts_*.jsonl"):
        try:
            with open(f) as fh:
                total += sum(1 for _ in fh)
        except Exception:
            pass
    return total


def parse_log_tail(log_file: Path, num_lines: int = 50) -> dict:
    """Parse the tail of the log file for key indicators."""
    info = {
        "last_modified": datetime.fromtimestamp(log_file.stat().st_mtime),
        "file_size_kb": log_file.stat().st_size / 1024,
        "ws_disconnects": 0,
        "ws_connects": 0,
        "dns_errors": 0,
        "registry_refreshes": 0,
        "last_registry_refresh": None,
        "last_ws_connect": None,
        "last_stats_line": None,
        "errors": [],
    }

    try:
        with open(log_file) as f:
            lines = f.readlines()

        # Check all lines for pattern counts
        for line in lines:
            if "WebSocket closed" in line or "keepalive ping timeout" in line:
                info["ws_disconnects"] += 1
            if "WebSocket connected" in line:
                info["ws_connects"] += 1
                info["last_ws_connect"] = line.strip()[:19]
            if "nodename nor servname" in line or "Failed to fetch" in line:
                info["dns_errors"] += 1
            if "Registry refreshed" in line:
                info["registry_refreshes"] += 1
                info["last_registry_refresh"] = line.strip()[:19]
            if "ERROR" in line and "nodename" not in line:
                info["errors"].append(line.strip()[-100:])
            if "STATS" in line and "Runtime" in line:
                info["last_stats_line"] = line.strip()

    except Exception as e:
        info["errors"].append(f"Log parse error: {e}")

    return info


def check_tmux_session() -> dict:
    """Check if the watchdog tmux session exists and has the right panes."""
    info = {"exists": False, "panes": 0, "watchdog_pane": False, "monitor_pane": False}
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-t", "watchdog", "-F", "#{pane_current_command}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            info["exists"] = True
            panes = result.stdout.strip().split("\n")
            info["panes"] = len(panes)
            for pane in panes:
                if "python" in pane.lower() or "Python" in pane:
                    info["watchdog_pane"] = True
                if "bash" in pane.lower() or "zsh" in pane.lower():
                    info["monitor_pane"] = True
    except Exception:
        pass
    return info


def run_healthcheck() -> dict:
    """Run the full health check and return structured results."""
    report = {
        "timestamp": datetime.utcnow().isoformat(),
        "status": "healthy",  # healthy, degraded, critical
        "issues": [],
        "warnings": [],
        "process": {},
        "tmux": {},
        "log": {},
        "stats": {},
        "alerts": {},
    }

    # 1. Process check
    pids = get_watchdog_pids()
    report["process"] = {
        "running": len(pids) > 0,
        "pids": pids,
        "count": len(pids),
    }
    if not pids:
        report["status"] = "critical"
        report["issues"].append("Watchdog process is NOT running")

    # 2. Tmux check
    report["tmux"] = check_tmux_session()
    if not report["tmux"]["exists"]:
        report["warnings"].append("Tmux session 'watchdog' not found")

    # 3. Log file check
    log_file = get_latest_log_file()
    if log_file:
        log_info = parse_log_tail(log_file)
        age_seconds = (datetime.now() - log_info["last_modified"]).total_seconds()
        log_info["age_seconds"] = age_seconds
        log_info["stale"] = age_seconds > 600  # >10 min = stale
        log_info["file_name"] = log_file.name
        report["log"] = log_info

        if log_info["stale"]:
            if report["process"]["running"]:
                report["status"] = max(report["status"], "degraded", key=lambda x: ["healthy", "degraded", "critical"].index(x))
                report["warnings"].append(
                    f"Log file is stale ({age_seconds/60:.0f} min since last write). "
                    "Process running but may be stuck or buffering."
                )
            else:
                report["issues"].append(f"Log file stale ({age_seconds/60:.0f} min) and process not running")

        if log_info["dns_errors"] > 5:
            report["warnings"].append(f"{log_info['dns_errors']} DNS errors in current log — network issues")

        if log_info["ws_disconnects"] > 10:
            report["warnings"].append(
                f"{log_info['ws_disconnects']} WebSocket disconnects — "
                "may indicate too many subscribed tokens or network instability"
            )
    else:
        report["status"] = "critical"
        report["issues"].append("No watchdog log files found in logs/watchdog/")

    # 4. Stats check
    stats_file = get_latest_stats_file()
    if stats_file:
        stats = parse_latest_stats(stats_file)
        if stats:
            report["stats"] = {
                "runtime_seconds": stats.get("runtime_seconds", 0),
                "runtime_human": str(timedelta(seconds=int(stats.get("runtime_seconds", 0)))),
                "total_scans": stats.get("total_scans", 0),
                "total_alerts": stats.get("total_alerts", 0),
                "markets_watched": stats.get("price_tracker", {}).get("markets_watched", 0),
                "markets_with_data": stats.get("price_tracker", {}).get("markets_with_data", 0),
                "total_snapshots": stats.get("price_tracker", {}).get("total_snapshots", 0),
                "ws_messages": stats.get("websocket", {}).get("ws_messages", 0),
                "highest_score": stats.get("anomaly_detector", {}).get("highest_score", 0),
                "events_tracked": stats.get("registry", {}).get("events_tracked", 0),
                "anomaly_checks": stats.get("anomaly_detector", {}).get("checks_performed", 0),
                "stats_file": stats_file.name,
                "stats_timestamp": stats.get("timestamp", ""),
            }

            # Check for low scan rate
            runtime_h = stats.get("runtime_seconds", 0) / 3600
            scans = stats.get("total_scans", 0)
            if runtime_h > 0.5 and scans / runtime_h < 5:
                report["warnings"].append(
                    f"Low scan rate: {scans/runtime_h:.1f} scans/hour "
                    f"(expected ~60 at 1/min interval)"
                )

            # Check WS message rate
            if runtime_h > 0.5:
                ws_rate = stats.get("websocket", {}).get("ws_messages", 0) / runtime_h
                if ws_rate < 50:
                    report["warnings"].append(
                        f"Low WebSocket message rate: {ws_rate:.0f}/hour — "
                        "WS may be disconnected or frozen"
                    )
        else:
            report["warnings"].append("Could not parse stats file")
    else:
        report["warnings"].append("No stats file found")

    # 5. Alerts check
    today_count, recent_alerts = count_alerts_today()
    total_alerts = count_total_alerts()
    report["alerts"] = {
        "today": today_count,
        "total": total_alerts,
        "recent": [
            {
                "event": a.get("event_title", ""),
                "score": a.get("suspicion_score", 0),
                "news_driven": a.get("news_driven", False),
                "detected_at": a.get("detected_at", ""),
            }
            for a in recent_alerts
        ],
    }

    # Compute overall status
    if report["issues"]:
        report["status"] = "critical"
    elif report["warnings"]:
        report["status"] = "degraded"

    return report


def print_human_report(report: dict):
    """Print a human-readable health report."""
    status_icons = {"healthy": "OK", "degraded": "WARN", "critical": "CRIT"}
    status = report["status"]
    icon = status_icons.get(status, "???")

    print()
    print("=" * 70)
    print(f"WATCHDOG HEALTH CHECK  [{icon}] {status.upper()}")
    print(f"Checked at: {report['timestamp'][:19]} UTC")
    print("=" * 70)

    # Process
    proc = report["process"]
    if proc["running"]:
        print(f"\n  Process: RUNNING (PID {', '.join(str(p['pid']) for p in proc['pids'])})")
    else:
        print(f"\n  Process: NOT RUNNING")

    # Tmux
    tmux = report["tmux"]
    if tmux["exists"]:
        print(f"  Tmux: session 'watchdog' active ({tmux['panes']} panes)")
    else:
        print(f"  Tmux: session 'watchdog' not found")

    # Stats
    stats = report.get("stats", {})
    if stats:
        print(f"\n  Runtime: {stats.get('runtime_human', 'unknown')}")
        print(f"  Scans: {stats.get('total_scans', 0)} | Anomaly checks: {stats.get('anomaly_checks', 0)}")
        print(f"  Markets watched: {stats.get('markets_watched', 0)} | With data: {stats.get('markets_with_data', 0)}")
        print(f"  Events tracked: {stats.get('events_tracked', 0)}")
        print(f"  WS messages: {stats.get('ws_messages', 0)}")
        print(f"  Highest score: {stats.get('highest_score', 0)}/10")

    # Log
    log = report.get("log", {})
    if log:
        age = log.get("age_seconds", 0)
        stale_label = " (STALE)" if log.get("stale") else ""
        print(f"\n  Log: {log.get('file_name', 'none')}{stale_label}")
        print(f"  Last update: {age/60:.0f} min ago")
        print(f"  WS connects: {log.get('ws_connects', 0)} | Disconnects: {log.get('ws_disconnects', 0)}")
        print(f"  DNS errors: {log.get('dns_errors', 0)} | Registry refreshes: {log.get('registry_refreshes', 0)}")

    # Alerts
    alerts = report.get("alerts", {})
    print(f"\n  Alerts today: {alerts.get('today', 0)} | All-time: {alerts.get('total', 0)}")
    for a in alerts.get("recent", []):
        driven = "NEWS" if a["news_driven"] else "UNEXPLAINED"
        print(f"    [{a['score']:.1f}] {driven} | {a['event'][:50]} | {a['detected_at'][:16]}")

    # Issues & Warnings
    if report["issues"]:
        print(f"\n  ISSUES:")
        for issue in report["issues"]:
            print(f"    [!] {issue}")

    if report["warnings"]:
        print(f"\n  WARNINGS:")
        for warning in report["warnings"]:
            print(f"    [~] {warning}")

    print()
    print("=" * 70)
    print()


def main():
    parser = argparse.ArgumentParser(description="Watchdog health check")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--quiet", action="store_true", help="Exit code only")
    args = parser.parse_args()

    os.chdir(PROJECT_DIR)
    report = run_healthcheck()

    if args.quiet:
        pass  # Just exit with code
    elif args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_human_report(report)

    # Exit code
    if report["status"] == "critical":
        sys.exit(2)
    elif report["status"] == "degraded":
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
