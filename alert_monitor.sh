#!/bin/bash
# Alert monitor — watches for new watchdog alerts and triggers Mac notifications
# Usage: ./alert_monitor.sh
#
# Polls the alerts directory every 30s for new JSONL lines.
# On new alert: plays system sound + speaks alert via TTS.

ALERTS_DIR="/Users/henryz2004/code/negrisk/polymarket-arbitrage/logs/watchdog"
SEEN_COUNT=0

echo "$(date '+%H:%M:%S') - Alert monitor started. Watching $ALERTS_DIR"

# Count existing alerts so we don't re-trigger on startup
for f in "$ALERTS_DIR"/alerts_*.jsonl; do
    if [ -f "$f" ]; then
        SEEN_COUNT=$((SEEN_COUNT + $(wc -l < "$f")))
    fi
done

echo "$(date '+%H:%M:%S') - Existing alerts: $SEEN_COUNT (will not re-trigger)"

while true; do
    sleep 30

    # Count current total alert lines
    CURRENT_COUNT=0
    for f in "$ALERTS_DIR"/alerts_*.jsonl; do
        if [ -f "$f" ]; then
            CURRENT_COUNT=$((CURRENT_COUNT + $(wc -l < "$f")))
        fi
    done

    if [ "$CURRENT_COUNT" -gt "$SEEN_COUNT" ]; then
        NEW_ALERTS=$((CURRENT_COUNT - SEEN_COUNT))
        echo "$(date '+%H:%M:%S') - NEW ALERT DETECTED ($NEW_ALERTS new)"

        # Get the latest alert details
        LATEST_FILE=$(ls -t "$ALERTS_DIR"/alerts_*.jsonl 2>/dev/null | head -1)
        if [ -n "$LATEST_FILE" ]; then
            LATEST=$(tail -1 "$LATEST_FILE")
            TITLE=$(echo "$LATEST" | python3 -c "import json,sys; print(json.load(sys.stdin).get('event_title','unknown'))" 2>/dev/null)
            SCORE=$(echo "$LATEST" | python3 -c "import json,sys; print(json.load(sys.stdin).get('suspicion_score',0))" 2>/dev/null)
            echo "$(date '+%H:%M:%S') -   Event: $TITLE"
            echo "$(date '+%H:%M:%S') -   Score: $SCORE"

            # Mac audio alert
            afplay /System/Library/Sounds/Glass.aiff &
            say "polymarket alert. suspicion score $SCORE. $TITLE" -v Trinoids &
        fi

        SEEN_COUNT=$CURRENT_COUNT
    fi
done
