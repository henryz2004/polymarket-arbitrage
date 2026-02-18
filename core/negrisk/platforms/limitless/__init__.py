"""
Limitless Exchange Integration
================================

Scan-only MVP for neg-risk arbitrage detection on Limitless Exchange (Base chain).

Limitless uses the same neg-risk / category market structure as Polymarket:
- Multi-outcome group markets (e.g. sports match winners)
- CLOB orderbooks per outcome
- One winner resolves to $1.00

API: https://api.limitless.exchange
WebSocket: wss://ws.limitless.exchange (Socket.IO)
"""

from core.negrisk.platforms.limitless.api_client import LimitlessAPIClient
from core.negrisk.platforms.limitless.registry import LimitlessRegistry
from core.negrisk.platforms.limitless.bba_tracker import LimitlessBBATracker
from core.negrisk.platforms.limitless.executor import LimitlessExecutor

__all__ = [
    "LimitlessAPIClient",
    "LimitlessRegistry",
    "LimitlessBBATracker",
    "LimitlessExecutor",
]
