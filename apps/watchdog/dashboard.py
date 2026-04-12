"""
Watchdog Dashboard Server
==========================

Lightweight FastAPI server that runs alongside the watchdog engine,
serving a real-time dashboard showing watched markets, price activity,
and alerts. Shares the event loop with the watchdog for zero-copy
access to engine state.

Usage:
    # Standalone (starts watchdog + dashboard together)
    python -m apps.watchdog dashboard --platform polymarket --port 8080
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Watchdog Dashboard")

# Mutable reference set by start_dashboard() once the runner is live.
_runner = None  # type: ignore
_alert_history: list[dict] = []
_ws_clients: set[WebSocket] = set()


def _engine():
    return _runner.engine if _runner else None


# ---------------------------------------------------------------------------
# Alert hook — called by the engine's dispatcher to push alerts to the UI
# ---------------------------------------------------------------------------

async def _on_alert(alert_dict: dict) -> None:
    """Called when a new alert fires. Stores it and broadcasts to WS clients."""
    _alert_history.append(alert_dict)
    # Keep last 200 alerts in memory
    if len(_alert_history) > 200:
        _alert_history.pop(0)

    msg = json.dumps({"type": "alert", "data": alert_dict}, default=str)
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    _ws_clients -= dead


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    return DASHBOARD_HTML


@app.get("/api/stats")
async def get_stats():
    engine = _engine()
    if not engine:
        return {"error": "engine not started"}

    stats = engine.get_stats()
    runtime = datetime.now() - _runner.start_time
    stats["runtime_seconds"] = runtime.total_seconds()
    stats["started_at"] = _runner.start_time.isoformat()
    return stats


@app.get("/api/markets")
async def get_markets():
    engine = _engine()
    if not engine:
        return []

    markets = engine.price_tracker.get_watched_markets()
    result = []
    for token_id, m in markets.items():
        current = m.current_snapshot
        price_1h = engine.price_tracker.get_price_change(token_id, 3600)
        price_24h = engine.price_tracker.get_price_change(token_id, 86400)

        # Get the Gamma API probability as display price — much more accurate
        # than mid-price when spreads are wide (e.g. bid=0.001, ask=0.999 → mid=0.5).
        gamma_price = None
        registry_result = engine.registry.get_event_by_token(token_id)
        if registry_result:
            _, outcome = registry_result
            gamma_price = outcome.gamma_probability

        # Use gamma price as display price; fall back to mid-price only when
        # gamma is unavailable (rare).
        best_bid = current.best_bid if current else None
        best_ask = current.best_ask if current else None
        spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None
        display_price = gamma_price if gamma_price is not None else m.current_price

        result.append({
            "token_id": token_id,
            "event_id": m.event_id,
            "event_title": m.event_title,
            "event_slug": m.event_slug,
            "outcome_name": m.outcome_name,
            "volume_24h": m.event_volume_24h,
            "current_price": display_price,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "last_update": current.timestamp.isoformat() if current else None,
            "source": current.source if current else None,
            "snapshots": len(m.live_history),
            "change_1h_pct": price_1h[2] if price_1h else None,
            "change_24h_pct": price_24h[2] if price_24h else None,
        })

    # Sort by absolute 1h change descending (most active first)
    result.sort(key=lambda x: abs(x["change_1h_pct"] or 0), reverse=True)
    return result


@app.get("/api/market/{token_id}")
async def get_market_detail(token_id: str):
    engine = _engine()
    if not engine:
        return {"error": "engine not started"}

    markets = engine.price_tracker.get_watched_markets()
    m = markets.get(token_id)
    if not m:
        return {"error": "market not found"}

    # Return price history (downsample if too many points)
    history = list(m.live_history)
    if len(history) > 500:
        step = len(history) // 500
        history = history[::step]

    return {
        "token_id": token_id,
        "event_title": m.event_title,
        "outcome_name": m.outcome_name,
        "event_slug": m.event_slug,
        "volume_24h": m.event_volume_24h,
        "current_price": m.current_price,
        "history": [
            {
                "t": s.timestamp.isoformat(),
                "p": round(s.mid_price, 4) if s.mid_price else None,
                "bid": s.best_bid,
                "ask": s.best_ask,
            }
            for s in history
        ],
    }


@app.get("/api/alerts")
async def get_alerts():
    # Also load from JSONL files if in-memory is empty
    if not _alert_history:
        _load_alert_history()
    return _alert_history[-50:]  # Last 50


def _load_alert_history():
    """Load alerts from JSONL files on disk."""
    alert_dir = Path("logs/watchdog")
    if not alert_dir.exists():
        return
    for f in sorted(alert_dir.glob("alerts_*.jsonl")):
        try:
            for line in f.read_text().strip().split("\n"):
                if line:
                    _alert_history.append(json.loads(line))
        except Exception:
            pass


def _parse_naive_utc(ts: str) -> Optional[datetime]:
    """Parse an ISO timestamp and return a naive-UTC datetime for comparison."""
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    # Strip timezone info — treat everything as UTC
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _load_alerts_since(hours: int) -> list[dict]:
    """Load alerts from JSONL files within the last N hours."""
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    alerts = []
    alert_dir = Path("logs/watchdog")
    if not alert_dir.exists():
        return alerts
    for f in sorted(alert_dir.glob("alerts_*.jsonl")):
        try:
            for line in f.read_text().strip().split("\n"):
                if not line:
                    continue
                alert = json.loads(line)
                dt = _parse_naive_utc(alert.get("detected_at", ""))
                if dt is None:
                    continue
                if dt >= cutoff:
                    alerts.append(alert)
        except Exception:
            pass
    # Also include in-memory alerts not yet flushed to disk
    for alert in _alert_history:
        dt = _parse_naive_utc(alert.get("detected_at", ""))
        if dt is None:
            continue
        if dt >= cutoff and not any(
            a.get("alert_id") == alert.get("alert_id") for a in alerts
        ):
            alerts.append(alert)
    alerts.sort(key=lambda a: a.get("detected_at", ""))
    return alerts


def _enrich_alert_with_current_price(alert: dict, engine) -> dict:
    """Add current market state to an alert so an investigator can compare."""
    token_id = alert.get("token_id")
    if not token_id or not engine:
        return alert

    enriched = dict(alert)
    markets = engine.price_tracker.get_watched_markets()
    m = markets.get(token_id)
    if m:
        enriched["current_price"] = m.current_price
        current = m.current_snapshot
        if current:
            enriched["current_bid"] = current.best_bid
            enriched["current_ask"] = current.best_ask
            enriched["current_source"] = current.source
            enriched["current_as_of"] = current.timestamp.isoformat()

        # Price trajectory since alert: where did price go after the spike?
        alert_time = _parse_naive_utc(alert.get("detected_at", ""))

        if alert_time and m.live_history:
            post_alert = [
                s for s in m.live_history if s.timestamp > alert_time
            ]
            if post_alert:
                prices = [s.mid_price for s in post_alert if s.mid_price is not None]
                if prices:
                    enriched["post_alert_high"] = round(max(prices), 4)
                    enriched["post_alert_low"] = round(min(prices), 4)
                    enriched["post_alert_latest"] = round(prices[-1], 4)
                    enriched["post_alert_samples"] = len(prices)

        # Did price revert? (potential false positive indicator)
        price_after = alert.get("price_after")
        if price_after and m.current_price is not None:
            revert = m.current_price - price_after
            enriched["price_reversion"] = round(revert, 4)
            enriched["reverted"] = (
                abs(revert) > 0.03
                and (
                    (alert.get("direction") == "up" and revert < -0.03)
                    or (alert.get("direction") == "down" and revert > 0.03)
                )
            )
    else:
        enriched["current_price"] = None
        enriched["market_still_watched"] = False

    return enriched


@app.get("/api/status")
async def get_full_status(lookback_hours: int = 24):
    """Comprehensive status endpoint for remote agent investigation.

    Returns system health, enriched alerts with current market context,
    and market summaries — enough for an agent to classify signals as
    true/false positive/negative.

    Query params:
        lookback_hours: how far back to fetch alerts (default 24)
    """
    engine = _engine()
    now = datetime.utcnow()

    # --- System health ---
    health = {"status": "offline", "checked_at": now.isoformat()}
    if engine and _runner:
        stats = engine.get_stats()
        runtime = datetime.now() - _runner.start_time
        health.update({
            "status": "running" if stats.get("running") else "stopped",
            "uptime_seconds": round(runtime.total_seconds()),
            "uptime_human": str(timedelta(seconds=int(runtime.total_seconds()))),
            "started_at": _runner.start_time.isoformat(),
            "total_scans": stats.get("total_scans", 0),
            "total_alerts": stats.get("total_alerts", 0),
            "markets_watched": stats.get("price_tracker", {}).get("markets_watched", 0),
            "markets_with_data": stats.get("price_tracker", {}).get("markets_with_data", 0),
            "total_snapshots": stats.get("price_tracker", {}).get("total_snapshots", 0),
            "detector_checks": stats.get("anomaly_detector", {}).get("checks_performed", 0),
            "detector_highest_score": stats.get("anomaly_detector", {}).get("highest_score", 0),
            "active_cooldowns": stats.get("anomaly_detector", {}).get("active_cooldowns", 0),
            "ws_messages": stats.get("websocket", {}).get("ws_messages", 0),
        })

    # --- Alerts with enrichment ---
    raw_alerts = _load_alerts_since(lookback_hours)
    enriched_alerts = [
        _enrich_alert_with_current_price(a, engine) for a in raw_alerts
    ]

    # Summary stats on alerts
    alert_summary = {
        "count": len(enriched_alerts),
        "lookback_hours": lookback_hours,
        "by_score_bucket": {
            "critical_7_plus": len([a for a in enriched_alerts if a.get("suspicion_score", 0) >= 7]),
            "high_5_to_7": len([a for a in enriched_alerts if 5 <= a.get("suspicion_score", 0) < 7]),
            "medium_3_to_5": len([a for a in enriched_alerts if 3 <= a.get("suspicion_score", 0) < 5]),
            "low_under_3": len([a for a in enriched_alerts if a.get("suspicion_score", 0) < 3]),
        },
        "news_driven_count": len([a for a in enriched_alerts if a.get("news_driven")]),
        "unexplained_count": len([a for a in enriched_alerts if not a.get("news_driven")]),
        "off_hours_count": len([a for a in enriched_alerts if a.get("is_off_hours")]),
        "reverted_count": len([a for a in enriched_alerts if a.get("reverted")]),
    }

    # --- Top movers (markets with biggest recent price changes) ---
    top_movers = []
    if engine:
        markets = engine.price_tracker.get_watched_markets()
        for token_id, m in markets.items():
            change_1h = engine.price_tracker.get_price_change(token_id, 3600)
            change_4h = engine.price_tracker.get_price_change(token_id, 14400)
            change_24h = engine.price_tracker.get_price_change(token_id, 86400)
            if not any([change_1h, change_4h, change_24h]):
                continue
            top_movers.append({
                "token_id": token_id,
                "event_title": m.event_title,
                "event_slug": m.event_slug,
                "outcome_name": m.outcome_name,
                "volume_24h": m.event_volume_24h,
                "current_price": m.current_price,
                "change_1h_pct": round(change_1h[2], 2) if change_1h else None,
                "change_4h_pct": round(change_4h[2], 2) if change_4h else None,
                "change_24h_pct": round(change_24h[2], 2) if change_24h else None,
                "snapshots": len(m.live_history),
            })
        top_movers.sort(
            key=lambda x: abs(x.get("change_1h_pct") or x.get("change_4h_pct") or 0),
            reverse=True,
        )
        top_movers = top_movers[:30]

    return {
        "health": health,
        "alert_summary": alert_summary,
        "alerts": enriched_alerts,
        "top_movers": top_movers,
    }


# ---------------------------------------------------------------------------
# WebSocket for live updates
# ---------------------------------------------------------------------------

# Track last-known prices for computing deltas
_last_prices: dict[str, float] = {}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    try:
        while True:
            engine = _engine()
            if engine:
                # Stats
                stats = engine.get_stats()
                runtime = datetime.now() - _runner.start_time
                stats["runtime_seconds"] = runtime.total_seconds()
                await ws.send_text(json.dumps({"type": "stats", "data": stats}, default=str))

                # Price deltas — use gamma probability for display
                updates = []
                watched = engine.price_tracker.get_watched_markets()
                for token_id, m in watched.items():
                    # Prefer gamma probability for display accuracy
                    gamma_price = None
                    reg = engine.registry.get_event_by_token(token_id)
                    if reg:
                        gamma_price = reg[1].gamma_probability
                    price = gamma_price if gamma_price is not None else m.current_price
                    if price is None:
                        continue
                    prev = _last_prices.get(token_id)
                    if prev is not None and abs(price - prev) > 0.0001:
                        change = (price - prev) / prev if prev > 0 else 0
                        updates.append({
                            "token_id": token_id,
                            "outcome_name": m.outcome_name,
                            "event_title": m.event_title,
                            "price": round(price, 4),
                            "prev_price": round(prev, 4),
                            "change_pct": round(change, 4),
                        })
                    _last_prices[token_id] = price

                if updates:
                    # Sort by absolute change, send top 20
                    updates.sort(key=lambda u: abs(u["change_pct"]), reverse=True)
                    await ws.send_text(json.dumps(
                        {"type": "price_updates", "data": updates[:20]}, default=str
                    ))

            await asyncio.sleep(5)
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        _ws_clients.discard(ws)


# ---------------------------------------------------------------------------
# Startup helper
# ---------------------------------------------------------------------------

async def start_dashboard(runner, port: int = 8080):
    """Start the dashboard server alongside an already-started WatchdogRunner."""
    global _runner
    _runner = runner

    # Hook into the alert dispatcher to get real-time alerts
    if runner.engine and runner.engine.dispatcher:
        runner.engine.dispatcher.add_callback(_on_alert)

    import uvicorn
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    logger.info(f"Dashboard available at http://0.0.0.0:{port}")
    await server.serve()


# ---------------------------------------------------------------------------
# HTML / JS / CSS — single-page dashboard
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Watchdog Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #c9d1d9; --text-dim: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --red: #f85149; --yellow: #d29922;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', monospace;
    background: var(--bg); color: var(--text); font-size: 14px;
  }
  /* --- Header --- */
  .header {
    background: var(--surface); border-bottom: 1px solid var(--border);
    padding: 16px 24px; display: flex; align-items: center; gap: 24px;
  }
  .header h1 { font-size: 18px; font-weight: 600; color: var(--accent); }
  .ws-dot {
    width: 8px; height: 8px; border-radius: 50%; background: var(--red);
    display: inline-block;
  }
  .ws-dot.connected { background: var(--green); }

  /* --- Metrics --- */
  .metrics {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 12px; padding: 16px 24px;
  }
  .metric-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 12px 16px;
  }
  .metric-card .label { font-size: 11px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px; }
  .metric-card .value { font-size: 22px; font-weight: 700; margin-top: 4px; }

  /* --- Live Ticker --- */
  .ticker-strip {
    padding: 0 24px; margin-bottom: 8px; height: 36px;
    display: flex; align-items: center; gap: 8px; overflow: hidden;
  }
  .ticker-strip .ticker-label {
    font-size: 11px; color: var(--text-dim); text-transform: uppercase;
    letter-spacing: 0.5px; flex-shrink: 0;
  }
  .ticker-items { display: flex; gap: 8px; overflow: hidden; }
  .ticker-pill {
    display: inline-flex; align-items: center; gap: 4px;
    padding: 3px 10px; border-radius: 4px; font-size: 12px;
    white-space: nowrap; flex-shrink: 0;
  }
  .ticker-pill.up { background: rgba(63,185,80,0.12); color: var(--green); }
  .ticker-pill.down { background: rgba(248,81,73,0.12); color: var(--red); }
  .ticker-pill.pop-in {
    animation: tickerPopIn 0.35s cubic-bezier(0.16, 1, 0.3, 1) forwards;
  }
  .ticker-pill.fade-out {
    animation: tickerFadeOut 0.25s ease forwards;
    pointer-events: none;
  }
  @keyframes tickerPopIn {
    from { opacity: 0; transform: translateX(-24px) scale(0.9); }
    to   { opacity: 1; transform: translateX(0) scale(1); }
  }
  @keyframes tickerFadeOut {
    from { opacity: 1; transform: scale(1); }
    to   { opacity: 0; transform: scale(0.85); }
  }

  /* --- Content --- */
  .content { padding: 0 24px 24px; }
  .tabs {
    display: flex; gap: 0; margin-bottom: 12px; border-bottom: 1px solid var(--border);
  }
  .tab {
    padding: 10px 20px; cursor: pointer; color: var(--text-dim);
    border-bottom: 2px solid transparent; font-size: 13px; font-weight: 500;
  }
  .tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  .tab:hover { color: var(--text); }
  .tab .tab-count {
    font-size: 11px; background: var(--border); color: var(--text-dim);
    padding: 1px 6px; border-radius: 8px; margin-left: 6px;
  }
  .panel { display: none; }
  .panel.active { display: block; }

  /* --- Filter bar --- */
  .filter-bar {
    display: flex; gap: 8px; align-items: center; margin-bottom: 12px;
  }
  .filter-btn {
    padding: 5px 14px; border-radius: 6px; font-size: 12px; font-weight: 500;
    cursor: pointer; border: 1px solid var(--border); background: transparent;
    color: var(--text-dim); transition: all 0.15s;
  }
  .filter-btn.active { background: var(--accent); color: #0d1117; border-color: var(--accent); }
  .filter-btn:hover:not(.active) { border-color: var(--text-dim); color: var(--text); }
  .search-bar {
    flex: 1; padding: 6px 12px;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 6px; color: var(--text); font-size: 13px;
  }
  .search-bar::placeholder { color: var(--text-dim); }

  /* --- Table --- */
  table { width: 100%; border-collapse: collapse; }
  th, td {
    text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--border);
    font-size: 13px; white-space: nowrap;
  }
  th {
    color: var(--text-dim); font-weight: 500; font-size: 11px;
    text-transform: uppercase; letter-spacing: 0.5px; cursor: pointer;
    user-select: none;
  }
  th:hover { color: var(--text); }
  th .sort-arrow { font-size: 10px; margin-left: 3px; }

  /* Event group rows */
  .event-row { cursor: pointer; }
  .event-row td { background: var(--surface); font-weight: 600; }
  .event-row:hover td { background: rgba(88,166,255,0.06); }
  .event-row .chevron {
    display: inline-block; transition: transform 0.2s; font-size: 10px;
    margin-right: 6px; color: var(--text-dim);
  }
  .event-row.expanded .chevron { transform: rotate(90deg); }
  .outcome-row { cursor: pointer; }
  .outcome-row td:first-child { padding-left: 32px; }
  .outcome-row:hover td { background: rgba(88,166,255,0.04); }

  /* Row flash on price change */
  .flash-green td { background: rgba(63,185,80,0.1) !important; transition: background 0.3s; }
  .flash-red td { background: rgba(248,81,73,0.1) !important; transition: background 0.3s; }

  /* Chart detail row */
  .chart-row td {
    padding: 16px; background: var(--surface);
    border-bottom: 2px solid var(--border);
  }
  .chart-container { position: relative; height: 220px; width: 100%; }
  .chart-header {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 8px;
  }
  .chart-title { font-weight: 600; font-size: 14px; }
  .chart-meta { font-size: 12px; color: var(--text-dim); }
  .chart-loading { text-align: center; padding: 40px; color: var(--text-dim); }

  .price { font-family: 'SF Mono', 'Fira Code', monospace; }
  .change-pos { color: var(--green); }
  .change-neg { color: var(--red); }
  .change-zero { color: var(--text-dim); }

  /* --- Alerts --- */
  .alert-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 16px; margin-bottom: 12px;
    border-left: 3px solid var(--yellow);
  }
  .alert-card.news-driven { border-left-color: var(--text-dim); }
  .alert-card .alert-header {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 8px;
  }
  .alert-card .alert-title { font-weight: 600; font-size: 14px; }
  .alert-card .alert-score { font-size: 20px; font-weight: 700; color: var(--yellow); }
  .alert-card .alert-meta { font-size: 12px; color: var(--text-dim); }
  .alert-card .alert-detail { margin-top: 8px; font-size: 13px; }
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 4px;
    font-size: 11px; font-weight: 600;
  }
  .badge-unexplained { background: rgba(210,153,34,0.15); color: var(--yellow); }
  .badge-news { background: rgba(139,148,158,0.15); color: var(--text-dim); }
  .badge-up { background: rgba(63,185,80,0.15); color: var(--green); }
  .badge-down { background: rgba(248,81,73,0.15); color: var(--red); }

  .empty-state { text-align: center; padding: 48px; color: var(--text-dim); }
  .truncate { max-width: 280px; overflow: hidden; text-overflow: ellipsis; }
  .link { color: var(--accent); text-decoration: none; }
  .link:hover { text-decoration: underline; }
  .outcome-count {
    font-size: 11px; color: var(--text-dim); font-weight: 400;
    margin-left: 6px;
  }
</style>
</head>
<body>

<div class="header">
  <h1>Watchdog</h1>
  <span><span class="ws-dot" id="ws-dot"></span> <span id="ws-label" style="font-size:12px;color:var(--text-dim)">connecting...</span></span>
  <span style="margin-left:auto;font-size:12px;color:var(--text-dim)" id="uptime"></span>
</div>

<div class="metrics">
  <div class="metric-card"><div class="label">Markets Watched</div><div class="value" id="m-watched">-</div></div>
  <div class="metric-card"><div class="label">With Data</div><div class="value" id="m-data">-</div></div>
  <div class="metric-card"><div class="label">Scans</div><div class="value" id="m-scans">-</div></div>
  <div class="metric-card"><div class="label">Alerts</div><div class="value" id="m-alerts">-</div></div>
  <div class="metric-card"><div class="label">Top Score</div><div class="value" id="m-score">-</div></div>
  <div class="metric-card"><div class="label">WS Msgs</div><div class="value" id="m-ws">-</div></div>
</div>

<div class="ticker-strip">
  <span class="ticker-label">Live</span>
  <div class="ticker-items" id="ticker-items"></div>
</div>

<div class="content">
  <div class="tabs">
    <div class="tab active" data-panel="markets-panel">Markets <span class="tab-count" id="markets-count">0</span></div>
    <div class="tab" data-panel="alerts-panel">Alerts <span class="tab-count" id="alerts-count">0</span></div>
  </div>

  <div class="panel active" id="markets-panel">
    <div class="filter-bar">
      <button class="filter-btn active" data-filter="active">Active</button>
      <button class="filter-btn" data-filter="all">All</button>
      <button class="filter-btn" data-filter="alerts">With Alerts</button>
      <input class="search-bar" id="market-search" placeholder="Search events or outcomes..." />
    </div>
    <table>
      <thead>
        <tr>
          <th data-sort="event_title">Event / Outcome</th>
          <th data-sort="current_price">Price</th>
          <th data-sort="change_1h_pct">1h</th>
          <th data-sort="change_24h_pct">24h</th>
          <th data-sort="volume_24h">Volume</th>
          <th data-sort="snapshots">Samples</th>
        </tr>
      </thead>
      <tbody id="markets-body"></tbody>
    </table>
    <div class="empty-state" id="markets-empty">Loading markets...</div>
  </div>

  <div class="panel" id="alerts-panel">
    <div id="alerts-list">
      <div class="empty-state" id="alerts-empty">No alerts yet. The watchdog is scanning...</div>
    </div>
  </div>
</div>

<script>
// =========================================================================
// State
// =========================================================================
let markets = [];
let alerts = [];
let sortCol = 'change_1h_pct';
let sortDir = -1;
let activeFilter = 'active';
let expandedEvents = new Set();
let openChartToken = null;
let chartInstance = null;
let chartCache = new Map(); // token_id -> {data, fetchedAt}
let alertTokens = new Set();
let tickerItems = [];
let ws = null;

// =========================================================================
// Tabs
// =========================================================================
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById(tab.dataset.panel).classList.add('active');
  });
});

// =========================================================================
// Filter buttons
// =========================================================================
document.querySelectorAll('.filter-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    activeFilter = btn.dataset.filter;
    renderMarkets();
  });
});

// =========================================================================
// Sorting
// =========================================================================
document.querySelectorAll('th[data-sort]').forEach(th => {
  th.addEventListener('click', () => {
    const col = th.dataset.sort;
    if (sortCol === col) sortDir *= -1;
    else { sortCol = col; sortDir = -1; }
    renderMarkets();
  });
});

// =========================================================================
// Search
// =========================================================================
document.getElementById('market-search').addEventListener('input', () => renderMarkets());

// =========================================================================
// Formatting helpers
// =========================================================================
function fmtPrice(p) {
  if (p == null) return '-';
  return (p * 100).toFixed(1) + '\u00a2';
}
function fmtChange(pct) {
  if (pct == null) return '<span class="change-zero">-</span>';
  const sign = pct >= 0 ? '+' : '';
  const cls = pct > 0.001 ? 'change-pos' : pct < -0.001 ? 'change-neg' : 'change-zero';
  return `<span class="${cls}">${sign}${(pct * 100).toFixed(1)}%</span>`;
}
function fmtVol(v) {
  if (v == null) return '-';
  if (v >= 1e6) return '$' + (v/1e6).toFixed(1) + 'M';
  if (v >= 1e3) return '$' + (v/1e3).toFixed(1) + 'K';
  return '$' + v.toFixed(0);
}
function fmtTime(iso) {
  if (!iso) return '-';
  const d = new Date(iso + 'Z');
  return d.toLocaleTimeString();
}
function fmtDuration(secs) {
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (h > 0) return h + 'h ' + m + 'm';
  return m + 'm';
}
function esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

// =========================================================================
// Group markets by event
// =========================================================================
function groupByEvent(marketList) {
  const groups = new Map();
  for (const m of marketList) {
    if (!groups.has(m.event_id)) {
      groups.set(m.event_id, {
        event_id: m.event_id,
        event_title: m.event_title,
        event_slug: m.event_slug,
        volume_24h: m.volume_24h,
        outcomes: [],
        max_change_1h: 0,
        max_change_24h: 0,
        total_snapshots: 0,
      });
    }
    const g = groups.get(m.event_id);
    g.outcomes.push(m);
    g.max_change_1h = Math.max(g.max_change_1h, Math.abs(m.change_1h_pct || 0));
    g.max_change_24h = Math.max(g.max_change_24h, Math.abs(m.change_24h_pct || 0));
    g.total_snapshots += m.snapshots || 0;
  }
  return Array.from(groups.values());
}

// =========================================================================
// Filter logic
// =========================================================================
function applyFilter(groups) {
  const query = document.getElementById('market-search').value.toLowerCase();

  return groups.filter(g => {
    // Text search
    if (query) {
      const match = g.event_title.toLowerCase().includes(query) ||
        g.outcomes.some(o => o.outcome_name.toLowerCase().includes(query));
      if (!match) return false;
    }

    // Activity filter
    if (activeFilter === 'active') {
      return g.max_change_1h > 0.005 || g.total_snapshots > 10;
    }
    if (activeFilter === 'alerts') {
      return g.outcomes.some(o => alertTokens.has(o.token_id));
    }
    return true; // 'all'
  });
}

// =========================================================================
// Render Markets (grouped by event)
// =========================================================================
function renderMarkets() {
  const groups = groupByEvent(markets);
  const filtered = applyFilter(groups);

  // Sort groups
  filtered.sort((a, b) => {
    let va, vb;
    if (sortCol === 'change_1h_pct') { va = a.max_change_1h; vb = b.max_change_1h; }
    else if (sortCol === 'change_24h_pct') { va = a.max_change_24h; vb = b.max_change_24h; }
    else if (sortCol === 'volume_24h') { va = a.volume_24h; vb = b.volume_24h; }
    else if (sortCol === 'snapshots') { va = a.total_snapshots; vb = b.total_snapshots; }
    else { va = a.event_title; vb = b.event_title;
      if (typeof va === 'string') return sortDir * va.localeCompare(vb);
    }
    return sortDir * ((va || 0) - (vb || 0));
  });

  const tbody = document.getElementById('markets-body');
  const empty = document.getElementById('markets-empty');

  // Update counts
  const totalOutcomes = filtered.reduce((s, g) => s + g.outcomes.length, 0);
  document.getElementById('markets-count').textContent = totalOutcomes;

  if (filtered.length === 0) {
    tbody.innerHTML = '';
    empty.style.display = 'block';
    empty.textContent = document.getElementById('market-search').value ? 'No matches' : 'No active markets yet...';
    return;
  }
  empty.style.display = 'none';

  // Sort arrows
  document.querySelectorAll('th[data-sort]').forEach(th => {
    const arrow = th.querySelector('.sort-arrow');
    if (arrow) arrow.remove();
    if (th.dataset.sort === sortCol) {
      th.insertAdjacentHTML('beforeend',
        `<span class="sort-arrow">${sortDir === -1 ? '\u25BC' : '\u25B2'}</span>`);
    }
  });

  let html = '';
  for (const g of filtered) {
    const expanded = expandedEvents.has(g.event_id);
    const polyUrl = g.event_slug ? `https://polymarket.com/event/${g.event_slug}` : '#';
    const chevron = expanded ? '\u25B6' : '\u25B6';
    const bestOutcome = g.outcomes.reduce((best, o) =>
      Math.abs(o.change_1h_pct || 0) > Math.abs(best.change_1h_pct || 0) ? o : best, g.outcomes[0]);

    html += `<tr class="event-row ${expanded ? 'expanded' : ''}" data-event-id="${g.event_id}">
      <td>
        <span class="chevron">\u25B6</span>
        <a class="link" href="${polyUrl}" target="_blank">${esc(g.event_title)}</a>
        <span class="outcome-count">${g.outcomes.length} outcome${g.outcomes.length > 1 ? 's' : ''}</span>
      </td>
      <td class="price">${fmtPrice(bestOutcome.current_price)}</td>
      <td>${fmtChange(g.max_change_1h > 0.0001 ? g.max_change_1h : null)}</td>
      <td>${fmtChange(g.max_change_24h > 0.0001 ? g.max_change_24h : null)}</td>
      <td>${fmtVol(g.volume_24h)}</td>
      <td>${g.total_snapshots}</td>
    </tr>`;

    if (expanded) {
      // Sort outcomes by absolute 1h change
      const sorted = [...g.outcomes].sort((a, b) =>
        Math.abs(b.change_1h_pct || 0) - Math.abs(a.change_1h_pct || 0));

      for (const o of sorted) {
        const isChartOpen = openChartToken === o.token_id;
        html += `<tr class="outcome-row" data-token-id="${o.token_id}">
          <td>${esc(o.outcome_name)}</td>
          <td class="price">${fmtPrice(o.current_price)}</td>
          <td>${fmtChange(o.change_1h_pct)}</td>
          <td>${fmtChange(o.change_24h_pct)}</td>
          <td>${fmtVol(o.volume_24h)}</td>
          <td>${o.snapshots}</td>
        </tr>`;

        if (isChartOpen) {
          html += `<tr class="chart-row"><td colspan="6">
            <div class="chart-header">
              <div>
                <span class="chart-title">${esc(o.outcome_name)}</span>
                <span class="chart-meta"> &mdash; ${esc(o.event_title)}</span>
              </div>
              <span class="chart-meta">${fmtPrice(o.current_price)} | ${fmtVol(o.volume_24h)} vol</span>
            </div>
            <div class="chart-container"><canvas id="price-chart"></canvas></div>
          </td></tr>`;
        }
      }
    }
  }
  tbody.innerHTML = html;

  // Attach event row click handlers
  tbody.querySelectorAll('.event-row').forEach(row => {
    row.addEventListener('click', (e) => {
      if (e.target.closest('a')) return; // Don't toggle on link click
      const eid = row.dataset.eventId;
      if (expandedEvents.has(eid)) expandedEvents.delete(eid);
      else expandedEvents.add(eid);
      renderMarkets();
    });
  });

  // Attach outcome row click handlers for chart
  tbody.querySelectorAll('.outcome-row').forEach(row => {
    row.addEventListener('click', () => {
      const tid = row.dataset.tokenId;
      if (openChartToken === tid) {
        openChartToken = null;
      } else {
        openChartToken = tid;
      }
      renderMarkets();
      if (openChartToken) loadChart(openChartToken);
    });
  });

  // Re-render chart if open
  if (openChartToken && document.getElementById('price-chart')) {
    loadChart(openChartToken);
  }
}

// =========================================================================
// Chart rendering
// =========================================================================
async function loadChart(tokenId) {
  const canvas = document.getElementById('price-chart');
  if (!canvas) return;

  // Check cache
  const cached = chartCache.get(tokenId);
  if (cached && Date.now() - cached.fetchedAt < 60000) {
    drawChart(canvas, cached.data);
    return;
  }

  // Show loading
  const container = canvas.parentElement;
  container.innerHTML = '<div class="chart-loading">Loading chart...</div>';

  try {
    const res = await fetch(`/api/market/${tokenId}`);
    const data = await res.json();
    chartCache.set(tokenId, { data, fetchedAt: Date.now() });

    // Re-check canvas exists (user might have closed)
    const canvas2 = document.getElementById('price-chart');
    if (canvas2) drawChart(canvas2, data);
    else {
      // Rebuild canvas
      container.innerHTML = '<canvas id="price-chart"></canvas>';
      drawChart(document.getElementById('price-chart'), data);
    }
  } catch (e) {
    container.innerHTML = '<div class="chart-loading">Failed to load chart</div>';
  }
}

function drawChart(canvas, data) {
  if (chartInstance) { chartInstance.destroy(); chartInstance = null; }
  if (!data.history || data.history.length === 0) {
    canvas.parentElement.innerHTML = '<div class="chart-loading">No price history yet</div>';
    return;
  }

  const labels = data.history.map(h => new Date(h.t + 'Z'));
  const prices = data.history.map(h => h.p ? h.p * 100 : null); // cents
  const bids = data.history.map(h => h.bid ? h.bid * 100 : null);
  const asks = data.history.map(h => h.ask ? h.ask * 100 : null);

  chartInstance = new Chart(canvas, {
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label: 'Ask',
          data: asks,
          borderColor: 'transparent',
          backgroundColor: 'rgba(88,166,255,0.08)',
          fill: '+1',
          pointRadius: 0,
          tension: 0.3,
        },
        {
          label: 'Mid Price',
          data: prices,
          borderColor: '#58a6ff',
          borderWidth: 2,
          backgroundColor: 'transparent',
          fill: false,
          pointRadius: 0,
          tension: 0.3,
        },
        {
          label: 'Bid',
          data: bids,
          borderColor: 'transparent',
          backgroundColor: 'rgba(88,166,255,0.08)',
          fill: '-1',
          pointRadius: 0,
          tension: 0.3,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 300 },
      interaction: { intersect: false, mode: 'index' },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#161b22',
          borderColor: '#30363d',
          borderWidth: 1,
          titleColor: '#c9d1d9',
          bodyColor: '#c9d1d9',
          callbacks: {
            title: (items) => {
              if (items[0]) return new Date(items[0].parsed.x).toLocaleString();
              return '';
            },
            label: (item) => {
              if (item.datasetIndex === 1) return `Price: ${item.parsed.y.toFixed(1)}\u00a2`;
              if (item.datasetIndex === 0) return `Ask: ${item.parsed.y.toFixed(1)}\u00a2`;
              if (item.datasetIndex === 2) return `Bid: ${item.parsed.y.toFixed(1)}\u00a2`;
              return '';
            },
          },
        },
      },
      scales: {
        x: {
          type: 'time',
          time: { tooltipFormat: 'MMM d, HH:mm' },
          grid: { color: 'rgba(48,54,61,0.5)' },
          ticks: { color: '#8b949e', maxTicksLimit: 8, font: { size: 11 } },
        },
        y: {
          grid: { color: 'rgba(48,54,61,0.5)' },
          ticks: {
            color: '#8b949e',
            font: { size: 11 },
            callback: (v) => v.toFixed(1) + '\u00a2',
          },
        },
      },
    },
  });
}

// =========================================================================
// Render Alerts
// =========================================================================
function renderAlerts() {
  const list = document.getElementById('alerts-list');
  const empty = document.getElementById('alerts-empty');
  document.getElementById('alerts-count').textContent = alerts.length;

  // Track tokens with alerts
  alertTokens = new Set(alerts.map(a => a.token_id));

  if (alerts.length === 0) {
    empty.style.display = 'block';
    return;
  }
  empty.style.display = 'none';

  const sorted = [...alerts].reverse();
  list.innerHTML = sorted.map(a => {
    const cls = a.news_driven ? 'alert-card news-driven' : 'alert-card';
    const badge = a.news_driven
      ? '<span class="badge badge-news">NEWS-DRIVEN</span>'
      : '<span class="badge badge-unexplained">UNEXPLAINED</span>';
    const dirBadge = a.direction === 'up'
      ? '<span class="badge badge-up">\u2191 UP</span>'
      : '<span class="badge badge-down">\u2193 DOWN</span>';
    const polyUrl = a.event_slug ? `https://polymarket.com/event/${a.event_slug}` : '#';

    return `<div class="${cls}">
      <div class="alert-header">
        <div>
          <div class="alert-title">${esc(a.outcome_name)}</div>
          <div class="alert-meta"><a class="link" href="${polyUrl}" target="_blank">${esc(a.event_title)}</a></div>
        </div>
        <div class="alert-score">${a.suspicion_score.toFixed(1)}</div>
      </div>
      <div class="alert-detail">
        ${dirBadge} ${badge}
        &nbsp; ${fmtPrice(a.price_before)} \u2192 ${fmtPrice(a.price_after)}
        &nbsp; (${a.abs_change >= 0.01 ? (a.abs_change * 100).toFixed(1) + '\u00a2' : '<1\u00a2'} in ${fmtDuration(a.window_seconds)})
        &nbsp; Vol: ${fmtVol(a.event_volume_24h)}
        ${a.is_off_hours ? '&nbsp; <span class="badge badge-unexplained">OFF-HOURS</span>' : ''}
      </div>
      <div class="alert-meta" style="margin-top:6px">${fmtTime(a.detected_at)}</div>
    </div>`;
  }).join('');
}

// =========================================================================
// Live Ticker
// =========================================================================
const MAX_TICKER = 12;

function addTickerItem(update) {
  const container = document.getElementById('ticker-items');
  const cls = update.change_pct >= 0 ? 'up' : 'down';
  const arrow = update.change_pct >= 0 ? '\u2191' : '\u2193';
  const name = update.outcome_name.length > 20 ? update.outcome_name.slice(0, 18) + '..' : update.outcome_name;

  const pill = document.createElement('span');
  pill.className = `ticker-pill ${cls} pop-in`;
  pill.textContent = `${name} ${fmtPrice(update.prev_price)}\u2192${fmtPrice(update.price)} ${arrow}${Math.abs(update.change_pct * 100).toFixed(1)}%`;
  container.insertBefore(pill, container.firstChild);

  // Remove pop-in class after animation completes so it doesn't replay
  pill.addEventListener('animationend', () => pill.classList.remove('pop-in'), { once: true });

  // Trim excess pills with fade-out
  while (container.children.length > MAX_TICKER) {
    const last = container.lastElementChild;
    last.classList.add('fade-out');
    last.addEventListener('animationend', () => last.remove(), { once: true });
    // Break to remove one at a time per cycle
    break;
  }
}

// =========================================================================
// Row flash on price update
// =========================================================================
function flashRows(updates) {
  for (const u of updates) {
    const row = document.querySelector(`[data-token-id="${u.token_id}"]`);
    if (!row) continue;
    const cls = u.change_pct >= 0 ? 'flash-green' : 'flash-red';
    row.classList.add(cls);
    setTimeout(() => row.classList.remove(cls), 1500);
  }
}

// =========================================================================
// WebSocket
// =========================================================================
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => {
    document.getElementById('ws-dot').classList.add('connected');
    document.getElementById('ws-label').textContent = 'live';
  };

  ws.onclose = () => {
    document.getElementById('ws-dot').classList.remove('connected');
    document.getElementById('ws-label').textContent = 'reconnecting...';
    setTimeout(connectWS, 3000);
  };

  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'stats') {
      updateMetrics(msg.data);
    } else if (msg.type === 'alert') {
      alerts.push(msg.data);
      renderAlerts();
      const tab = document.querySelector('[data-panel="alerts-panel"]');
      if (!tab.classList.contains('active')) {
        tab.style.color = 'var(--yellow)';
        setTimeout(() => { tab.style.color = ''; }, 2000);
      }
    } else if (msg.type === 'price_updates') {
      // Stagger pill insertions so they pop in one by one
      msg.data.forEach((u, i) => {
        setTimeout(() => addTickerItem(u), i * 80);
        const m = markets.find(x => x.token_id === u.token_id);
        if (m) m.current_price = u.price;
      });
      flashRows(msg.data);
    }
  };
}

function updateMetrics(stats) {
  document.getElementById('m-watched').textContent = stats.price_tracker?.markets_watched ?? '-';
  document.getElementById('m-data').textContent = stats.price_tracker?.markets_with_data ?? '-';
  document.getElementById('m-scans').textContent = stats.total_scans ?? '-';
  document.getElementById('m-alerts').textContent = stats.total_alerts ?? '-';
  document.getElementById('m-score').textContent = (stats.anomaly_detector?.highest_score ?? 0).toFixed(1);
  document.getElementById('m-ws').textContent = (stats.websocket?.ws_messages ?? 0).toLocaleString();

  if (stats.runtime_seconds) {
    document.getElementById('uptime').textContent = 'Uptime: ' + fmtDuration(stats.runtime_seconds);
  }
}

// =========================================================================
// Initial data load + periodic refresh
// =========================================================================
async function loadInitial() {
  try {
    const [mRes, aRes] = await Promise.all([
      fetch('/api/markets'), fetch('/api/alerts')
    ]);
    markets = await mRes.json();
    alerts = await aRes.json();
    renderMarkets();
    renderAlerts();
  } catch (e) {
    console.error('Failed to load initial data', e);
  }
}

setInterval(async () => {
  try {
    const res = await fetch('/api/markets');
    markets = await res.json();
    renderMarkets();
  } catch (e) {}
}, 15000);

// =========================================================================
// Start
// =========================================================================
loadInitial();
connectWS();
</script>
</body>
</html>"""
