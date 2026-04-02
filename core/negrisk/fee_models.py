"""
Negrisk Fee Models
====================

Platform-specific fee calculators that implement FeeModelProtocol.

Each model encapsulates:
- Taker fee formula (varies by exchange)
- Gas cost per leg (varies by chain)
"""

from datetime import datetime
from typing import Optional


class PolymarketFeeModel:
    """
    Polymarket fee model using the CTF Exchange on-chain formula.

    Most neg-risk markets: fee_rate_bps=0 (fee-free).
    Fee-enabled markets (e.g. 15-min crypto): fee_rate_bps=1000.

    SELL: fee_per_leg = (fee_rate_bps / 10000) * min(p, 1-p)
    BUY:  fee_per_leg = (fee_rate_bps / 10000) * min(p, 1-p) / p

    Gas: $0 (Polymarket covers gas on Polygon).
    """

    def __init__(self, fee_rate_bps: float = 0, gas_per_leg_usd: float = 0.0):
        self._fee_rate_bps = fee_rate_bps
        self._gas_per_leg = gas_per_leg_usd

    @property
    def gas_per_leg(self) -> float:
        return self._gas_per_leg

    def compute_fee_per_share(self, prices: list[float], side: str, fee_rate_bps_override: Optional[float] = None) -> float:
        """
        Compute total taker fee per share across all legs.

        Uses the Polymarket CTF Exchange formula (CalculatorHelper.sol).

        fee_rate_bps_override: Per-event fee rate (e.g. crypto neg-risk markets
        at 1000 bps). If provided and > 0, overrides the instance default.
        This matters for fee-enabled Polymarket markets — ignoring it would
        show inflated edges and cause real money loss on execution.
        """
        rate = fee_rate_bps_override if fee_rate_bps_override and fee_rate_bps_override > 0 else self._fee_rate_bps
        if rate == 0:
            return 0.0

        base_rate = rate / 10000.0
        total_fee = 0.0

        for p in prices:
            if p <= 0 or p >= 1.0:
                continue
            min_p = min(p, 1.0 - p)
            if side == "BUY":
                total_fee += base_rate * min_p / p
            else:
                total_fee += base_rate * min_p

        return total_fee


class LimitlessFeeModel:
    """
    Limitless Exchange fee model with dynamic lifecycle-based fees.

    Limitless uses a fee that scales over the market's lifetime:
    - Near creation: ~3 bps (0.03%)
    - Near resolution: ~300 bps (3%)

    The API doesn't expose numeric fee rates (only metadata.fee boolean),
    so we estimate based on the market's lifecycle position using
    created_at and expiration_timestamp.

    When a per-event fee_rate_bps is provided (stored on NegriskEvent),
    that takes precedence over the fallback default.

    Gas: ~$0.001 per leg on Base chain.
    """

    # Fee curve parameters (estimated from external sources)
    MIN_FEE_BPS: float = 3.0      # ~0.03% at market creation
    MAX_FEE_BPS: float = 300.0    # ~3% near resolution

    def __init__(self, fee_rate_bps: float = 300, gas_per_leg_usd: float = 0.001):
        """
        Args:
            fee_rate_bps: Fallback fee rate when per-event rate is unavailable.
                          300 bps (3%) is conservative — the worst-case near resolution.
            gas_per_leg_usd: Gas cost per leg in dollars (~$0.001 on Base).
        """
        self._fee_rate_bps = fee_rate_bps
        self._gas_per_leg = gas_per_leg_usd

    @property
    def gas_per_leg(self) -> float:
        return self._gas_per_leg

    @staticmethod
    def estimate_fee_bps(
        created_at: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        now: Optional[datetime] = None,
    ) -> float:
        """
        Estimate the current fee rate in bps based on market lifecycle position.

        Linear interpolation from MIN_FEE_BPS at creation to MAX_FEE_BPS at expiry.

        Args:
            created_at: When the market was created
            end_date: When the market expires/resolves
            now: Current time (default: utcnow)

        Returns:
            Estimated fee in bps (3-300 range). Falls back to MAX_FEE_BPS
            if timestamps are missing.
        """
        if not created_at or not end_date:
            return LimitlessFeeModel.MAX_FEE_BPS

        if now is None:
            now = datetime.utcnow()

        total_duration = (end_date - created_at).total_seconds()
        if total_duration <= 0:
            return LimitlessFeeModel.MAX_FEE_BPS

        elapsed = (now - created_at).total_seconds()
        fraction = max(0.0, min(1.0, elapsed / total_duration))

        fee_range = LimitlessFeeModel.MAX_FEE_BPS - LimitlessFeeModel.MIN_FEE_BPS
        return LimitlessFeeModel.MIN_FEE_BPS + fraction * fee_range

    def compute_fee_per_share(
        self,
        prices: list[float],
        side: str,
        fee_rate_bps_override: Optional[float] = None,
    ) -> float:
        """
        Compute total taker fee per share across all legs.

        Same CTF formula as Polymarket: fee_rate * min(p, 1-p) [/ p for BUY].

        Args:
            prices: List of per-leg prices
            side: "BUY" or "SELL"
            fee_rate_bps_override: Per-event fee rate (from NegriskEvent.fee_rate_bps).
                                   If provided and > 0, overrides the instance default.
        """
        rate = fee_rate_bps_override if fee_rate_bps_override and fee_rate_bps_override > 0 else self._fee_rate_bps
        if rate == 0:
            return 0.0

        base_rate = rate / 10000.0
        total_fee = 0.0

        for p in prices:
            if p <= 0 or p >= 1.0:
                continue
            min_p = min(p, 1.0 - p)
            if side == "BUY":
                total_fee += base_rate * min_p / p
            else:
                total_fee += base_rate * min_p

        return total_fee
