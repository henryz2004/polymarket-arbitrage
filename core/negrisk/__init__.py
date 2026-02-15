"""
Negrisk Arbitrage Module
=========================

Detects and executes arbitrage opportunities in Polymarket neg-risk markets.

Neg-risk markets are multi-outcome "winner-take-all" events where:
- Multiple mutually exclusive outcomes exist (e.g., "Who wins the election?")
- Each outcome has a YES token
- If sum of all YES ask prices < $1.00, buying all guarantees profit
- The NegRisk adapter provides capital efficiency for these markets
"""

from core.negrisk.models import (
    NegriskConfig,
    NegriskEvent,
    Outcome,
    OutcomeBBA,
    NegriskOpportunity,
    NegriskStats,
)
from core.negrisk.registry import NegriskRegistry
from core.negrisk.bba_tracker import BBATracker
from core.negrisk.detector import NegriskDetector
from core.negrisk.engine import NegriskEngine

__all__ = [
    "NegriskConfig",
    "NegriskEvent",
    "Outcome",
    "OutcomeBBA",
    "NegriskOpportunity",
    "NegriskStats",
    "NegriskRegistry",
    "BBATracker",
    "NegriskDetector",
    "NegriskEngine",
]
