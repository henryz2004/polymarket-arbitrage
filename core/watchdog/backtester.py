"""
Watchdog Backtester
====================

Replays historical price data through the anomaly detector to verify
it would have caught known insider trading events.

Usage:
    backtester = WatchdogBacktester(config)
    results = await backtester.run(scenarios)

The backtester fetches real CLOB price history and feeds it through the
same PriceTracker + AnomalyDetector pipeline used in production, with
a simulated clock so all time-window calculations work correctly.
"""

import calendar
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import httpx

from core.watchdog.anomaly_detector import AnomalyDetector
from core.watchdog.models import AnomalyAlert, PriceSnapshot, WatchdogConfig
from core.watchdog.price_tracker import PriceTracker, WatchedMarket

logger = logging.getLogger(__name__)


@dataclass
class BacktestScenario:
    """A single backtest scenario targeting a specific market event."""

    name: str  # Human-readable name, e.g. "Iran strike insider trading"
    slug: str  # Gamma API event slug
    description: str = ""  # What happened and why it's suspicious

    # Time range for the backtest (UTC)
    start_time: Optional[datetime] = None  # None = use all available data
    end_time: Optional[datetime] = None

    # Expected result: did insider trading happen?
    expect_alert: bool = True
    # Known insider trading window (for reporting)
    insider_window_start: Optional[datetime] = None
    insider_window_end: Optional[datetime] = None
    # Token IDs to focus on (if known); empty = all tokens in event
    focus_token_ids: list[str] = field(default_factory=list)


@dataclass
class BacktestAlert:
    """An alert that fired during backtesting."""

    scenario_name: str
    alert: AnomalyAlert
    simulated_time: datetime  # When the detector would have caught it

    def to_dict(self) -> dict:
        d = self.alert.to_dict()
        d["scenario_name"] = self.scenario_name
        d["simulated_time"] = self.simulated_time.isoformat()
        return d


@dataclass
class BacktestResult:
    """Result of running a single scenario."""

    scenario: BacktestScenario
    alerts: list[BacktestAlert]
    tokens_fetched: int
    price_points_total: int
    time_range: Optional[tuple[datetime, datetime]] = None  # actual data range

    @property
    def caught(self) -> bool:
        """Did the detector fire at least one alert?"""
        return len(self.alerts) > 0

    @property
    def passed(self) -> bool:
        """Did the result match expectations?"""
        if self.scenario.expect_alert:
            return self.caught
        return not self.caught

    @property
    def max_score(self) -> float:
        if not self.alerts:
            return 0.0
        return max(a.alert.suspicion_score for a in self.alerts)

    @property
    def first_alert_time(self) -> Optional[datetime]:
        if not self.alerts:
            return None
        return min(a.simulated_time for a in self.alerts)

    @property
    def caught_during_insider_window(self) -> bool:
        """Was any alert within the known insider trading window?"""
        if not self.alerts or not self.scenario.insider_window_start:
            return False
        for a in self.alerts:
            t = a.simulated_time
            if t >= self.scenario.insider_window_start:
                if self.scenario.insider_window_end is None or t <= self.scenario.insider_window_end:
                    return True
        return False

    def summary(self) -> str:
        """Human-readable summary."""
        lines = []
        status = "PASS" if self.passed else "FAIL"
        lines.append(f"[{status}] {self.scenario.name}")
        lines.append(f"  Tokens: {self.tokens_fetched} | Price points: {self.price_points_total}")
        if self.time_range:
            lines.append(f"  Data range: {self.time_range[0]:%Y-%m-%d %H:%M} — {self.time_range[1]:%Y-%m-%d %H:%M}")
        lines.append(f"  Alerts fired: {len(self.alerts)} | Max score: {self.max_score:.1f}/10")
        if self.first_alert_time:
            lines.append(f"  First alert: {self.first_alert_time:%Y-%m-%d %H:%M:%S}")
        if self.scenario.insider_window_start:
            caught_str = "YES" if self.caught_during_insider_window else "NO"
            lines.append(f"  Caught during insider window: {caught_str}")
        for a in self.alerts:
            lines.append(
                f"    [{a.alert.suspicion_score:.1f}] {a.alert.outcome_name}: "
                f"{a.alert.price_before:.3f} -> {a.alert.price_after:.3f} "
                f"({a.alert.pct_change:.0%}) at {a.simulated_time:%H:%M:%S}"
            )
        return "\n".join(lines)


