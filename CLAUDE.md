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
├── ArbEngine                         # Single-platform opportunity detection
├── CrossPlatformArbEngine            # Cross-platform arbitrage (Polymarket vs Kalshi)
├── ExecutionEngine                   # Order placement and management
├── RiskManager                       # Position/loss limits, kill switch
├── Portfolio                         # Position and PnL tracking
└── DashboardIntegration              # WebSocket bridge to dashboard UI
```

**Data Flow:**
1. DataFeed updates markets, order books, positions in real-time
2. ArbEngine analyzes market state, generates Signals for bundle arbitrage
3. CrossPlatformArbEngine matches markets across platforms using text similarity
4. ExecutionEngine places orders (respects risk limits)
5. Portfolio tracks positions and PnL
6. Dashboard displays real-time state via WebSocket

## Key Directories

- `core/` - Trading logic: arb_engine.py, cross_platform_arb.py, execution.py, risk_manager.py, portfolio.py, data_feed.py
- `polymarket_client/` - Polymarket REST + WebSocket client, data models
- `kalshi_client/` - Kalshi REST client, data models
- `dashboard/` - FastAPI server (server.py) with embedded HTML, bot integration
- `utils/` - Config loading, logging setup, backtesting
- `tests/` - Unit tests for arb engine, risk manager, portfolio

## Configuration

Edit `config.yaml`:
- `mode.trading_mode`: "dry_run" or "live"
- `mode.data_mode`: "real" (live markets) or "simulation" (fake data with opportunities)
- `mode.cross_platform_enabled`: Enable Polymarket + Kalshi arbitrage
- `trading.min_edge`: Minimum profit threshold (default 1%)
- `risk.max_position_per_market`, `max_global_exposure`, `max_daily_loss`: Risk limits

## Code Style

- Async-first: All I/O uses asyncio
- Type hints required (checked by mypy)
- Format with black
- Custom log levels: TRADE (25), OPPORTUNITY (26) defined in utils/logging_utils.py
