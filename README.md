# Polymarket + Kalshi Arbitrage Bot

<div align="center">

![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)
![Status](https://img.shields.io/badge/Status-Active-brightgreen.svg)
![Platforms](https://img.shields.io/badge/Platforms-Polymarket%20%7C%20Kalshi-orange.svg)

**Cross-platform arbitrage detection between Polymarket and Kalshi prediction markets**

[Features](#-features) • [Demo](#-demo) • [Quick Start](#-quick-start) • [Dashboard](#-live-dashboard) • [Configuration](#%EF%B8%8F-configuration)

**Author: [ImMike](https://github.com/ImMike)**

</div>

---

## 🎬 Demo

### 🎥 Video Demo

[**▶️ Watch Demo Video (Click to Download)**](https://github.com/ImMike/polymarket-arbitrage/raw/main/Polymarket-Arb-clip.mp4)

*Watch the bot in action - scanning 5,000+ markets and finding opportunities in real-time*

### Screenshots

<div align="center">

#### 📊 Real Market Data Mode
*Scanning 5,000+ live Polymarket markets*

![Live Data Dashboard](polymarket-live-data.png)

#### 🧪 Simulation Mode  
*Testing with simulated opportunities - 99.6% win rate, $573 profit*

![Simulated Data Dashboard](simulated-market-data.png)

</div>

---

## 🎯 Features

- **🔀 Cross-Platform Arbitrage** - Detects price differences between Polymarket and Kalshi for the same prediction
- **🔍 Bundle Arbitrage Detection** - Identifies when YES + NO prices don't sum to ~$1.00
- **🎯 Neg-Risk Arbitrage** - Multi-outcome winner-take-all markets where sum of all outcome asks < $1.00
- **📊 Market Making** - Captures spreads by placing competitive bid/ask orders
- **🛡️ Risk Management** - Position limits, loss limits, kill switch
- **📈 Live Dashboard** - Real-time web UI showing opportunities and bot activity
- **🔄 Dual Data Modes** - Switch between real market data and simulation
- **💰 Fee Accounting** - Realistic edge calculations including fees & gas costs
- **📝 Comprehensive Logging** - Detailed logs for trades, opportunities, and errors
- **🤖 Market Matching AI** - Automatically matches similar predictions across platforms using text similarity
- **⚛️ Atomic Execution** - Bundle and neg-risk orders execute all-or-nothing with rollback

---

## 🔄 Data Modes

The bot supports two data modes, configurable in `config.yaml`:

### 🧪 Simulation Mode (for demos & testing)

```yaml
mode:
  data_mode: "simulation"  # Generates fake data with opportunities
```

- Generates simulated order books with realistic price dynamics
- Periodically introduces mispricings to create arbitrage opportunities
- Perfect for **screenshots, demos, and testing strategies**
- Fast updates to see the bot in action

### 🌐 Real Mode (for live trading)

```yaml
mode:
  data_mode: "real"  # Fetches actual Polymarket data
```

- Connects to **Polymarket's Gamma API** for market discovery
- Fetches **real order books** from the CLOB (Central Limit Order Book) API
- Scans **5,000+ markets** across all categories
- Real markets are highly efficient - arbitrage opportunities are rare!

---

## 📁 Project Structure

```
polymarket-arbitrage/
├── main.py                   # Main entry point
├── run_with_dashboard.py     # Bot + live dashboard
├── config.yaml               # Configuration (edit this!)
├── requirements.txt          # Python dependencies
│
├── polymarket_client/        # Polymarket API client
│   ├── api.py               # REST + WebSocket integration
│   └── models.py            # Data classes
│
├── kalshi_client/            # Kalshi API client (NEW!)
│   ├── api.py               # Kalshi REST API integration
│   └── models.py            # Kalshi data classes
│
├── core/                     # Trading logic
│   ├── data_feed.py         # Real-time market data manager
│   ├── arb_engine.py        # Single-platform opportunity detection
│   ├── cross_platform_arb.py # Cross-platform arbitrage
│   ├── execution.py         # Order management (single + atomic bundles)
│   ├── risk_manager.py      # Risk limits & kill switch
│   ├── portfolio.py         # Position & PnL tracking
│   └── negrisk/             # Neg-risk multi-outcome arbitrage
│       ├── models.py        # Data models (events, outcomes, opportunities)
│       ├── registry.py      # Event discovery from Gamma API
│       ├── bba_tracker.py   # Real-time BBA via WebSocket + CLOB
│       ├── detector.py      # Opportunity detection (sum of asks < $1)
│       └── engine.py        # Orchestrator
│
├── dashboard/                # Web dashboard
│   ├── server.py            # FastAPI server
│   └── integration.py       # Bot-dashboard bridge
│
├── utils/                    # Utilities
│   ├── config_loader.py     # YAML config parser
│   ├── logging_utils.py     # Colored console logging
│   └── backtest.py          # Backtesting engine
│
├── tests/                    # Unit tests
│   ├── test_arb_engine.py
│   ├── test_risk_manager.py
│   ├── test_portfolio.py
│   └── test_negrisk.py      # Neg-risk arbitrage tests
│
├── negrisk_long_test.py      # Long-term neg-risk testing script
├── NEGRISK_TESTING.md        # Neg-risk testing guide
│
└── logs/                     # Log files (auto-created)
    └── negrisk/              # Neg-risk test logs
```

---

## 🚀 Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/ImMike/polymarket-arbitrage.git
cd polymarket-arbitrage

# Create virtual environment (recommended)
python -m venv venv
source venv/bin/activate      # Linux/Mac
venv\Scripts\activate         # Windows

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure

Edit `config.yaml`:

```yaml
mode:
  trading_mode: "dry_run"     # Start with dry run!
  data_mode: "real"           # Use "simulation" for demos
  cross_platform_enabled: true  # Enable Polymarket + Kalshi arbitrage
  kalshi_enabled: true        # Enable Kalshi monitoring

trading:
  min_edge: 0.01              # 1% minimum edge
  default_order_size: 5       # Start small

risk:
  max_position_per_market: 15
  max_global_exposure: 50
  max_daily_loss: 10
```

### 3. Run with Dashboard

```bash
# Run bot with live dashboard
python run_with_dashboard.py

# Open http://localhost:8000 in your browser
```

### 4. Other Run Modes

```bash
# Bot only (no dashboard)
python main.py

# Verbose logging
python main.py -v

# Specify config file
python main.py --config config.live.yaml
```

---

## 📊 Live Dashboard

The dashboard provides real-time visibility into bot operations:

<div align="center">

| Metric | Description |
|--------|-------------|
| **Opportunities** | Bundle arb & market-making signals found |
| **Markets Monitored** | Total markets being scanned |
| **Order Books** | Markets with live price data |
| **Uptime** | Bot running time |
| **PnL** | Profit/Loss tracking |

</div>

Access at `http://localhost:8000` when running with `run_with_dashboard.py`

---

## 📈 Trading Strategies

### 🔀 Cross-Platform Arbitrage (NEW!)

Detects when the same prediction is priced differently on Polymarket vs Kalshi:

| Condition | Action | Profit |
|-----------|--------|--------|
| Polymarket YES cheaper than Kalshi YES | Buy on Polymarket, Sell on Kalshi | Price difference |
| Kalshi YES cheaper than Polymarket YES | Buy on Kalshi, Sell on Polymarket | Price difference |

**Example**: 
- "Will Trump win?" YES is **$0.52** on Polymarket
- Same prediction YES is **$0.58** on Kalshi
- **Profit opportunity**: Buy on Polymarket, sell on Kalshi = **6% edge** (minus fees)

The bot uses **text similarity matching** to automatically find equivalent predictions across platforms.

### Neg-Risk Arbitrage

Exploits mispricing in **multi-outcome winner-take-all markets** (e.g., "Who will win the 2024 election?"). Polymarket's neg-risk adapter allows capital-efficient trading of these events.

| Condition | Action | Profit |
|-----------|--------|--------|
| Sum of all outcome asks < $1.00 (after fees) | Buy YES on every outcome | Guaranteed $1 payout when one wins |

**Example**: An event with 5 outcomes priced at $0.28, $0.25, $0.20, $0.10, $0.08 = $0.91 total. Buy all 5 for $0.91, one will resolve to $1.00 = **9% gross edge** (minus 1.5% fees).

**How it works:**
1. **Registry** discovers neg-risk events from Gamma API (~300 events, ~6000 tokens)
2. **BBA Tracker** streams real-time prices via WebSocket, seeds initial data from CLOB
3. **Detector** continuously scans for sum-of-asks < $1.00 - fees
4. **Engine** executes atomically -- all legs must succeed or all are rolled back

**Key difference from bundle arb**: Bundle arb trades YES+NO on a single binary market. Neg-risk trades YES across *all outcomes* of a multi-outcome event.

### Bundle Arbitrage

Detects when YES + NO tokens are mispriced within a single platform:

| Condition | Action | Profit |
|-----------|--------|--------|
| `ask_yes + ask_no < $1.00` | Buy both | Guaranteed $1 payout |
| `bid_yes + bid_no > $1.00` | Sell both | Lock in premium |

**Example**: If YES trades at $0.45 and NO at $0.52, buying both costs $0.97 but pays out $1.00 = **3% profit**

### Market Making

Places orders inside wide spreads:

1. If spread ≥ 5¢, place bid slightly above best bid
2. Place ask slightly below best ask  
3. Profit when both sides fill

---

## ⚙️ Configuration

### Key Parameters

| Section | Parameter | Description | Default |
|---------|-----------|-------------|---------|
| `mode` | `trading_mode` | `"dry_run"` or `"live"` | `dry_run` |
| `mode` | `data_mode` | `"simulation"` or `"real"` | `real` |
| `mode` | `cross_platform_enabled` | Enable Polymarket + Kalshi | `true` |
| `mode` | `kalshi_enabled` | Enable Kalshi monitoring | `true` |
| `mode` | `min_match_similarity` | Market matching threshold | 0.6 |
| `trading` | `min_edge` | Min profit after fees | 0.01 (1%) |
| `trading` | `min_spread` | Min spread for MM | 0.05 (5¢) |
| `trading` | `mm_enabled` | Enable market making | true |
| `risk` | `max_position_per_market` | Max $ per market | 200 |
| `risk` | `max_global_exposure` | Max total exposure | 5000 |
| `risk` | `max_daily_loss` | Stop-loss limit | 500 |

### Neg-Risk Configuration

Neg-risk parameters are defined in `NegriskConfig` (see `core/negrisk/models.py`):

| Parameter | Description | Default |
|-----------|-------------|---------|
| `min_net_edge` | Min net edge after fees & gas | 0.025 (2.5%) |
| `min_outcomes` | Min outcomes for event | 3 |
| `max_legs` | Max outcomes to trade | 15 |
| `staleness_ttl_ms` | Max BBA data age (ms) | 60000 (60s) |
| `taker_fee_bps` | Polymarket taker fee | 150 (1.5%) |
| `gas_per_leg` | Gas cost per leg ($) | 0.05 |
| `min_liquidity_per_outcome` | Min ask liquidity per outcome ($) | 50 |
| `min_event_volume_24h` | Min 24h event volume ($) | 5000 |
| `max_position_per_event` | Max exposure per event ($) | 500 |

### Fee Configuration

```yaml
trading:
  maker_fee_bps: 0            # Polymarket maker fee (0%)
  taker_fee_bps: 150          # Polymarket taker fee (1.5%)
  estimated_gas_per_order: 0.001  # Polygon gas (minimal - Polymarket covers gas)
```

### Environment Variables

Store sensitive data in environment variables:

```bash
export POLYMARKET_API_KEY="your_api_key"
export POLYMARKET_PRIVATE_KEY="your_private_key"
```

---

## 🧪 Testing

```bash
# Run all tests
pytest tests/ -v

# Run specific test
pytest tests/test_arb_engine.py -v

# Run neg-risk tests
pytest tests/test_negrisk.py -v

# With coverage report
pytest tests/ --cov=core --cov=polymarket_client
```

### Neg-Risk Long-Term Testing

Run the neg-risk detector against live market data to measure opportunity frequency:

```bash
# 4-hour test (default)
python negrisk_long_test.py

# 12-hour overnight test
python negrisk_long_test.py --duration 12 --edge 1.5

# Run in background
nohup python negrisk_long_test.py --duration 12 > /dev/null 2>&1 &
```

Logs go to `logs/negrisk/` -- see `NEGRISK_TESTING.md` for full details on analyzing results.

---

## 📊 How It Works

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      CROSS-PLATFORM ARBITRAGE FLOW                          │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌──────────────┐         ┌───────────────┐         ┌──────────────┐       │
│  │  Polymarket  │────────▶│  Market       │◀────────│    Kalshi    │       │
│  │  5000+ mkts  │         │  Matcher      │         │  5000+ mkts  │       │
│  └──────────────┘         └───────┬───────┘         └──────────────┘       │
│         │                         │                        │                │
│         │                    Matched Pairs                 │                │
│         │                         │                        │                │
│         ▼                         ▼                        ▼                │
│  ┌──────────────┐         ┌───────────────┐         ┌──────────────┐       │
│  │  Data Feed   │────────▶│ Cross-Platform│◀────────│  Kalshi      │       │
│  │  (orderbooks)│         │  Arb Engine   │         │  Orderbooks  │       │
│  └──────────────┘         └───────┬───────┘         └──────────────┘       │
│         │                         │                        │                │
│         │                    Opportunities                 │                │
│         │                         │                        │                │
│         ▼                         ▼                        ▼                │
│  ┌──────────────┐         ┌───────────────┐         ┌──────────────┐       │
│  │  Dashboard   │◀────────│   Execution   │────────▶│  Portfolio   │       │
│  │  (live UI)   │         │   (orders)    │         │  (tracking)  │       │
│  └──────────────┘         └───────────────┘         └──────────────┘       │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## ⚠️ Important Notes

### About Real Markets

> **Real prediction markets are highly efficient.** Arbitrage opportunities are rare and fleeting. The bot is designed to catch them when they occur, but don't expect constant profits.

### Risk Warnings

1. **🧪 Start in dry run mode** - Always test before using real money
2. **💵 Start small** - Begin with minimal capital ($50-100)
3. **👀 Monitor actively** - Don't leave running unattended
4. **📉 Expect losses** - Trading always carries risk
5. **🔬 This is experimental** - Use at your own risk

### Polymarket Notes

- Polymarket uses a **hybrid model**: centralized order matching, on-chain settlement
- Polymarket covers gas fees for trading on Polygon (the neg-risk module includes a conservative `gas_per_leg` parameter as a safety margin)
- Taker fee is 1.5% (150 bps); maker fee is 0%
- Funds are held in USDC on Polygon
- API keys required for live trading

### Kalshi Notes

- Kalshi is a **CFTC-regulated** US prediction market exchange
- Prices are in cents (e.g., 55¢ for YES)
- No authentication required for public market data
- Must be US-based to trade (KYC required)
- API documentation: [docs.kalshi.com](https://docs.kalshi.com)

---

## 🔧 Development

### Adding New Strategies

1. Add detection logic in `core/arb_engine.py`
2. Create `Opportunity` objects with entry/exit prices
3. Execution engine handles order placement

### Extending the Dashboard

The dashboard uses FastAPI + vanilla JS. Add new endpoints in `dashboard/server.py` and update the HTML in `get_embedded_html()`.

---

## 📄 License

MIT License - See [LICENSE](LICENSE) for details

---

## 👤 Author

**[ImMike](https://github.com/ImMike)**

- GitHub: [@ImMike](https://github.com/ImMike)

---

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit changes (`git commit -m 'Add amazing feature'`)
4. Push to branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

<div align="center">

**⚠️ Disclaimer**: This software is for educational purposes. Trading prediction markets involves risk of loss. Past performance does not guarantee future results. Always do your own research.

Made with ☕ and Python by [ImMike](https://github.com/ImMike)

</div>