class WatchdogBacktester:
    """
    Replays historical price data through the watchdog anomaly detector.

    Approach:
    1. Fetch price history from CLOB /prices-history endpoint
    2. Create a PriceTracker + AnomalyDetector with production config
    3. Feed snapshots chronologically, running detection at each scan interval
    4. Collect all alerts that would have fired
    """

    CLOB_URL = "https://clob.polymarket.com"
    GAMMA_URL = "https://gamma-api.polymarket.com"

    def __init__(self, config: Optional[WatchdogConfig] = None):
        self.config = config or WatchdogConfig()

    async def run(self, scenarios: list[BacktestScenario]) -> list[BacktestResult]:
        """Run all scenarios and return results."""
        results = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            for scenario in scenarios:
                logger.info(f"Running backtest: {scenario.name}")
                try:
                    result = await self._run_scenario(client, scenario)
                    results.append(result)
                except Exception as e:
                    logger.error(f"Scenario '{scenario.name}' failed: {e}", exc_info=True)
                    results.append(BacktestResult(
                        scenario=scenario,
                        alerts=[],
                        tokens_fetched=0,
                        price_points_total=0,
                    ))
        return results

    async def _run_scenario(
        self, client: httpx.AsyncClient, scenario: BacktestScenario
    ) -> BacktestResult:
        """Run a single backtest scenario."""

        # 1. Discover event and its markets from Gamma API
        markets = await self._fetch_event_markets(client, scenario.slug)
        if not markets:
            logger.warning(f"No markets found for slug '{scenario.slug}'")
            return BacktestResult(
                scenario=scenario, alerts=[], tokens_fetched=0, price_points_total=0
            )

        # 2. Fetch price history for each token
        all_timeseries: dict[str, list[PriceSnapshot]] = {}
        total_points = 0

        for market_info in markets:
            token_id = market_info["token_id"]

            # Skip if scenario focuses on specific tokens and this isn't one
            if scenario.focus_token_ids and token_id not in scenario.focus_token_ids:
                continue

            history = await self._fetch_price_history(client, token_id)
            if not history:
                continue

            # Filter to scenario time range
            if scenario.start_time:
                history = [s for s in history if s.timestamp >= scenario.start_time]
            if scenario.end_time:
                history = [s for s in history if s.timestamp <= scenario.end_time]

            if history:
                all_timeseries[token_id] = history
                total_points += len(history)

        if not all_timeseries:
            logger.warning(f"No price data for any token in '{scenario.slug}'")
            return BacktestResult(
                scenario=scenario, alerts=[], tokens_fetched=0, price_points_total=0
            )

        # Compute actual data time range
        all_timestamps = []
        for series in all_timeseries.values():
            all_timestamps.extend(s.timestamp for s in series)
        time_range = (min(all_timestamps), max(all_timestamps))

        # 3. Build tracker + detector
        tracker = PriceTracker(self.config)
        detector = AnomalyDetector(self.config)

        # Register each token as a watched market
        token_to_market = {}
        for market_info in markets:
            tid = market_info["token_id"]
            if tid not in all_timeseries:
                continue
            wm = WatchedMarket(
                token_id=tid,
                event_id=market_info.get("event_id", ""),
                outcome_name=market_info.get("outcome_name", ""),
                event_title=market_info.get("event_title", ""),
                event_slug=scenario.slug,
                event_volume_24h=market_info.get("volume_24h", 0),
                max_history_hours=self.config.price_history_window_hours,
            )
            tracker._markets[tid] = wm
            token_to_market[tid] = wm

        # 4. Merge all snapshots into a single chronological timeline
        timeline: list[tuple[str, PriceSnapshot]] = []
        for token_id, series in all_timeseries.items():
            for snap in series:
                timeline.append((token_id, snap))
        timeline.sort(key=lambda x: x[1].timestamp)

        # 5. Replay through the detector
        alerts: list[BacktestAlert] = []
        scan_interval = timedelta(seconds=self.config.price_poll_interval_seconds)
        last_scan_time: Optional[datetime] = None

        for token_id, snapshot in timeline:
            wm = token_to_market.get(token_id)
            if not wm:
                continue

            # Inject snapshot into the market's history
            wm.history.append(snapshot)

            # Run detection scan at configured intervals
            current_time = snapshot.timestamp
            should_scan = (
                last_scan_time is None
                or (current_time - last_scan_time) >= scan_interval
            )

            if should_scan:
                last_scan_time = current_time

                # Override datetime.utcnow() for cooldown checks in the detector
                with patch("core.watchdog.anomaly_detector.datetime") as mock_dt:
                    mock_dt.utcnow.return_value = current_time
                    mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

                    for tid in all_timeseries:
                        alert = detector.check_market(tid, tracker)
                        if alert:
                            alerts.append(BacktestAlert(
                                scenario_name=scenario.name,
                                alert=alert,
                                simulated_time=current_time,
                            ))

        return BacktestResult(
            scenario=scenario,
            alerts=alerts,
            tokens_fetched=len(all_timeseries),
            price_points_total=total_points,
            time_range=time_range,
        )

    async def _fetch_event_markets(
        self, client: httpx.AsyncClient, slug: str
    ) -> list[dict]:
        """Fetch event markets from Gamma API."""
        resp = await client.get(f"{self.GAMMA_URL}/events", params={"slug": slug})
        if resp.status_code != 200:
            logger.error(f"Gamma API returned {resp.status_code} for slug '{slug}'")
            return []

        events = resp.json()
        if not events:
            return []

        event = events[0]
        event_id = str(event.get("id", ""))
        event_title = event.get("title", "")
        event_vol = float(event.get("volume24hr", 0) or 0)
        markets_data = event.get("markets", [])

        result = []
        for m in markets_data:
            # Each market has two tokens (YES and NO). We track the YES token.
            tokens = m.get("clobTokenIds", "")
            try:
                token_ids = json.loads(tokens) if isinstance(tokens, str) else tokens
            except (json.JSONDecodeError, TypeError):
                token_ids = []

            if not token_ids:
                continue

            yes_token = token_ids[0]  # First token is YES
            outcome_name = m.get("groupItemTitle", "") or m.get("question", "")[:50]

            result.append({
                "token_id": yes_token,
                "event_id": event_id,
                "event_title": event_title,
                "outcome_name": outcome_name,
                "volume_24h": event_vol,
                "active": m.get("active", True),
                "closed": m.get("closed", False),
            })

        return result

    async def _fetch_price_history(
        self, client: httpx.AsyncClient, token_id: str
    ) -> list[PriceSnapshot]:
        """Fetch historical prices from CLOB prices-history endpoint."""
        try:
            resp = await client.get(
                f"{self.CLOB_URL}/prices-history",
                params={"market": token_id, "interval": "max", "fidelity": 1},
            )
            if resp.status_code != 200:
                return []

            data = resp.json()
            history = data.get("history", [])

            snapshots = []
            for point in history:
                ts = point.get("t")
                price = point.get("p")
                if ts is None or price is None:
                    continue
                try:
                    if isinstance(ts, (int, float)):
                        timestamp = datetime.utcfromtimestamp(ts)
                    else:
                        continue

                    snapshots.append(PriceSnapshot(
                        timestamp=timestamp,
                        mid_price=float(price),
                        source="backtest",  # Passes through live-source filter
                    ))
                except (ValueError, TypeError):
                    continue

            snapshots.sort(key=lambda s: s.timestamp)
            return snapshots

        except Exception as e:
            logger.debug(f"Price history fetch failed for {token_id[:12]}: {e}")
            return []

    async def run_and_cache(
        self,
        scenarios: list[BacktestScenario],
        cache_dir: str = "logs/watchdog/backtest_cache",
    ) -> list[BacktestResult]:
        """
        Run scenarios with local caching of price data.

        Cached data allows reproducible offline backtests without hitting the API.
        """
        cache_path = Path(cache_dir)
        cache_path.mkdir(parents=True, exist_ok=True)

        results = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            for scenario in scenarios:
                logger.info(f"Running backtest (cached): {scenario.name}")

                # Check cache
                cache_file = cache_path / f"{scenario.slug}.jsonl"
                if cache_file.exists():
                    logger.info(f"  Using cached data: {cache_file}")
                    market_data, price_data = self._load_cache(cache_file)
                else:
                    # Fetch and cache
                    market_data = await self._fetch_event_markets(client, scenario.slug)
                    price_data = {}
                    for m in market_data:
                        tid = m["token_id"]
                        if scenario.focus_token_ids and tid not in scenario.focus_token_ids:
                            continue
                        history = await self._fetch_price_history(client, tid)
                        if history:
                            price_data[tid] = history
                    self._save_cache(cache_file, market_data, price_data)
                    logger.info(f"  Cached {len(price_data)} tokens to {cache_file}")

                # Run detection with cached data
                result = self._run_from_cache(scenario, market_data, price_data)
                results.append(result)

        return results

    def _save_cache(
        self,
        path: Path,
        markets: list[dict],
        prices: dict[str, list[PriceSnapshot]],
    ) -> None:
        """Save fetched data to cache file."""
        with open(path, "w") as f:
            # First line: market metadata
            f.write(json.dumps({"type": "markets", "data": markets}) + "\n")
            # Subsequent lines: price data per token
            # Use calendar.timegm for UTC-safe epoch conversion (naive datetimes
            # are treated as UTC, matching utcfromtimestamp in _load_cache)
            for token_id, snapshots in prices.items():
                points = [
                    {"t": calendar.timegm(s.timestamp.timetuple()), "p": s.mid_price}
                    for s in snapshots
                ]
                f.write(json.dumps({
                    "type": "prices",
                    "token_id": token_id,
                    "count": len(points),
                    "data": points,
                }) + "\n")

    def _load_cache(
        self, path: Path
    ) -> tuple[list[dict], dict[str, list[PriceSnapshot]]]:
        """Load data from cache file."""
        markets = []
        prices: dict[str, list[PriceSnapshot]] = {}

        with open(path) as f:
            for line in f:
                record = json.loads(line)
                if record["type"] == "markets":
                    markets = record["data"]
                elif record["type"] == "prices":
                    token_id = record["token_id"]
                    snapshots = []
                    for point in record["data"]:
                        snapshots.append(PriceSnapshot(
                            timestamp=datetime.utcfromtimestamp(point["t"]),
                            mid_price=float(point["p"]),
                            source="backtest",
                        ))
                    prices[token_id] = snapshots

        return markets, prices

    def _run_from_cache(
        self,
        scenario: BacktestScenario,
        market_data: list[dict],
        price_data: dict[str, list[PriceSnapshot]],
    ) -> BacktestResult:
        """Run detection on cached data (no API calls)."""

        # Filter prices to scenario time range
        filtered_prices: dict[str, list[PriceSnapshot]] = {}
        for tid, series in price_data.items():
            if scenario.focus_token_ids and tid not in scenario.focus_token_ids:
                continue
            filtered = series
            if scenario.start_time:
                filtered = [s for s in filtered if s.timestamp >= scenario.start_time]
            if scenario.end_time:
                filtered = [s for s in filtered if s.timestamp <= scenario.end_time]
            if filtered:
                filtered_prices[tid] = filtered

        if not filtered_prices:
            return BacktestResult(
                scenario=scenario, alerts=[], tokens_fetched=0, price_points_total=0
            )

        # Compute time range
        all_ts = []
        for series in filtered_prices.values():
            all_ts.extend(s.timestamp for s in series)
        time_range = (min(all_ts), max(all_ts))
        total_points = sum(len(s) for s in filtered_prices.values())

        # Build tracker + detector
        tracker = PriceTracker(self.config)
        detector = AnomalyDetector(self.config)

        token_to_market = {}
        for m in market_data:
            tid = m["token_id"]
            if tid not in filtered_prices:
                continue
            wm = WatchedMarket(
                token_id=tid,
                event_id=m.get("event_id", ""),
                outcome_name=m.get("outcome_name", ""),
                event_title=m.get("event_title", ""),
                event_slug=scenario.slug,
                event_volume_24h=m.get("volume_24h", 0),
                max_history_hours=self.config.price_history_window_hours,
            )
            tracker._markets[tid] = wm
            token_to_market[tid] = wm

        # Merge into chronological timeline
        timeline: list[tuple[str, PriceSnapshot]] = []
        for tid, series in filtered_prices.items():
            for snap in series:
                timeline.append((tid, snap))
        timeline.sort(key=lambda x: x[1].timestamp)

        # Replay
        alerts: list[BacktestAlert] = []
        scan_interval = timedelta(seconds=self.config.price_poll_interval_seconds)
        last_scan_time: Optional[datetime] = None

        for token_id, snapshot in timeline:
            wm = token_to_market.get(token_id)
            if not wm:
                continue

            wm.history.append(snapshot)

            current_time = snapshot.timestamp
            should_scan = (
                last_scan_time is None
                or (current_time - last_scan_time) >= scan_interval
            )

            if should_scan:
                last_scan_time = current_time

                with patch("core.watchdog.anomaly_detector.datetime") as mock_dt:
                    mock_dt.utcnow.return_value = current_time
                    mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

                    for tid in filtered_prices:
                        alert = detector.check_market(tid, tracker)
                        if alert:
                            alerts.append(BacktestAlert(
                                scenario_name=scenario.name,
                                alert=alert,
                                simulated_time=current_time,
                            ))

        return BacktestResult(
            scenario=scenario,
            alerts=alerts,
            tokens_fetched=len(filtered_prices),
            price_points_total=total_points,
            time_range=time_range,
        )


