# AGENTS.md

This file provides repository-specific guidance for coding agents working in this project.

## Project Overview

Python 3.10+ async arbitrage trading bot for Polymarket and Kalshi prediction markets. Features a real-time web dashboard, risk management, and both simulation and live trading modes.

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

# Run single test file
pytest tests/shared/test_arb_engine.py -v

# Run neg-risk tests
pytest tests/negrisk/test_negrisk.py -v

# Run neg-risk long-term test (4 hours default)
python -m apps.negrisk long-test --duration 4

# Run neg-risk overnight test (12 hours, custom edge)
python -m apps.negrisk long-test --duration 12 --edge 1.5

# Run watchdog (suspicious activity scanner, 24h default)
python -m apps.watchdog scan --platform polymarket

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
TradingBot
├── PolymarketClient / KalshiClient  # API clients for each exchange
├── DataFeed                          # Real-time market data manager
├── ArbEngine                         # Single-platform bundle arbitrage (YES+NO)
├── CrossPlatformArbEngine            # Cross-platform arbitrage (Polymarket vs Kalshi)
├── NegriskEngine                     # Neg-risk multi-outcome arbitrage
│   ├── NegriskRegistry               #   Event discovery + priority scoring from Gamma API
│   ├── BBATracker                    #   Real-time BBA via WebSocket + CLOB seeding/re-seeding
│   ├── NegriskDetector               #   Multi-outcome detection (BUY_ALL + SELL_ALL)
│   ├── BinaryBundleDetector          #   Binary YES+NO bundle detection (BUY_BINARY + SELL_BINARY)
│   └── PartialPositionDetector       #   +EV partial subset detection (not riskless, disabled by default)
├── WatchdogEngine                    # Suspicious activity detection scanner
│   ├── PriceTracker                  #   Rolling price history per token
│   ├── AnomalyDetector               #   Spike detection + suspicion scoring
│   ├── NewsChecker                   #   Google News RSS headline correlation
│   └── AlertDispatcher               #   Console + JSONL alert output
├── ExecutionEngine                   # Order placement with signal dedup + cooldowns
├── RiskManager                       # Position/loss limits, kill switch
├── Portfolio                         # Position and PnL tracking
└── DashboardIntegration              # WebSocket bridge to dashboard UI
```

### Data Flow (Bundle/Cross-Platform)

1. `DataFeed` updates markets, order books, and positions in real time.
2. `ArbEngine` analyzes market state and generates signals for bundle arbitrage.
3. `CrossPlatformArbEngine` matches markets across platforms using text similarity.
4. `ExecutionEngine` places orders while respecting risk limits.
5. `Portfolio` tracks positions and PnL.
6. The dashboard displays real-time state via WebSocket.

### Data Flow (Neg-Risk)

1. `NegriskRegistry` discovers neg-risk events from the Gamma API and scores priority using near-resolution and volume-spike heuristics.
2. `BBATracker` streams real-time BBA via WebSocket, seeds with CLOB on startup, and re-seeds gamma-only tokens periodically.
3. `NegriskDetector` checks both directions:
   - `BUY_ALL`: sum of asks < $1.00 - fees - gas
   - `SELL_ALL`: sum of bids > $1.00 + fees + gas
   - Supports taker mode and maker mode
   - Uses depth-adjusted pricing across the full order book
   - Rejects phantom gamma-only liquidity without real CLOB depth
4. `BinaryBundleDetector` checks 2-outcome events for YES+NO mispricing.
5. `NegriskEngine` orchestrates scanning with event priority sorting and `ws_only_mode`.
6. Production safety includes stale-data validation, signal deduplication, execution cooldowns, and WebSocket connectivity tracking.

### `ws_only_mode`

Skips CLOB fetches before execution and trusts WebSocket data for lower latency. Additional safeguards include staleness checks and re-validation before execution. Enable with `NegriskConfig(ws_only_mode=True)`.

## Key Directories

- `core/` - Trading logic: `arb_engine.py`, `cross_platform_arb.py`, `execution.py`, `risk_manager.py`, `portfolio.py`, `data_feed.py`
- `core/negrisk/` - Neg-risk arbitrage
- `polymarket_client/` - Polymarket REST and WebSocket client plus data models
- `kalshi_client/` - Kalshi REST client plus data models
- `dashboard/` - FastAPI server (`server.py`) with embedded HTML and bot integration
- `utils/` - Config loading, logging setup, backtesting
- `core/watchdog/` - Suspicious activity detection
- `tests/` - Unit tests for arbitrage, risk, portfolio, neg-risk, and watchdog behavior

### `core/negrisk/`

- `models.py` - Data models, `NegriskConfig`, `OutcomeBBA`, source tracking, depth levels
- `registry.py` - Event discovery from Gamma API and priority scoring
- `bba_tracker.py` - Real-time BBA, CLOB seeding/re-seeding, WS connectivity tracking
- `detector.py` - Multi-outcome detection, BUY_ALL, SELL_ALL, taker/maker modes, depth scanning, phantom rejection
- `binary_detector.py` - Binary YES+NO bundle detection
- `partial_detector.py` - +EV partial subset detection with Kelly sizing, disabled by default
- `engine.py` - Orchestrator with `ws_only_mode`, priority sorting, cooldowns, and post-scan delay

### `core/watchdog/`

- `models.py` - `WatchdogConfig`, `AnomalyAlert`, `PriceSnapshot`
- `engine.py` - Registry discovery, BBA tracking, scan loop, news enrichment
- `price_tracker.py` - Rolling price history, CLOB backfill, rate-limited sampling
- `anomaly_detector.py` - Spike detection and suspicion scoring
- `news_checker.py` - Google News RSS headline fetching, keyword extraction, date filtering
- `alert_dispatcher.py` - Console and JSONL output, including NEWS-DRIVEN vs UNEXPLAINED labels

## Configuration

Primary negrisk config: `config/negrisk.yaml`

- `mode.trading_mode`: `dry_run` or `live`
- `mode.data_mode`: `real` or `simulation`
- `mode.cross_platform_enabled`: enable Polymarket + Kalshi arbitrage
- `trading.min_edge`: minimum profit threshold, default 1%
- `risk.max_position_per_market`, `risk.max_global_exposure`, `risk.max_daily_loss`: risk limits

### Neg-Risk Config

Defined in `NegriskConfig` in `core/negrisk/models.py`.

Core detection:
- `min_net_edge`: minimum net edge after fees and gas, default 1.5%
- `min_outcomes` / `max_legs`: outcome count bounds, 3-15
- `staleness_ttl_ms`: maximum BBA age before rejection, default 5000 ms
- `fee_rate_bps`: per-leg fee rate from the CLOB API, default 0
- `gas_per_leg`: gas cost per leg in dollars, default $0.00
- `min_liquidity_per_outcome`: minimum ask-side liquidity per outcome, default $50
- `min_event_volume_24h`: minimum 24h event volume, default $5,000
- `max_position_per_event`: maximum dollar exposure per event, default $500

WebSocket and data:
- `ws_only_mode`: skip CLOB fetches and trust WebSocket data, default `false`
- `ws_sequence_gap_threshold`: maximum sequence gaps before CLOB refresh, default 5
- `reseed_interval_seconds`: gamma-only token re-seed interval, default 300
- `use_depth_scanning`: walk full order book depth, default `true`
- `max_book_levels`: depth levels to store per outcome, default 10
- `detection_latency_tracking`: track detection timing stats, default `true`

Order strategy:
- `order_strategy`: `taker` or `maker`
- `maker_price_offset_bps`: offset from mid-price, default 0
- `maker_timeout_seconds`: cancel unfilled maker orders after default 30 seconds
- `maker_min_net_edge`: lower threshold for maker orders, default 1.5%

Partial-CLOB tolerance:
- `max_gamma_only_legs`: max outcomes with gamma-only prices, default 0
- `gamma_max_spread`: maximum gamma spread tolerance, default 5 cents
- `gamma_max_probability`: maximum implied probability for gamma-only legs, default 20%

Event prioritization:
- `prioritize_near_resolution`: boost near-resolution events, default `true`
- `resolution_window_hours`: priority window, default 24
- `priority_edge_discount`: minimum edge multiplier for high-priority events, default 0.5
- `volume_spike_threshold`: volume spike multiplier, default 2.0x

Optional detectors:
- `binary_bundle_enabled`: enable YES+NO bundle arbitrage on binary events, default `false`
- `enable_partial_positions`: enable +EV partial subset detection, default `false`
- `min_partial_ev`, `max_excluded_probability`, `partial_kelly_fraction`: partial-position parameters

### Watchdog Config

Defined in `WatchdogConfig` in `core/watchdog/models.py`.

- `watch_keywords`: geopolitical keywords used to filter events
- `watch_slugs`: force-watch specific event slugs
- `min_event_volume_24h`: minimum 24h volume to watch, default $10,000
- `relative_thresholds`: `(pct_change, window_seconds)` pairs
- `absolute_thresholds`: `(cent_move, window_seconds)` pairs
- `off_hours_utc`: off-hours window for suspicion scoring, default 7-11 UTC
- `price_poll_interval_seconds`: scan interval, default 60
- `alert_cooldown_seconds`: per-token dedup window, default 300
- `news_check_enabled`: enable Google News headline enrichment, default `true`
- `news_lookback_hours`: headline matching lookback window, default 6
- `warmup_seconds`: suppress alerts until enough live data exists, default 300
- `min_price_floor`: ignore outcomes below this price, default 3 cents

Alert fields include `news_driven: bool`. `True` means correlated headlines were found; `False` means the move was unexplained. Alerts are written to `logs/watchdog/alerts_YYYYMMDD.jsonl`.

### Long-Term Testing

See `docs/negrisk_testing.md` for the detailed test guide. Neg-risk logs are written to `logs/negrisk/`.

## Polymarket API Reference

### Documentation URLs

- Full docs index: <https://docs.polymarket.com/llms.txt>
- Order creation: <https://docs.polymarket.com/trading/orders/create.md>
- Neg-risk trading: <https://docs.polymarket.com/advanced/neg-risk.md>
- Fees: <https://docs.polymarket.com/trading/fees.md>
- Authentication: <https://docs.polymarket.com/api-reference/authentication.md>
- CTF operations: <https://docs.polymarket.com/trading/ctf/overview.md>
- Contract addresses: <https://docs.polymarket.com/resources/contract-addresses.md>
- WebSocket market: <https://docs.polymarket.com/api-reference/wss/market.md>
- WebSocket user: <https://docs.polymarket.com/api-reference/wss/user.md>
- Python SDK: <https://github.com/Polymarket/py-clob-client>

### Contract Addresses (Polygon, Chain ID 137)

- CTF Exchange: `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`
- Neg Risk CTF Exchange: `0xC5d563A36AE78145C45a50134d48A1215220f80a`
- Neg Risk Adapter: `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296`
- Conditional Tokens (CTF): `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`
- USDC.e: `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`

### `py-clob-client` Usage

```python
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

