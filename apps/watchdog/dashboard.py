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
from datetime import datetime, timedelta
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

        result.append({
            "token_id": token_id,
            "event_id": m.event_id,
            "event_title": m.event_title,
            "event_slug": m.event_slug,
            "outcome_name": m.outcome_name,
            "volume_24h": m.event_volume_24h,
            "current_price": m.current_price,
            "best_bid": current.best_bid if current else None,
            "best_ask": current.best_ask if current else None,
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


# ---------------------------------------------------------------------------
# WebSocket for live updates
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    try:
        while True:
            # Send stats every 5 seconds
            engine = _engine()
            if engine:
                stats = engine.get_stats()
                runtime = datetime.now() - _runner.start_time
                stats["runtime_seconds"] = runtime.total_seconds()
                await ws.send_text(json.dumps({"type": "stats", "data": stats}, default=str))
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

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Watchdog Dashboard</title>
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
  .header {
    background: var(--surface); border-bottom: 1px solid var(--border);
    padding: 16px 24px; display: flex; align-items: center; gap: 24px;
  }
  .header h1 { font-size: 18px; font-weight: 600; color: var(--accent); }
  .header .ws-dot {
    width: 8px; height: 8px; border-radius: 50%; background: var(--red);
    display: inline-block;
  }
  .header .ws-dot.connected { background: var(--green); }
  .metrics {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px; padding: 16px 24px;
  }
  .metric-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 12px 16px;
  }
  .metric-card .label { font-size: 11px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px; }
  .metric-card .value { font-size: 22px; font-weight: 700; margin-top: 4px; }
  .content { padding: 0 24px 24px; }
  .tabs {
    display: flex; gap: 0; margin-bottom: 16px; border-bottom: 1px solid var(--border);
  }
  .tab {
    padding: 10px 20px; cursor: pointer; color: var(--text-dim);
    border-bottom: 2px solid transparent; font-size: 13px; font-weight: 500;
  }
  .tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  .tab:hover { color: var(--text); }
  .panel { display: none; }
  .panel.active { display: block; }
  table { width: 100%; border-collapse: collapse; }
  th, td {
    text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--border);
    font-size: 13px; white-space: nowrap;
  }
  th { color: var(--text-dim); font-weight: 500; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; cursor: pointer; }
  th:hover { color: var(--text); }
  tr:hover td { background: rgba(88,166,255,0.04); }
  .price { font-family: 'SF Mono', 'Fira Code', monospace; }
  .change-pos { color: var(--green); }
  .change-neg { color: var(--red); }
  .change-zero { color: var(--text-dim); }
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
  .alert-card .alert-score {
    font-size: 20px; font-weight: 700; color: var(--yellow);
  }
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
  .empty-state {
    text-align: center; padding: 48px; color: var(--text-dim);
  }
  .search-bar {
    width: 100%; padding: 8px 12px; margin-bottom: 12px;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 6px; color: var(--text); font-size: 13px;
  }
  .search-bar::placeholder { color: var(--text-dim); }
  .truncate { max-width: 300px; overflow: hidden; text-overflow: ellipsis; }
  .link { color: var(--accent); text-decoration: none; }
  .link:hover { text-decoration: underline; }
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
  <div class="metric-card"><div class="label">Markets with Data</div><div class="value" id="m-data">-</div></div>
  <div class="metric-card"><div class="label">Total Scans</div><div class="value" id="m-scans">-</div></div>
  <div class="metric-card"><div class="label">Alerts Fired</div><div class="value" id="m-alerts">-</div></div>
  <div class="metric-card"><div class="label">Highest Score</div><div class="value" id="m-score">-</div></div>
  <div class="metric-card"><div class="label">WS Messages</div><div class="value" id="m-ws">-</div></div>
</div>

<div class="content">
  <div class="tabs">
    <div class="tab active" data-panel="markets-panel">Markets</div>
    <div class="tab" data-panel="alerts-panel">Alerts</div>
  </div>

  <div class="panel active" id="markets-panel">
    <input class="search-bar" id="market-search" placeholder="Search markets..." />
    <table>
      <thead>
        <tr>
          <th data-sort="outcome_name">Outcome</th>
          <th data-sort="event_title">Event</th>
          <th data-sort="current_price">Price</th>
          <th data-sort="change_1h_pct">1h Change</th>
          <th data-sort="change_24h_pct">24h Change</th>
          <th data-sort="volume_24h">Volume 24h</th>
          <th data-sort="snapshots">Samples</th>
          <th data-sort="last_update">Last Update</th>
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
// --- State ---
let markets = [];
let alerts = [];
let sortCol = 'change_1h_pct';
let sortDir = -1; // -1 = desc
let ws = null;

