# Negrisk Long-Term Testing Guide

This guide explains how to run long-term tests for neg-risk arbitrage detection.

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
    "1.5-2.0%": 2,
    "2.0-3.0%": 1,
    "3.0-5.0%": 0,
    "5.0%+": 0
  }
}
```

## Configuration

The test uses these parameters (from `negrisk_long_test.py`):

```python
NegriskConfig(
    min_net_edge=0.015,              # 1.5% (or --edge argument)
    min_outcomes=3,                  # At least 3 outcomes
    max_legs=15,                     # Max 15 outcomes
    staleness_ttl_ms=60000.0,        # 60 second freshness
    taker_fee_bps=150,               # 1.5% Polymarket fee
    gas_per_leg=0.05,                # $0.05 gas per leg
    min_liquidity_per_outcome=50.0,  # $50 minimum
    min_event_volume_24h=5000.0,     # $5k volume minimum
)
```

## Analysis Examples

### Count total opportunities:
```bash
wc -l logs/negrisk/opportunities_*.jsonl
```

### Find best opportunities (>3% edge):
```bash
cat logs/negrisk/opportunities_*.jsonl | jq 'select(.net_edge > 0.03)'
```

### Extract all opportunity edges:
```bash
cat logs/negrisk/opportunities_*.jsonl | jq -r '.net_edge' | sort -n
```

### Get final stats from last snapshot:
```bash
tail -1 logs/negrisk/stats_*.jsonl | jq '.'
```

### Count rejections over time:
```bash
cat logs/negrisk/stats_*.jsonl | jq '{time: .timestamp, stale: .detector.stale_data_rejections, liquidity: .detector.liquidity_rejections}'
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
- **Zero opportunities for hours**: Edge threshold may be too high
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

## Analyzing Results

After the test completes, send the log files for analysis:

1. **Main log**: Shows timeline of events, errors, opportunities
2. **Opportunities JSONL**: Machine-readable opportunity data
3. **Stats JSONL**: Time-series performance metrics

Look for:
- Opportunity frequency and quality
- Rejection patterns (is staleness still an issue?)
- WebSocket health (gaps, disconnects)
- Edge distribution (are opportunities clustered by edge size?)
- Event patterns (are certain event types more profitable?)

## Tips

1. **Run multiple tests with different parameters** to understand sensitivity
2. **Start with shorter tests** (1-2 hours) to validate setup
3. **Monitor the first 10-15 minutes** - if stale rejections are high, something's wrong
4. **Check logs directory** fills up - each 12-hour test = ~50-100MB
5. **Use `tail -f`** to watch logs in real-time:
   ```bash
   tail -f logs/negrisk/negrisk_test_*.log
   ```
