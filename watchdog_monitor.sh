#!/bin/bash
# =============================================================================
# Watchdog Periodic Monitor
# =============================================================================
#
# Runs a health check every INTERVAL seconds and:
#   1. Logs each check-in to logs/watchdog/checkins.jsonl
#   2. Auto-restarts the watchdog if it's dead
#   3. Alerts (audio + visual) on critical issues
#   4. Provides a persistent tmux-based status display
#
# Usage:
#   bash watchdog_monitor.sh                    # Default: check every 2 hours
#   bash watchdog_monitor.sh --interval 3600    # Every 1 hour
#   bash watchdog_monitor.sh --interval 300     # Every 5 min (debugging)
#
# Recommended:
#   tmux new-session -d -s monitor "bash watchdog_monitor.sh"
# =============================================================================

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

PYTHON="./venv/bin/python3"
CHECKIN_LOG="logs/watchdog/checkins.jsonl"
INTERVAL=${2:-7200}  # Default 2 hours (7200 seconds)
MAX_RESTART_ATTEMPTS=3
RESTART_COOLDOWN=300  # 5 min between restart attempts

# Parse --interval flag
while [[ $# -gt 0 ]]; do
    case $1 in
        --interval) INTERVAL="$2"; shift 2 ;;
        *) shift ;;
    esac
done

mkdir -p logs/watchdog

# Track restart attempts to avoid infinite loops
RESTART_ATTEMPTS=0
LAST_RESTART_TIME=0

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [MONITOR] $1"
}

log_checkin() {
    local status="$1"
    local action="$2"
    local details="$3"
    local timestamp
    timestamp=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
    echo "{\"timestamp\": \"$timestamp\", \"status\": \"$status\", \"action\": \"$action\", \"details\": \"$details\"}" >> "$CHECKIN_LOG"
}

alert_critical() {
    local msg="$1"
    log "CRITICAL: $msg"
    # Audio alert (non-blocking)
    afplay /System/Library/Sounds/Sosumi.aiff 2>/dev/null &
    say "watchdog critical. $msg" -v Trinoids 2>/dev/null &
}

alert_degraded() {
    local msg="$1"
    log "DEGRADED: $msg"
    afplay /System/Library/Sounds/Tink.aiff 2>/dev/null &
}

alert_healthy() {
    log "HEALTHY: Watchdog running normally"
}

restart_watchdog() {
    local now
    now=$(date +%s)
    local elapsed=$((now - LAST_RESTART_TIME))

    # Check restart cooldown
    if [ $elapsed -lt $RESTART_COOLDOWN ] && [ $RESTART_ATTEMPTS -gt 0 ]; then
        log "Skipping restart — cooldown active ($elapsed/${RESTART_COOLDOWN}s)"
        return 1
    fi

    # Check max attempts
    if [ $RESTART_ATTEMPTS -ge $MAX_RESTART_ATTEMPTS ]; then
        log "Max restart attempts ($MAX_RESTART_ATTEMPTS) reached — manual intervention needed"
        alert_critical "Max restart attempts exceeded. Please check manually."
        return 1
    fi

    RESTART_ATTEMPTS=$((RESTART_ATTEMPTS + 1))
    LAST_RESTART_TIME=$now

    log "Restarting watchdog (attempt $RESTART_ATTEMPTS/$MAX_RESTART_ATTEMPTS)..."

    # Kill existing session
    tmux kill-session -t watchdog 2>/dev/null || true
    sleep 2

    # Kill any orphaned processes
    pkill -f "watchdog_runner" 2>/dev/null || true
    sleep 1

    # Start fresh
    local logfile="logs/watchdog/watchdog_$(date +%Y%m%d_%H%M%S).log"
    tmux new-session -d -s watchdog \
        "PYTHONUNBUFFERED=1 caffeinate -i $PYTHON -m apps.watchdog scan --platform polymarket --duration 87600 2>&1 | tee $logfile"

    log "Waiting 90s for watchdog startup..."
    sleep 90

    # Verify it started
    if pgrep -f "watchdog_runner" > /dev/null 2>&1; then
        log "Watchdog restarted successfully"

        # Start alert monitor in split pane
        tmux split-window -t watchdog -v "bash alert_monitor.sh" 2>/dev/null || true

        # Reset restart counter on success
        RESTART_ATTEMPTS=0
        return 0
    else
        log "Watchdog failed to start!"
        return 1
    fi
}

