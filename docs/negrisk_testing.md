# Negrisk Long-Term Testing Guide

This guide explains how to run long-term tests for neg-risk arbitrage detection.

## Overnight Quickstart

Run two parallel tests overnight to compare edge thresholds:

```bash
# Create logs directory
mkdir -p logs/negrisk

# Conservative test (1.5% edge, 12 hours)
nohup python negrisk_long_test.py --duration 12 --edge 1.5 > /dev/null 2>&1 &

# Aggressive test (0.8% edge, 12 hours)
nohup python negrisk_long_test.py --duration 12 --edge 0.8 > /dev/null 2>&1 &

# Verify both are running
jobs -l
```

Check results in the morning:

```bash
# How many opportunities did each find?
for f in logs/negrisk/opportunities_*.jsonl; do echo "$f: $(wc -l < "$f") opportunities"; done

# Quick summary of best edges found
cat logs/negrisk/opportunities_*.jsonl | jq -r '[.net_edge, .event_title] | @tsv' | sort -rn | head -10

# Final stats from each test
for f in logs/negrisk/stats_*.jsonl; do echo "=== $f ===" && tail -1 "$f" | jq '{detected: .detector.opportunities_detected, stale: .detector.stale_data_rejections, liquidity: .detector.liquidity_rejections}'; done
```

## Quick Start

### Run a 4-hour test (default):
```bash
python negrisk_long_test.py
```

### Run an 8-hour overnight test:
```bash
python negrisk_long_test.py --duration 8
```

### Run with lower edge threshold (0.8%):
```bash
python negrisk_long_test.py --duration 4 --edge 0.8
```

### Run in background:
```bash
nohup python negrisk_long_test.py --duration 12 > /dev/null 2>&1 &
```

### Watch logs in real-time:
```bash
tail -f logs/negrisk/negrisk_test_*.log
```

## What Gets Logged

### 1. Main Log File (`logs/negrisk/negrisk_test_YYYYMMDD_HHMMSS.log`)
Detailed timestamped log of everything:
- Registry initialization (events discovered, tokens tracked)
- BBA tracker startup (WebSocket connection, seeding)
- Every opportunity detected (full details)
- Periodic stats snapshots (every 5 minutes)
- All rejections (stale data, low liquidity)
- Errors and exceptions
- Final summary

### 2. Opportunities File (`logs/negrisk/opportunities_YYYYMMDD_HHMMSS.jsonl`)
JSON Lines file with one opportunity per line:
```json
{
  "timestamp": "2026-02-15T01:23:45.123456",
  "opportunity_id": "negrisk_abc123",
  "event_title": "2024 Presidential Election Winner",
  "sum_of_asks": 0.9750,
  "gross_edge": 0.0250,
  "net_edge": 0.0185,
  "num_legs": 5,
  "suggested_size": 100.0,
  "total_cost": 97.50,
  "expected_profit": 1.85,
  "legs": [...]
}
```

### 3. Stats File (`logs/negrisk/stats_YYYYMMDD_HHMMSS.jsonl`)
JSON Lines file with stats snapshots every 5 minutes:
```json
{
  "timestamp": "2026-02-15T01:25:00.000000",
  "runtime_seconds": 300,
  "total_scans": 150,
  "registry": {...},
  "tracker": {...},
  "detector": {...},
  "opportunities_by_edge": {
    "0.0-2.0%": 3,
    "2.0-3.0%": 1,
    "3.0-5.0%": 0,
    "5.0%+": 0
  }
}
```

> **Note:** Edge buckets are dynamic based on the `--edge` flag. With `--edge 1.5` you'll see `"1.5-2.0%"`, with `--edge 0.0` you'll see `"0.0-2.0%"`, etc.

## Configuration

The test uses these parameters (from `negrisk_long_test.py`):

