"""
Negrisk Platform Protocols
============================
"""

from typing import Optional, Protocol, runtime_checkable

from core.shared.markets.protocols import BBATrackerProtocol, RegistryProtocol


@runtime_checkable
class FeeModelProtocol(Protocol):
    """Contract for platform-specific fee calculation."""

    @property
    def gas_per_leg(self) -> float: ...

    def compute_fee_per_share(self, prices: list[float], side: str, fee_rate_bps_override: Optional[float] = None) -> float: ...
