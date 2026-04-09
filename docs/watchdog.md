# Watchdog

`watchdog` is the suspicious-activity detection app in this monorepo.

## Entrypoints

```bash
python -m apps.watchdog scan --platform polymarket
python -m apps.watchdog scan --platform kalshi
python -m apps.watchdog backtest
```

## Config

- Polymarket defaults: [config/watchdog.polymarket.yaml](/Users/henryz2004/code/negrisk/polymarket-arbitrage/config/watchdog.polymarket.yaml)
- Kalshi defaults: [config/watchdog.kalshi.yaml](/Users/henryz2004/code/negrisk/polymarket-arbitrage/config/watchdog.kalshi.yaml)

## Runtime Env

- `ALERT_WEBHOOK_URL` for Discord alert delivery
- `KALSHI_API_KEY` plus `KALSHI_PRIVATE_KEY` or `KALSHI_PRIVATE_KEY_PATH` for authenticated Kalshi runs

## Code Ownership

- `core/watchdog/`: anomaly detection, price history, alerts, news correlation
- `core/watchdog/platforms/polymarket/`: Polymarket discovery + BBA adapter
- `core/watchdog/platforms/kalshi/`: Kalshi adapter surface
- `core/shared/markets/`: shared event/outcome/BBA primitives

## Notes

- Product-specific tests live under [tests/watchdog](/Users/henryz2004/code/negrisk/polymarket-arbitrage/tests/watchdog).
- The Kalshi watchdog is now treated as a watchdog platform, not a separate top-level product.
- `main` now treats `python -m apps.watchdog ...` as the supported interface.