client = ClobClient("https://clob.polymarket.com")

client = ClobClient(
    "https://clob.polymarket.com",
    key="<private-key>",
    chain_id=137,
    signature_type=0,
    funder="<wallet-address>"
)
client.set_api_creds(client.create_or_derive_api_creds())

order = client.create_order(OrderArgs(
    token_id="<token-id>", price=0.50, size=10.0, side=BUY
), options={"tickSize": "0.01", "negRisk": True})
resp = client.post_order(order, OrderType.GTC)

mo = client.create_market_order(MarketOrderArgs(
    token_id="<token-id>", amount=25.0, side=BUY, price=0.55
), options={"tickSize": "0.01", "negRisk": True})
resp = client.post_order(mo, OrderType.FOK)

orders = [{"order": signed_order, "orderType": OrderType.GTC}, ...]
resp = client.post_orders(orders)
```

### Fee Structure

- Taker fee formula: `fee = shares * feeRate * p * (1-p)^exponent`
- Geopolitics: 0% fees
- Politics/Finance/Tech: `feeRate=0.04`, peak 1.00%
- Sports: `feeRate=0.03`, peak 0.75%
- Crypto: `feeRate=0.072`, peak 1.80%
- Makers pay 0%

### Authentication Flow

1. L1: private key signs an EIP-712 message to create API credentials.
2. L2: requests use HMAC-SHA256 headers such as `POLY_SIGNATURE`, `POLY_TIMESTAMP`, `POLY_API_KEY`, and `POLY_PASSPHRASE`.
3. Order placement requires both L2 headers and local EIP-712 signing via the SDK.

### Neg-Risk Order Specifics

- Pass `negRisk=True` in order options for multi-outcome markets.
- USDC.e approval is needed for the CTF Exchange, Neg Risk CTF Exchange, and Neg Risk Adapter.
- Conditional Tokens approval is needed for the same three contracts.
- Tick size must be fetched per market; `0.01` is common but should not be assumed universally.

## Code Style

- Prefer async-first implementations for I/O.
- Use type hints throughout; the repo uses `mypy`.
- Format Python with `black`.
- Custom log levels `TRADE` (25) and `OPPORTUNITY` (26) are defined in `utils/logging_utils.py`.
- After changing neg-risk logic, re-run the neg-risk script relevant to the change before finishing.