```python
NegriskConfig(
    min_net_edge=0.015,              # 1.5% (or --edge argument)
    min_outcomes=3,                  # At least 3 outcomes
    max_legs=15,                     # Max 15 outcomes
    staleness_ttl_ms=5000.0,         # 5 second freshness
    fee_rate_bps=0,                  # Most neg-risk markets are fee-free
    gas_per_leg=0.0,                 # Polymarket covers gas
    min_liquidity_per_outcome=50.0,  # $50 minimum
    min_event_volume_24h=5000.0,     # $5k volume minimum
    ws_only_mode=True,               # WebSocket-only mode (no CLOB verification)
    use_depth_scanning=True,         # Full book depth pricing
)
```

### Production Safety Features

The test harness (and production engine) include several safety mechanisms:

- **Stale data validation**: Rejects opportunities where BBA data exceeds `staleness_ttl_ms` age
- **Signal deduplication**: Prevents duplicate signal submission within 60 seconds (in `execution.py`)
- **Execution cooldown**: 5-second per-event cooldown prevents double-execution
- **WS connectivity tracking**: Monitors WebSocket health; sequence gaps trigger CLOB refresh
- **Phantom liquidity rejection**: Filters opportunities backed only by Gamma API prices without real CLOB depth
- **Post-scan delay**: 500ms delay after scan completion prevents redundant re-scans

## Analysis Commands

### Opportunity Counts

```bash
# Total opportunities found
wc -l logs/negrisk/opportunities_*.jsonl

# Count per file (if running parallel tests)
for f in logs/negrisk/opportunities_*.jsonl; do echo "$f: $(wc -l < "$f")"; done

# Opportunities per hour
cat logs/negrisk/opportunities_*.jsonl | jq -r '.timestamp[:13]' | sort | uniq -c
```

### Edge Analysis

```bash
# All edges, sorted
cat logs/negrisk/opportunities_*.jsonl | jq -r '.net_edge' | sort -n

# Best opportunities (>3% edge)
cat logs/negrisk/opportunities_*.jsonl | jq 'select(.net_edge > 0.03)'

# Top 10 by edge
cat logs/negrisk/opportunities_*.jsonl | jq -r '[.net_edge, .num_legs, .event_title] | @tsv' | sort -rn | head -10

# Average edge
cat logs/negrisk/opportunities_*.jsonl | jq -r '.net_edge' | awk '{s+=$1; n++} END {if(n>0) printf "avg=%.4f n=%d\n", s/n, n}'

# Edge distribution buckets
cat logs/negrisk/opportunities_*.jsonl | jq -r '.net_edge' | awk '{
  if ($1 < 0.02) b["1-2%"]++
  else if ($1 < 0.03) b["2-3%"]++
  else if ($1 < 0.05) b["3-5%"]++
  else b["5%+"]++
} END { for (k in b) print k, b[k] }' | sort
```

### Profit & Sizing

```bash
# Total theoretical profit
cat logs/negrisk/opportunities_*.jsonl | jq -r '.expected_profit' | awk '{s+=$1} END {printf "$%.2f\n", s}'

# Profit per opportunity
cat logs/negrisk/opportunities_*.jsonl | jq -r '[.expected_profit, .total_cost, .event_title] | @tsv' | sort -rn | head -10

# Size distribution
cat logs/negrisk/opportunities_*.jsonl | jq -r '.suggested_size' | sort -n | uniq -c
```

### Event Patterns

```bash
# Which events produce the most opportunities?
cat logs/negrisk/opportunities_*.jsonl | jq -r '.event_title' | sort | uniq -c | sort -rn | head -10

# Opportunity count by number of legs
cat logs/negrisk/opportunities_*.jsonl | jq -r '.num_legs' | sort -n | uniq -c

# Events with repeated opportunities (persistent mispricings)
cat logs/negrisk/opportunities_*.jsonl | jq -r '.event_title' | sort | uniq -c | awk '$1 > 2' | sort -rn
```

### Health & Rejections