run_health_check() {
    log "--- Health check #$CHECK_NUMBER ---"

    # Run the Python health check
    local report
    report=$($PYTHON watchdog_healthcheck.py --json 2>/dev/null) || report="{}"

    local status
    status=$(echo "$report" | $PYTHON -c "import json,sys; print(json.load(sys.stdin).get('status','critical'))" 2>/dev/null) || status="critical"

    local running
    running=$(echo "$report" | $PYTHON -c "import json,sys; print(json.load(sys.stdin).get('process',{}).get('running',False))" 2>/dev/null) || running="False"

    local markets
    markets=$(echo "$report" | $PYTHON -c "import json,sys; print(json.load(sys.stdin).get('stats',{}).get('markets_watched',0))" 2>/dev/null) || markets="0"

    local scans
    scans=$(echo "$report" | $PYTHON -c "import json,sys; print(json.load(sys.stdin).get('stats',{}).get('total_scans',0))" 2>/dev/null) || scans="0"

    local ws_msgs
    ws_msgs=$(echo "$report" | $PYTHON -c "import json,sys; print(json.load(sys.stdin).get('stats',{}).get('ws_messages',0))" 2>/dev/null) || ws_msgs="0"

    local total_alerts
    total_alerts=$(echo "$report" | $PYTHON -c "import json,sys; print(json.load(sys.stdin).get('alerts',{}).get('total',0))" 2>/dev/null) || total_alerts="0"

    local today_alerts
    today_alerts=$(echo "$report" | $PYTHON -c "import json,sys; print(json.load(sys.stdin).get('alerts',{}).get('today',0))" 2>/dev/null) || today_alerts="0"

    local runtime
    runtime=$(echo "$report" | $PYTHON -c "import json,sys; print(json.load(sys.stdin).get('stats',{}).get('runtime_human','unknown'))" 2>/dev/null) || runtime="unknown"

    local issues
    issues=$(echo "$report" | $PYTHON -c "import json,sys; r=json.load(sys.stdin); print('; '.join(r.get('issues',[]) + r.get('warnings',[]))[:200])" 2>/dev/null) || issues=""

    # Print status line
    log "Status: $status | Running: $running | Markets: $markets | Scans: $scans | WS: $ws_msgs | Alerts: $today_alerts today / $total_alerts total | Runtime: $runtime"

    if [ -n "$issues" ]; then
        log "Issues: $issues"
    fi

    # Take action based on status
    local action="none"

    case "$status" in
        "critical")
            if [ "$running" = "False" ]; then
                alert_critical "Watchdog not running"
                if restart_watchdog; then
                    action="restarted"
                    status="recovered"
                else
                    action="restart_failed"
                fi
            else
                alert_critical "Process running but critical issues detected"
                action="investigated"
            fi
            ;;
        "degraded")
            alert_degraded "$issues"
            action="monitored"
            # Reset restart counter on degraded (not dead)
            RESTART_ATTEMPTS=0
            ;;
        "healthy")
            alert_healthy
            action="none"
            # Reset restart counter on healthy
            RESTART_ATTEMPTS=0
            ;;
    esac

    # Log check-in
    local details="markets=$markets scans=$scans ws=$ws_msgs alerts_today=$today_alerts runtime=$runtime"
    if [ -n "$issues" ]; then
        details="$details issues=[$issues]"
    fi
    log_checkin "$status" "$action" "$details"

    CHECK_NUMBER=$((CHECK_NUMBER + 1))
}

# =============================================================================
# Main loop
# =============================================================================

log "=========================================="
log "Watchdog Periodic Monitor started"
log "Interval: ${INTERVAL}s ($(echo "$INTERVAL / 3600" | bc -l | xargs printf '%.1f')h)"
log "Project: $PROJECT_DIR"
log "Python: $PYTHON"
log "Check-in log: $CHECKIN_LOG"
log "=========================================="

CHECK_NUMBER=1

# Run first check immediately
run_health_check

# Loop
while true; do
    local_interval=$INTERVAL
    log "Next check in ${local_interval}s ($(date -v+${local_interval}S '+%H:%M:%S' 2>/dev/null || date -d "+${local_interval} seconds" '+%H:%M:%S' 2>/dev/null || echo 'N/A'))"
    sleep "$local_interval"
    run_health_check
done
