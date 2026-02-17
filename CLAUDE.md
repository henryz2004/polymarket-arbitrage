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
│   ├── NegriskRegistry               #   Event discovery from Gamma API
│   ├── BBATracker                    #   Real-time BBA via WebSocket + CLOB
│   └── NegriskDetector               #   Opportunity detection (sum of asks < $1)
├── ExecutionEngine                   # Order placement (single + atomic bundles)
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
1. NegriskRegistry discovers neg-risk events from Gamma API (multi-outcome winner-take-all markets)
2. BBATracker streams real-time best bid/ask via WebSocket, seeds with CLOB on startup
3. NegriskDetector checks if sum of all outcome asks < $1.00 - fees - gas
4. NegriskEngine creates atomic bundle signal (BUY YES on every outcome)
5. ExecutionEngine places all legs atomically with per-leg market routing and fresh slippage checks

## Key Directories

- `core/` - Trading logic: arb_engine.py, cross_platform_arb.py, execution.py, risk_manager.py, portfolio.py, data_feed.py
- `core/negrisk/` - Neg-risk arbitrage: models.py, registry.py, bba_tracker.py, detector.py, engine.py
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

- `min_net_edge`: Minimum net edge after fees and gas (default 2.5%)
- `min_outcomes` / `max_legs`: Outcome count bounds (3-15)
- `staleness_ttl_ms`: Max BBA data age before rejection (default 60s)
- `taker_fee_bps`: Polymarket taker fee in basis points (default 150 = 1.5%)
- `gas_per_leg`: Gas cost per leg in dollars (default $0.05; note: Polymarket covers gas, so this is conservative)
- `min_liquidity_per_outcome`: Minimum ask-side liquidity per outcome (default $50)
- `min_event_volume_24h`: Minimum 24h event volume (default $5,000)
- `max_position_per_event`: Maximum dollar exposure per event (default $500)

### Long-Term Testing

See `NEGRISK_TESTING.md` for detailed testing guide. Logs output to `logs/negrisk/`.

## Code Style

- Async-first: All I/O uses asyncio
- Type hints required (checked by mypy)
- Format with black
- Custom log levels: TRADE (25), OPPORTUNITY (26) defined in utils/logging_utils.py
