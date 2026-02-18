"""
Negrisk Fee Models
====================

Platform-specific fee calculators that implement FeeModelProtocol.

Each model encapsulates:
- Taker fee formula (varies by exchange)
- Gas cost per leg (varies by chain)
"""


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

    def compute_fee_per_share(self, prices: list[float], side: str) -> float:
        """
        Compute total taker fee per share across all legs.

        Uses the Polymarket CTF Exchange formula (CalculatorHelper.sol).
        """
        if self._fee_rate_bps == 0:
            return 0.0

        base_rate = self._fee_rate_bps / 10000.0
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
    Limitless Exchange fee model.

    Taker fee: 300 bps (3%) using the same CTF formula: fee_rate * min(p, 1-p).
    Gas: ~$0.001 per leg on Base chain.
    """

    def __init__(self, fee_rate_bps: float = 300, gas_per_leg_usd: float = 0.001):
        self._fee_rate_bps = fee_rate_bps
        self._gas_per_leg = gas_per_leg_usd

    @property
    def gas_per_leg(self) -> float:
        return self._gas_per_leg

    def compute_fee_per_share(self, prices: list[float], side: str) -> float:
        """
        Compute total taker fee per share across all legs.

        Same CTF formula as Polymarket but with fee_rate_bps=300 (3%).
        """
        if self._fee_rate_bps == 0:
            return 0.0

        base_rate = self._fee_rate_bps / 10000.0
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