```bash
# Final stats from last snapshot
tail -1 logs/negrisk/stats_*.jsonl | jq '.'

# Rejections over time
cat logs/negrisk/stats_*.jsonl | jq '{time: .timestamp, stale: .detector.stale_data_rejections, liquidity: .detector.liquidity_rejections}'

# WebSocket health over time
cat logs/negrisk/stats_*.jsonl | jq '{time: .timestamp, ws_msgs: .tracker.ws_messages, clob: .tracker.clob_fetches, gaps: .tracker.sequence_gaps}'

# Check for errors in log
grep -c "ERROR" logs/negrisk/negrisk_test_*.log
grep "ERROR" logs/negrisk/negrisk_test_*.log | tail -5
```

### Compare Parallel Test Runs

```bash
# Side-by-side summary of all test runs
for f in logs/negrisk/stats_*.jsonl; do
  echo "=== $(basename $f) ==="
  tail -1 "$f" | jq '{
    runtime_min: (.runtime_seconds / 60 | floor),
    detected: .detector.opportunities_detected,
    stale_rejections: .detector.stale_data_rejections,
    liquidity_rejections: .detector.liquidity_rejections,
    best_edge: .detector.best_edge_seen,
    ws_messages: .tracker.ws_messages
  }'
done

# Compare opportunity counts across edge thresholds
echo "Conservative (1.5%):" && wc -l logs/negrisk/opportunities_*_1.5_*.jsonl 2>/dev/null
echo "Aggressive (0.8%):" && wc -l logs/negrisk/opportunities_*_0.8_*.jsonl 2>/dev/null
```

## Expected Behavior

### Normal Operation:
- **Stale data rejections**: Should be low (<100 per 5 min snapshot) after initial seeding
- **WebSocket messages**: Should continuously increase
- **Opportunities**: Highly dependent on market conditions
  - Efficient markets: may see 0-5 per hour
  - Volatile periods: may see 10-20 per hour

### Warning Signs:
- **High stale rejections** (>1000 per snapshot): WebSocket may be disconnected
- **No WebSocket messages**: Connection issue
- **Zero opportunities for hours**: Edge threshold may be too high, or gas_per_leg too conservative
- **Constant errors in log**: Check for API issues

## Recommended Test Runs

### 1. Baseline Test (4 hours, 1.5% edge):
```bash
python negrisk_long_test.py --duration 4 --edge 1.5
```
Good for: Verifying system stability, checking if opportunities exist

### 2. Aggressive Test (2 hours, 0.8% edge):
```bash
python negrisk_long_test.py --duration 2 --edge 0.8
```
Good for: Seeing more opportunities, testing detection logic

### 3. Overnight Test (12 hours, 1.5% edge):
```bash
python negrisk_long_test.py --duration 12 --edge 1.5
```
Good for: Stability testing, catching market events across time zones

### 4. Production Simulation (8 hours, 2.5% edge):
```bash
python negrisk_long_test.py --duration 8 --edge 2.5
```
Good for: Simulating production parameters from roadmap

### 5. Parallel Overnight (12 hours, two thresholds):
```bash
nohup python negrisk_long_test.py --duration 12 --edge 1.5 > /dev/null 2>&1 &
nohup python negrisk_long_test.py --duration 12 --edge 0.8 > /dev/null 2>&1 &
```
Good for: Understanding edge sensitivity, comparing opportunity frequency across thresholds

### 6. Wide-open scan (24 hours, 0% edge):
```bash
nohup python negrisk_long_test.py --duration 24 --edge 0.0 > /dev/null 2>&1 &
```
Good for: Seeing ALL opportunities regardless of edge, understanding the full opportunity landscape

## Tips

1. **Run multiple tests with different parameters** to understand sensitivity
2. **Start with shorter tests** (1-2 hours) to validate setup
3. **Monitor the first 10-15 minutes** - if stale rejections are high, something's wrong
4. **Check logs directory** size - each 12-hour test = ~10-30MB
5. **Use `tail -f`** to watch logs in real-time:
   ```bash
   tail -f logs/negrisk/negrisk_test_*.log
   ```
6. **Kill background tests** if needed:
   ```bash
   jobs -l                    # List running jobs
   kill %1 %2                 # Kill by job number
   pkill -f negrisk_long_test # Kill all negrisk tests
   ```
