# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Python 3.10+ async arbitrage trading bot for Polymarket and Kalshi prediction markets. Features a real-time web dashboard, risk management, and both simulation and live trading modes.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run bot with dashboard (main entry point)
python run_with_dashboard.py

# Run bot only (no dashboard)
python main.py

# Run with verbose logging
python main.py -v

# Run tests
pytest tests/ -v

# Run single test file
pytest tests/test_arb_engine.py -v

# Run neg-risk tests
pytest tests/test_negrisk.py -v

# Run neg-risk long-term test (4 hours default)
python negrisk_long_test.py

# Run neg-risk overnight test (12 hours, custom edge)
python negrisk_long_test.py --duration 12 --edge 1.5

# Run watchdog (suspicious activity scanner, 24h default)
python watchdog_runner.py

# Run watchdog with custom args
python watchdog_runner.py --duration 24 --min-volume 10000

# Run watchdog in tmux (recommended for long runs)
tmux new-session -d -s watchdog "caffeinate -i ./venv/bin/python3 watchdog_runner.py --duration 24"

# Attach to watchdog tmux session
tmux attach -t watchdog

# Run watchdog tests
pytest tests/test_watchdog.py -v

# Format code
black .

# Type check
mypy .
```

## Architecture

```
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

**Data Flow (Bundle/Cross-Platform):**
1. DataFeed updates markets, order books, positions in real-time
2. ArbEngine analyzes market state, generates Signals for bundle arbitrage
3. CrossPlatformArbEngine matches markets across platforms using text similarity
4. ExecutionEngine places orders (respects risk limits)
5. Portfolio tracks positions and PnL
6. Dashboard displays real-time state via WebSocket

**Data Flow (Neg-Risk):**
1. NegriskRegistry discovers neg-risk events from Gamma API, scores priority (near-resolution, volume spikes)
2. BBATracker streams real-time BBA via WebSocket, seeds with CLOB on startup, re-seeds gamma-only tokens periodically
3. NegriskDetector checks both directions:
   - **BUY_ALL**: sum of asks < $1.00 - fees - gas (buy YES on every outcome)
   - **SELL_ALL**: sum of bids > $1.00 + fees + gas (sell YES on every outcome)
   - Supports taker mode (cross spread) and maker mode (place at mid-price, 0% fee)
   - Depth-adjusted pricing walks full order book, not just top-of-book
   - Phantom liquidity rejection filters gamma-only prices without real CLOB depth
4. BinaryBundleDetector checks 2-outcome events for YES+NO mispricing (separate from multi-outcome)
5. NegriskEngine orchestrates scanning with event priority sorting and ws_only_mode option
6. Production safety: stale data validation, signal dedup (60s), execution cooldown (5s/event), WS connectivity tracking

**ws_only_mode:** Skips CLOB fetches before execution, trusting WebSocket data for lower latency. Includes additional validation (staleness checks, re-validation before execution). Enable with `NegriskConfig(ws_only_mode=True)`.

## Key Directories

- `core/` - Trading logic: arb_engine.py, cross_platform_arb.py, execution.py (with signal dedup), risk_manager.py, portfolio.py, data_feed.py
- `core/negrisk/` - Neg-risk arbitrage:
  - `models.py` - Data models, NegriskConfig, OutcomeBBA (with source tracking + depth levels)
  - `registry.py` - Event discovery from Gamma API + priority scoring (near-resolution, volume spikes)
  - `bba_tracker.py` - Real-time BBA via WebSocket + CLOB seeding/re-seeding + WS connectivity tracking
  - `detector.py` - Multi-outcome detection: BUY_ALL, SELL_ALL, taker/maker modes, depth scanning, phantom rejection
  - `binary_detector.py` - Binary YES+NO bundle detection (BUY_BINARY, SELL_BINARY)
  - `partial_detector.py` - +EV partial subset detection (Kelly criterion sizing, disabled by default)
  - `engine.py` - Orchestrator: ws_only_mode, priority sorting, execution cooldowns, post-scan delay
