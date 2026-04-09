# CLAUDE.md

This file provides repository-specific guidance for Claude Code when working in this repository.

## Project Overview

This repo is organized as a monorepo with two first-class apps:

- `negrisk`: arbitrage scanning, trading, and dashboards
- `watchdog`: suspicious-activity detection, alerting, and backtesting

Both apps share market-data primitives under `core/shared/markets/`.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run negrisk scan / trading bot
python -m apps.negrisk scan

# Run negrisk dashboard
python -m apps.negrisk dashboard

# Run negrisk scan with verbose logging
python -m apps.negrisk scan -v

# Run tests
pytest tests/ -v

# Run shared arbitrage tests
pytest tests/shared/test_arb_engine.py -v

# Run neg-risk tests
pytest tests/negrisk/test_negrisk.py -v

# Run neg-risk long-term test (4 hours default)
python -m apps.negrisk long-test --duration 4

# Run neg-risk overnight test (12 hours, custom edge)
python -m apps.negrisk long-test --duration 12 --edge 1.5

# Run Polymarket watchdog
python -m apps.watchdog scan --platform polymarket

# Run Kalshi watchdog
python -m apps.watchdog scan --platform kalshi

# Run watchdog with custom args
python -m apps.watchdog scan --platform polymarket --duration 24 --min-volume 10000

# Run watchdog in tmux (recommended for long runs)
tmux new-session -d -s watchdog "caffeinate -i ./venv/bin/python3 -m apps.watchdog scan --platform polymarket --duration 24"

# Attach to watchdog tmux session
tmux attach -t watchdog

# Run watchdog tests
pytest tests/watchdog/test_watchdog.py -v

# Run Kalshi watchdog tests
pytest tests/watchdog/test_kalshi_watchdog.py -v

# Format code
black .

# Type check
mypy .
```

## Architecture

```text
apps/
├── negrisk/                     # Negrisk CLI + runtime entrypoints
└── watchdog/                    # Watchdog CLI + runtime entrypoints

core/
├── shared/markets/             # Shared event/outcome/BBA models + protocols
├── negrisk/                    # Neg-risk arbitrage logic
├── watchdog/                   # Watchdog logic + platform adapters
├── arb_engine.py               # Single-platform bundle arbitrage
├── cross_platform_arb.py       # Cross-platform arbitrage
├── data_feed.py                # Real-time market data manager
├── execution.py                # Order placement with signal dedup + cooldowns
├── risk_manager.py             # Position/loss limits, kill switch
└── portfolio.py                # Position and PnL tracking
```

### Data Flow (Negrisk)

1. `NegriskRegistry` discovers events from Gamma or platform-specific registries.
2. `BBATracker` streams real-time BBA, seeds with CLOB, and refreshes stale coverage.
3. `NegriskDetector` checks `BUY_ALL`, `SELL_ALL`, and optional binary/partial variants.
4. `NegriskEngine` orchestrates scanning, cooldowns, and optional execution.

### Data Flow (Watchdog)

1. A watchdog platform adapter discovers markets and streams live price updates.
2. `PriceTracker` stores rolling history.
3. `AnomalyDetector` scores suspicious moves.
4. `NewsChecker` classifies alerts as `NEWS-DRIVEN` vs `UNEXPLAINED`.
5. `AlertDispatcher` sends alerts to console, JSONL, and Discord when `ALERT_WEBHOOK_URL` is set.

## Key Directories

- `apps/negrisk/` - Negrisk app entrypoints: `scan.py`, `dashboard.py`, `long_test.py`
- `apps/watchdog/` - Watchdog app entrypoints: `polymarket_runner.py`, `kalshi_runner.py`, `backtest.py`
- `core/shared/markets/` - Shared event/outcome/BBA models and protocols
- `core/negrisk/` - Neg-risk arbitrage logic
- `core/watchdog/` - Watchdog logic and platform adapters
- `polymarket_client/` - Polymarket REST + WebSocket client
- `kalshi_client/` - Kalshi REST + WebSocket/auth client
- `dashboard/` - FastAPI dashboard server + integration
- `tests/negrisk/`, `tests/watchdog/`, `tests/shared/` - Product-specific and shared tests

## Configuration

Primary configs:

- `config/negrisk.yaml`
- `config/watchdog.polymarket.yaml`
- `config/watchdog.kalshi.yaml`

Shared secrets are typically provided through `.env` or deployment env vars.

### Watchdog Runtime Env

- `ALERT_WEBHOOK_URL` - Discord webhook for watchdog alerts
- `KALSHI_API_KEY` - Kalshi API key ID for authenticated watchdog runs
- `KALSHI_PRIVATE_KEY` or `KALSHI_PRIVATE_KEY_PATH` - Kalshi private key material

### Negrisk Config Highlights

Defined in `NegriskConfig` in `core/negrisk/models.py`.

- `min_net_edge`
- `min_outcomes` / `max_legs`
- `staleness_ttl_ms`
- `min_liquidity_per_outcome`
- `min_event_volume_24h`
- `ws_only_mode`
- `binary_bundle_enabled`
- `enable_partial_positions`

### Watchdog Config Highlights

Defined in `WatchdogConfig` in `core/watchdog/models.py`.

- `watch_keywords`
- `watch_slugs`
- `min_event_volume_24h`
- `relative_thresholds`
- `absolute_thresholds`
- `price_poll_interval_seconds`
- `alert_cooldown_seconds`
- `news_check_enabled`
- `min_price_floor`
- `resolution_price_ceiling`

## Notes

- `main` now treats the `apps/...` entrypoints as the supported interface.
- The legacy root-script layout was preserved separately on the `compat/legacy-entrypoints` branch.
- See `docs/negrisk_testing.md` for long-run negrisk testing guidance.
