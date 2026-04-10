# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Monorepo with two first-class apps sharing market-data primitives under `core/shared/markets/`:

- **negrisk**: neg-risk arbitrage scanning, trading, and dashboards for prediction markets
- **watchdog**: suspicious-activity detection, alerting, and backtesting across Polymarket and Kalshi

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

# Run all tests
pytest tests/ -v

# Run a single test file
pytest tests/shared/test_arb_engine.py -v

# Run neg-risk tests
pytest tests/negrisk/test_negrisk.py -v

# Run watchdog tests
pytest tests/watchdog/test_watchdog.py -v

# Run Kalshi watchdog tests
pytest tests/watchdog/test_kalshi_watchdog.py -v

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

# Format code
black .

# Type check
mypy .
```

## Architecture

```text
apps/
├── negrisk/                     # CLI entrypoints: scan.py, dashboard.py, long_test.py
└── watchdog/                    # CLI entrypoints: polymarket_runner.py, kalshi_runner.py, backtest.py

core/
├── shared/markets/             # Shared event/outcome/BBA models + protocols
├── negrisk/                    # Neg-risk arbitrage logic
│   ├── registry.py             # Event discovery from Gamma API + priority scoring
│   ├── bba_tracker.py          # Real-time BBA, CLOB seeding, WS connectivity
│   ├── detector.py             # BUY_ALL/SELL_ALL detection, depth scanning, phantom rejection
│   ├── binary_detector.py      # Binary YES+NO bundle detection
│   ├── partial_detector.py     # +EV partial subset detection (disabled by default)
│   ├── engine.py               # Orchestrator: ws_only_mode, priority sorting, cooldowns
│   └── models.py               # NegriskConfig, OutcomeBBA, depth levels
├── watchdog/                   # Watchdog logic + platform adapters
│   ├── engine.py               # Registry discovery, scan loop, news enrichment
│   ├── price_tracker.py        # Rolling price history, CLOB backfill
│   ├── anomaly_detector.py     # Spike detection and suspicion scoring
│   ├── news_checker.py         # Google News RSS headline fetching + classification
│   ├── alert_dispatcher.py     # Console/JSONL/Discord output
│   └── models.py               # WatchdogConfig, AnomalyAlert, PriceSnapshot
├── arb_engine.py               # Single-platform bundle arbitrage
├── cross_platform_arb.py       # Cross-platform arbitrage
├── data_feed.py                # Real-time market data manager
├── execution.py                # Order placement with signal dedup + cooldowns
├── risk_manager.py             # Position/loss limits, kill switch
└── portfolio.py                # Position and PnL tracking

