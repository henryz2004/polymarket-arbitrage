"""
Polymarket Neg-Risk Executor
================================

Order placement for neg-risk arbitrage on Polymarket (Polygon chain).

Uses py-clob-client SDK for:
- EIP-712 signed order creation
- FOK (Fill-or-Kill) market orders
- negRisk=True order options for multi-outcome markets

Contract addresses (Polygon, chain_id=137):
- CTF Exchange: 0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E
- Neg Risk CTF Exchange: 0xC5d563A36AE78145C45a50134d48A1215220f80a
- Neg Risk Adapter: 0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296
- USDC.e: 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
"""

from core.negrisk.platforms.polymarket.executor import PolymarketExecutor

__all__ = [
    "PolymarketExecutor",
]
