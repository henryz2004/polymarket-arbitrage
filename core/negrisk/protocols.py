"""
Negrisk Platform Protocols
============================

Protocol definitions that capture the contracts used by the engine and detector.
Existing Polymarket classes (NegriskRegistry, BBATracker) already satisfy these
protocols without modification — structural subtyping via typing.Protocol.
"""

from typing import Callable, Optional, Protocol, runtime_checkable

from core.negrisk.models import NegriskEvent, Outcome


@runtime_checkable
class RegistryProtocol(Protocol):
    """Contract for event discovery and BBA storage."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    def get_all_events(self) -> list[NegriskEvent]: ...
    def get_tradeable_events(self) -> list[NegriskEvent]: ...
    def get_event(self, event_id: str) -> Optional[NegriskEvent]: ...
    def get_event_by_token(self, token_id: str) -> Optional[tuple[NegriskEvent, Outcome]]: ...
    def get_all_token_ids(self) -> list[str]: ...
    def get_stats(self) -> dict: ...


@runtime_checkable
class BBATrackerProtocol(Protocol):
    """Contract for real-time BBA streaming + REST seeding."""

    ws_connected: bool
    last_ws_message_at: Optional["datetime"]

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def fetch_all_prices(self, event: NegriskEvent) -> dict: ...
    def get_gamma_only_tokens(self) -> list[str]: ...
    def get_stats(self) -> dict: ...


@runtime_checkable
class FeeModelProtocol(Protocol):
    """Contract for platform-specific fee calculation."""

    @property
    def gas_per_leg(self) -> float: ...

    def compute_fee_per_share(self, prices: list[float], side: str) -> float: ...
