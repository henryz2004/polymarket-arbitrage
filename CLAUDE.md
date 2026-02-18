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
- `tests/` - Unit tests for arb engine, risk manager, portfolio, neg-risk

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

### Long-Term Testing

See `NEGRISK_TESTING.md` for detailed testing guide. Logs output to `logs/negrisk/`.

## Code Style

- Async-first: All I/O uses asyncio
- Type hints required (checked by mypy)
- Format with black
- Custom log levels: TRADE (25), OPPORTUNITY (26) defined in utils/logging_utils.py
