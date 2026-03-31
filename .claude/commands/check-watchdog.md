You are performing a periodic health check on the Polymarket watchdog insider-trading monitor. Do the following:

1. Run `./venv/bin/python3 watchdog_healthcheck.py --json` and parse the JSON output
2. Run `tail -30` on the latest watchdog log file in `logs/watchdog/watchdog_*.log` (sorted by modification time)
3. Check if any new alert files exist today: `ls -la logs/watchdog/alerts_$(date -u +%Y%m%d).jsonl`
4. Check the tmux session: `tmux capture-pane -t watchdog -p | tail -20`

Based on the results, provide a concise status report covering:
- Is the watchdog process running?
- Is the WebSocket connected and receiving data?
- Are scans happening at the expected rate (~1/min)?
- Any alerts fired? If so, summarize them (event name, score, news-driven or unexplained)
- Any errors or warnings (DNS failures, WS disconnects, stale data)?
- Overall health verdict: HEALTHY, DEGRADED, or CRITICAL

If the watchdog is NOT running or is in a CRITICAL state:
- Restart it: `tmux kill-session -t watchdog 2>/dev/null; tmux new-session -d -s watchdog "PYTHONUNBUFFERED=1 caffeinate -i ./venv/bin/python3 watchdog_runner.py --duration 87600 2>&1 | tee logs/watchdog/watchdog_$(date +%Y%m%d_%H%M%S).log"`
- Wait 90 seconds for startup
- Start the alert monitor in a split pane: `tmux split-window -t watchdog -v "bash alert_monitor.sh"`
- Verify it's healthy again

If DEGRADED (e.g. stale log but process running), investigate and report what's wrong.

Log this check-in to `logs/watchdog/checkins.jsonl` with a line like:
```
{"timestamp": "<UTC ISO>", "status": "healthy|degraded|critical", "action": "none|restarted|investigated", "details": "<brief summary>"}
```