- `polymarket_client/` - Polymarket REST + WebSocket client, data models
- `kalshi_client/` - Kalshi REST client, data models
- `dashboard/` - FastAPI server (server.py) with embedded HTML, bot integration
- `utils/` - Config loading, logging setup, backtesting
- `core/watchdog/` - Suspicious activity detection:
  - `models.py` - WatchdogConfig, AnomalyAlert (with `news_driven` flag), PriceSnapshot
  - `engine.py` - Orchestrator: registry discovery, BBA tracking, scan loop, news enrichment
  - `price_tracker.py` - Rolling price history per token, CLOB backfill, rate-limited sampling
  - `anomaly_detector.py` - Spike detection (relative + absolute thresholds), suspicion scoring (0-10)
  - `news_checker.py` - Google News RSS headline fetching, keyword extraction, date filtering
  - `alert_dispatcher.py` - Console (colored) + JSONL file output, NEWS-DRIVEN vs UNEXPLAINED labels
- `tests/` - Unit tests for arb engine, risk manager, portfolio, neg-risk, watchdog

## Configuration

Edit `config.yaml`:
- `mode.trading_mode`: "dry_run" or "live"
- `mode.data_mode`: "real" (live markets) or "simulation" (fake data with opportunities)
- `mode.cross_platform_enabled`: Enable Polymarket + Kalshi arbitrage
- `trading.min_edge`: Minimum profit threshold (default 1%)
- `risk.max_position_per_market`, `max_global_exposure`, `max_daily_loss`: Risk limits

### Neg-Risk Config (in `NegriskConfig` dataclass, `core/negrisk/models.py`)

Core detection:
- `min_net_edge`: Minimum net edge after fees and gas (default 1.5%)
- `min_outcomes` / `max_legs`: Outcome count bounds (3-15)
- `staleness_ttl_ms`: Max BBA data age before rejection (default 5000ms = 5s)
- `fee_rate_bps`: Per-leg fee rate from CLOB API (default 0, most neg-risk markets are fee-free)
- `gas_per_leg`: Gas cost per leg in dollars (default $0.00; Polymarket covers gas)
- `min_liquidity_per_outcome`: Minimum ask-side liquidity per outcome (default $50)
- `min_event_volume_24h`: Minimum 24h event volume (default $5,000)
- `max_position_per_event`: Maximum dollar exposure per event (default $500)

WebSocket & data:
- `ws_only_mode`: Skip CLOB fetches, trust WebSocket data (default false)
- `ws_sequence_gap_threshold`: Max sequence gaps before CLOB refresh (default 5)
- `reseed_interval_seconds`: Re-seed gamma-only tokens interval (default 300s)
- `use_depth_scanning`: Walk full order book depth (default true)
- `max_book_levels`: Depth levels to store per outcome (default 10)
- `detection_latency_tracking`: Track detection timing stats (default true)

Order strategy:
- `order_strategy`: "taker" (cross spread) or "maker" (place at mid, 0% fee)
- `maker_price_offset_bps`: Offset from mid-price (default 0)
- `maker_timeout_seconds`: Cancel unfilled maker orders (default 30s)
- `maker_min_net_edge`: Lower threshold for maker orders (default 1.5%)

Partial-CLOB tolerance:
- `max_gamma_only_legs`: Max outcomes with gamma-only prices (default 0 = strict)
- `gamma_max_spread`: Max gamma spread tolerance (default 5 cents)
- `gamma_max_probability`: Max implied prob for gamma-only legs (default 20%)

Event prioritization:
- `prioritize_near_resolution`: Boost near-resolution events (default true)
- `resolution_window_hours`: Priority window (default 24h)
- `priority_edge_discount`: Min edge multiplier for high-priority events (default 0.5)
- `volume_spike_threshold`: Volume spike multiplier (default 2.0x)

Optional detectors:
- `binary_bundle_enabled`: YES+NO bundle arb on 2-outcome events (default false)
- `enable_partial_positions`: +EV partial subset detection (default false, NOT riskless)
- `min_partial_ev` / `max_excluded_probability` / `partial_kelly_fraction`: Partial position params

### Watchdog Config (in `WatchdogConfig` dataclass, `core/watchdog/models.py`)

- `watch_keywords`: Geopolitical keywords to filter events (strike, war, attack, etc.)
- `watch_slugs`: Force-watch specific event slugs
- `min_event_volume_24h`: Minimum 24h volume to watch (default $10,000)
- `relative_thresholds`: (pct_change, window_seconds) pairs — e.g. 50% in 1h, 100% in 4h
- `absolute_thresholds`: (cent_move, window_seconds) pairs — e.g. 5c in 30min, 10c in 1h
- `off_hours_utc`: Off-hours window for suspicion scoring (default 7-11 UTC = 2-6 AM EST)
- `price_poll_interval_seconds`: Scan interval (default 60s)
- `alert_cooldown_seconds`: Dedup window per token (default 300s)
- `news_check_enabled`: Fetch Google News headlines for alert enrichment (default true)
- `news_lookback_hours`: Only match headlines from the last N hours (default 6)
- `warmup_seconds`: Don't fire alerts until N seconds of live data (default 300s)
- `min_price_floor`: Ignore outcomes below this price (default 3c)

