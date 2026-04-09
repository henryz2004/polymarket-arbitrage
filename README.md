# Prediction Markets Monorepo

This repository now exposes two first-class apps that share market-data infrastructure:

- `negrisk`: arbitrage scanning, trading, and dashboards
- `watchdog`: suspicious-activity detection, alerting, and backtesting

## Run

```bash
# Negrisk bot / scanner
python -m apps.negrisk scan

# Negrisk dashboard
python -m apps.negrisk dashboard

# Long-running negrisk scan
python -m apps.negrisk long-test --duration 4 --edge 1.5

# Polymarket watchdog
python -m apps.watchdog scan --platform polymarket

# Kalshi watchdog
python -m apps.watchdog scan --platform kalshi

# Watchdog backtests
python -m apps.watchdog backtest
```

## Repo Layout

```text
apps/
  negrisk/     CLI surface for arbitrage and dashboard workflows
  watchdog/    CLI surface for suspicious-activity scanning/backtests
core/
  negrisk/     Neg-risk arbitrage domain logic
  watchdog/    Shared watchdog logic + platform adapters
  shared/      Shared market event / BBA primitives
config/
  negrisk.yaml
  watchdog.polymarket.yaml
  watchdog.kalshi.yaml
docs/
  negrisk.md
  watchdog.md
  negrisk_testing.md
tests/
  negrisk/
  watchdog/
  shared/
```

## Docs

- [Negrisk guide](/Users/henryz2004/code/negrisk/polymarket-arbitrage/docs/negrisk.md)
- [Watchdog guide](/Users/henryz2004/code/negrisk/polymarket-arbitrage/docs/watchdog.md)
- [Negrisk long-run testing](/Users/henryz2004/code/negrisk/polymarket-arbitrage/docs/negrisk_testing.md)

## Compatibility

The historical root scripts still work for one migration phase, but they print deprecation notices:

- `main.py`
- `run_with_dashboard.py`
- `negrisk_long_test.py`
- `watchdog_runner.py`
- `kalshi_watchdog_runner.py`
- `backtest_runner.py`

The recommended surface is the `python -m apps...` commands above.