// --- Tabs ---
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById(tab.dataset.panel).classList.add('active');
  });
});

// --- Sorting ---
document.querySelectorAll('th[data-sort]').forEach(th => {
  th.addEventListener('click', () => {
    const col = th.dataset.sort;
    if (sortCol === col) sortDir *= -1;
    else { sortCol = col; sortDir = -1; }
    renderMarkets();
  });
});

// --- Search ---
document.getElementById('market-search').addEventListener('input', () => renderMarkets());

// --- Formatting ---
function fmtPrice(p) {
  if (p == null) return '-';
  return (p * 100).toFixed(1) + '\\u00a2';
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

// --- Render Markets ---
function renderMarkets() {
  const query = document.getElementById('market-search').value.toLowerCase();
  let filtered = markets;
  if (query) {
    filtered = markets.filter(m =>
      m.outcome_name.toLowerCase().includes(query) ||
      m.event_title.toLowerCase().includes(query)
    );
  }

  filtered.sort((a, b) => {
    let va = a[sortCol], vb = b[sortCol];
    if (va == null) va = -Infinity;
    if (vb == null) vb = -Infinity;
    if (typeof va === 'string') return sortDir * va.localeCompare(vb);
    return sortDir * (va - vb);
  });

  const tbody = document.getElementById('markets-body');
  const empty = document.getElementById('markets-empty');

  if (filtered.length === 0) {
    tbody.innerHTML = '';
    empty.style.display = 'block';
    empty.textContent = query ? 'No matches' : 'Loading markets...';
    return;
  }
  empty.style.display = 'none';

  tbody.innerHTML = filtered.map(m => {
    const polyUrl = m.event_slug ? `https://polymarket.com/event/${m.event_slug}` : '#';
    return `<tr>
      <td class="truncate">${esc(m.outcome_name)}</td>
      <td class="truncate"><a class="link" href="${polyUrl}" target="_blank">${esc(m.event_title)}</a></td>
      <td class="price">${fmtPrice(m.current_price)}</td>
      <td>${fmtChange(m.change_1h_pct)}</td>
      <td>${fmtChange(m.change_24h_pct)}</td>
      <td>${fmtVol(m.volume_24h)}</td>
      <td>${m.snapshots}</td>
      <td style="color:var(--text-dim)">${fmtTime(m.last_update)}</td>
    </tr>`;
  }).join('');
}

// --- Render Alerts ---
function renderAlerts() {
  const list = document.getElementById('alerts-list');
  const empty = document.getElementById('alerts-empty');

  if (alerts.length === 0) {
    empty.style.display = 'block';
    return;
  }
  empty.style.display = 'none';

  // Newest first
  const sorted = [...alerts].reverse();
  list.innerHTML = sorted.map(a => {
    const cls = a.news_driven ? 'alert-card news-driven' : 'alert-card';
    const badge = a.news_driven
      ? '<span class="badge badge-news">NEWS-DRIVEN</span>'
      : '<span class="badge badge-unexplained">UNEXPLAINED</span>';
    const dirBadge = a.direction === 'up'
      ? '<span class="badge badge-up">\\u2191 UP</span>'
      : '<span class="badge badge-down">\\u2193 DOWN</span>';
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
        &nbsp; ${fmtPrice(a.price_before)} \\u2192 ${fmtPrice(a.price_after)}
        &nbsp; (${a.abs_change >= 0.01 ? (a.abs_change * 100).toFixed(1) + '\\u00a2' : '<1\\u00a2'} in ${fmtDuration(a.window_seconds)})
        &nbsp; Vol: ${fmtVol(a.event_volume_24h)}
        ${a.is_off_hours ? '&nbsp; <span class="badge badge-unexplained">OFF-HOURS</span>' : ''}
      </div>
      <div class="alert-meta" style="margin-top:6px">${fmtTime(a.detected_at)}</div>
    </div>`;
  }).join('');
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

// --- WebSocket ---
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
      // Flash the alerts tab
      const tab = document.querySelector('[data-panel="alerts-panel"]');
      if (!tab.classList.contains('active')) {
        tab.style.color = 'var(--yellow)';
        setTimeout(() => { tab.style.color = ''; }, 2000);
      }
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

// --- Initial data load ---
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

// --- Periodic market refresh ---
setInterval(async () => {
  try {
    const res = await fetch('/api/markets');
    markets = await res.json();
    renderMarkets();
  } catch (e) {}
}, 15000);

// --- Start ---
loadInitial();
connectWS();
</script>
</body>
</html>"""