Alert fields include `news_driven: bool` — `True` when correlated headlines found, `False` when unexplained (the real insider-trading signal). Alerts log to `logs/watchdog/alerts_YYYYMMDD.jsonl`.

### Long-Term Testing

See `NEGRISK_TESTING.md` for detailed testing guide. Logs output to `logs/negrisk/`.

## Polymarket API Reference (for CLOB order execution)

### Documentation URLs
- Full docs index: https://docs.polymarket.com/llms.txt
- Order creation: https://docs.polymarket.com/trading/orders/create.md
- Neg-risk trading: https://docs.polymarket.com/advanced/neg-risk.md
- Fees: https://docs.polymarket.com/trading/fees.md
- Authentication: https://docs.polymarket.com/api-reference/authentication.md
- CTF operations: https://docs.polymarket.com/trading/ctf/overview.md
- Contract addresses: https://docs.polymarket.com/resources/contract-addresses.md
- WebSocket market: https://docs.polymarket.com/api-reference/wss/market.md
- WebSocket user: https://docs.polymarket.com/api-reference/wss/user.md
- Python SDK: https://github.com/Polymarket/py-clob-client

### Contract Addresses (Polygon, Chain ID 137)
- CTF Exchange: `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`
- Neg Risk CTF Exchange: `0xC5d563A36AE78145C45a50134d48A1215220f80a`
- Neg Risk Adapter: `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296`
- Conditional Tokens (CTF): `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`
- USDC.e: `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`

### py-clob-client Usage (pip install py-clob-client)
```python
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

# Read-only client
client = ClobClient("https://clob.polymarket.com")

# Trading client (EOA wallet, signature_type=0)
client = ClobClient(
    "https://clob.polymarket.com",
    key="<private-key>",
    chain_id=137,
    signature_type=0,
    funder="<wallet-address>"
)
client.set_api_creds(client.create_or_derive_api_creds())

# Limit order (neg-risk market)
order = client.create_order(OrderArgs(
    token_id="<token-id>", price=0.50, size=10.0, side=BUY
), options={"tickSize": "0.01", "negRisk": True})
resp = client.post_order(order, OrderType.GTC)

# Market order (FOK)
mo = client.create_market_order(MarketOrderArgs(
    token_id="<token-id>", amount=25.0, side=BUY, price=0.55  # worst price
), options={"tickSize": "0.01", "negRisk": True})
resp = client.post_order(mo, OrderType.FOK)

# Batch orders (up to 15)
orders = [{"order": signed_order, "orderType": OrderType.GTC}, ...]
resp = client.post_orders(orders)
```

### Fee Structure (Taker only, Makers pay 0%)
- `fee = shares * feeRate * p * (1-p)^exponent` (varies by category)
- **Geopolitics: 0% fees** (most neg-risk events are geopolitical)
- Politics/Finance/Tech: feeRate=0.04, peak 1.00%
- Sports: feeRate=0.03, peak 0.75%
- Crypto: feeRate=0.072, peak 1.80%
- Fees peak at p=0.50, decrease toward extremes

### Authentication Flow
1. L1: Private key signs EIP-712 message → creates API creds (apiKey, secret, passphrase)
2. L2: HMAC-SHA256 headers (POLY_ADDRESS, POLY_SIGNATURE, POLY_TIMESTAMP, POLY_API_KEY, POLY_PASSPHRASE)
3. Orders require both L2 headers AND local EIP-712 signing via SDK

### Neg-Risk Order Specifics
- Must pass `negRisk=True` in order options for multi-outcome markets
- USDC.e approval needed for: CTF Exchange, Neg Risk CTF Exchange, Neg Risk Adapter
- Conditional Tokens approval needed for same three contracts
- Tick size must be fetched per market (0.01 typical for most markets)

## Code Style

- Async-first: All I/O uses asyncio
- Type hints required (checked by mypy)
- Format with black
- Custom log levels: TRADE (25), OPPORTUNITY (26) defined in utils/logging_utils.py
