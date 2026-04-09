# Negrisk

`negrisk` is the arbitrage/trading app in this monorepo.

## Entrypoints

```bash
python -m apps.negrisk scan
python -m apps.negrisk dashboard
python -m apps.negrisk long-test --duration 4 --edge 1.5
```

## Config

Default config: [config/negrisk.yaml](/Users/henryz2004/code/negrisk/polymarket-arbitrage/config/negrisk.yaml)

## Code Ownership

- `core/negrisk/`: neg-risk registry, BBA tracking, detection, execution
- `core/shared/markets/`: shared event/outcome/BBA primitives
- `dashboard/`: dashboard server/integration

## Notes

- Use the `dashboard` subcommand for the live UI.
- Use the `long-test` subcommand for extended scan and execution validation.
- Product-specific tests live under [tests/negrisk](/Users/henryz2004/code/negrisk/polymarket-arbitrage/tests/negrisk).
- `main` now treats `python -m apps.negrisk ...` as the supported interface.