polymarket_client/              # Polymarket REST + WebSocket client
kalshi_client/                  # Kalshi REST + WebSocket/auth client
dashboard/                      # FastAPI dashboard server + integration
utils/                          # Config loading, logging setup, backtesting
```

### Data Flow (Negrisk)

1. `NegriskRegistry` discovers neg-risk events from the Gamma API and scores priority using near-resolution and volume-spike heuristics.
2. `BBATracker` streams real-time BBA via WebSocket, seeds with CLOB on startup, and re-seeds gamma-only tokens periodically.
3. `NegriskDetector` checks both directions:
   - `BUY_ALL`: sum of asks < $1.00 - fees - gas
   - `SELL_ALL`: sum of bids > $1.00 + fees + gas
   - Supports taker and maker modes with depth-adjusted pricing
   - Rejects phantom gamma-only liquidity without real CLOB depth
4. `BinaryBundleDetector` checks 2-outcome events for YES+NO mispricing.
5. `NegriskEngine` orchestrates scanning with event priority sorting and `ws_only_mode`.
6. Production safety includes stale-data validation, signal deduplication, execution cooldowns, and WebSocket connectivity tracking.

### Data Flow (Watchdog)

1. Platform adapter discovers markets and streams live price updates.
2. `PriceTracker` stores rolling history.
3. `AnomalyDetector` scores suspicious moves.
4. `NewsChecker` classifies alerts as `NEWS-DRIVEN` vs `UNEXPLAINED`.
5. `AlertDispatcher` sends alerts to console, JSONL, and Discord when `ALERT_WEBHOOK_URL` is set.

## Configuration

Primary configs: `config/negrisk.yaml`, `config/watchdog.polymarket.yaml`, `config/watchdog.kalshi.yaml`

Secrets via `.env` or deployment env vars:
- `ALERT_WEBHOOK_URL` — Discord webhook for watchdog alerts
- `KALSHI_API_KEY` — Kalshi API key ID
- `KALSHI_PRIVATE_KEY` or `KALSHI_PRIVATE_KEY_PATH` — Kalshi private key material

### Negrisk Config (`NegriskConfig` in `core/negrisk/models.py`)

- `min_net_edge`: minimum net edge after fees/gas (default 1.5%)
- `min_outcomes` / `max_legs`: outcome count bounds (3–15)
- `staleness_ttl_ms`: max BBA age before rejection (default 5000 ms)
- `fee_rate_bps`, `gas_per_leg`: cost parameters
- `min_liquidity_per_outcome`: min ask-side liquidity per outcome (default $50)
- `min_event_volume_24h`: min 24h event volume (default $5,000)
- `ws_only_mode`: skip CLOB fetches and trust WebSocket data (default `false`)
- `use_depth_scanning`: walk full order book depth (default `true`)
- `order_strategy`: `taker` or `maker`
- `binary_bundle_enabled`: YES+NO bundle arbitrage on binary events (default `false`)
- `enable_partial_positions`: +EV partial subset detection (default `false`)

### Watchdog Config (`WatchdogConfig` in `core/watchdog/models.py`)

- `watch_keywords`: geopolitical keywords for event filtering
- `watch_slugs`: force-watch specific event slugs
- `min_event_volume_24h`: min 24h volume to watch (default $10,000)
- `relative_thresholds` / `absolute_thresholds`: `(change, window_seconds)` pairs
- `price_poll_interval_seconds`: scan interval (default 60)
- `alert_cooldown_seconds`: per-token dedup window (default 300)
- `news_check_enabled`: Google News headline enrichment (default `true`)
- `min_price_floor`: ignore outcomes below this price (default 3¢)
- `resolution_price_ceiling`: suppress near-resolution alerts (default 95¢)

## Code Style

- Async-first for I/O operations.
- Type hints throughout; the repo uses `mypy` for checking.
- Format with `black`.
- Custom log levels `TRADE` (25) and `OPPORTUNITY` (26) defined in `utils/logging_utils.py`.
- After changing neg-risk logic, re-run the relevant neg-risk script before finishing.

## Polymarket API Reference

- Docs index: <https://docs.polymarket.com/llms.txt>
- Neg-risk trading: <https://docs.polymarket.com/advanced/neg-risk.md>
- Fees: <https://docs.polymarket.com/trading/fees.md>
- Python SDK: <https://github.com/Polymarket/py-clob-client>

### Key Contracts (Polygon, Chain ID 137)

- CTF Exchange: `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`
- Neg Risk CTF Exchange: `0xC5d563A36AE78145C45a50134d48A1215220f80a`
- Neg Risk Adapter: `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296`
- Conditional Tokens (CTF): `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`
- USDC.e: `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`

### Neg-Risk Order Notes

- Pass `negRisk=True` in order options for multi-outcome markets.
- USDC.e and Conditional Tokens approval needed for CTF Exchange, Neg Risk CTF Exchange, and Neg Risk Adapter.
- Tick size must be fetched per market; `0.01` is common but not universal.

### Fee Structure

- Taker fee: `shares * feeRate * p * (1-p)^exponent`
- Geopolitics: 0% fees
- Politics/Finance/Tech: `feeRate=0.04`, peak 1.00%
- Sports: `feeRate=0.03`, peak 0.75%
- Crypto: `feeRate=0.072`, peak 1.80%
- Makers pay 0%

## Notes

- `main` treats the `apps/...` entrypoints as the supported interface.
- Legacy root-script layout preserved on the `compat/legacy-entrypoints` branch.
- See `docs/negrisk_testing.md` for long-run testing guidance.
- Negrisk logs: `logs/negrisk/`; Watchdog alerts: `logs/watchdog/alerts_YYYYMMDD.jsonl`.