# ============================================================================
# Built-in ground truth scenarios
# ============================================================================

IRAN_SCENARIOS = [
    BacktestScenario(
        name="Iran strike — Feb 28 insider trading",
        slug="usisrael-strikes-iran-on",
        description=(
            "US-Israel joint strikes on Iran began Feb 28, 2026. Six wallets "
            "made $1.2M+ by positioning before news. Price jumped from ~7c to "
            "25.5c at 2:15 AM PST (10:15 UTC), 21 hours before public news."
        ),
        start_time=datetime(2026, 2, 27, 0, 0),
        end_time=datetime(2026, 3, 2, 0, 0),
        expect_alert=True,
        insider_window_start=datetime(2026, 2, 28, 8, 0),  # ~8 UTC = midnight PST
        insider_window_end=datetime(2026, 2, 28, 14, 0),  # Before news broke
    ),
    BacktestScenario(
        name="Iran ceasefire — insider positioning at 6c",
        slug="us-x-iran-ceasefire-by",
        description=(
            "10 new wallets bought ceasefire outcomes at 6c before Trump's "
            "'winding down' comments. $2M+ in suspected insider profits."
        ),
        start_time=datetime(2026, 3, 1, 0, 0),
        end_time=datetime(2026, 3, 15, 0, 0),
        expect_alert=True,
    ),
    BacktestScenario(
        name="US forces enter Iran — market creation spike",
        slug="us-forces-enter-iran-by",
        description=(
            "New market created after strikes began. Early outcomes saw rapid "
            "price movements as informed traders positioned."
        ),
        start_time=datetime(2026, 2, 28, 0, 0),
        end_time=datetime(2026, 3, 10, 0, 0),
        expect_alert=True,
    ),
    BacktestScenario(
        name="Iran conflict end — ceasefire negotiation signals",
        slug="iran-x-israelus-conflict-ends-by",
        description=(
            "Conflict end market tracks diplomatic developments. Spikes "
            "correlated with leaked negotiation progress."
        ),
        start_time=datetime(2026, 3, 1, 0, 0),
        end_time=datetime(2026, 3, 25, 0, 0),
        expect_alert=True,
    ),
    BacktestScenario(
        name="Trump end military ops — policy signal trading",
        slug="trump-announces-end-of-military-operations-against-iran-by",
        description=(
            "Market tracking Trump's military policy announcements. "
            "Insider knowledge of White House decisions."
        ),
        start_time=datetime(2026, 3, 1, 0, 0),
        end_time=datetime(2026, 3, 25, 0, 0),
        expect_alert=True,
    ),
]

# Non-Iran scenarios for broader validation
OTHER_SCENARIOS = [
    BacktestScenario(
        name="France municipal elections — expected resolution",
        slug="who-will-win-the-nice-mayoral-election",
        description=(
            "Nice mayoral election resolved Mar 23. Price moved from ~1c to 50c "
            "as results came in. News-driven, not insider trading."
        ),
        start_time=datetime(2026, 3, 20, 0, 0),
        end_time=datetime(2026, 3, 24, 0, 0),
        expect_alert=True,  # Should still alert (we catch it, then news enrichment marks it)
    ),
]

ALL_SCENARIOS = IRAN_SCENARIOS + OTHER_SCENARIOS
