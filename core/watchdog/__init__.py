"""
Suspicious Activity Watchdog
==============================

Monitors Polymarket neg-risk events for suspicious price movements
without corresponding public news catalysts.

Detects:
- Significant price spikes (relative and absolute)
- Off-hours trading anomalies
- Price moves without matching news headlines
"""

from core.watchdog.models import WatchdogConfig, PriceSnapshot, AnomalyAlert
from core.watchdog.price_tracker import PriceTracker
from core.watchdog.anomaly_detector import AnomalyDetector
from core.watchdog.news_checker import NewsChecker
from core.watchdog.alert_dispatcher import (
    AlertDispatcher,
    ConsoleChannel,
    DiscordWebhookChannel,
    FileChannel,
)
from core.watchdog.engine import WatchdogEngine
from core.watchdog.platforms.kalshi import KalshiWatchdogConfig, KalshiWatchdogEngine

__all__ = [
    "WatchdogConfig",
    "PriceSnapshot",
    "AnomalyAlert",
    "PriceTracker",
    "AnomalyDetector",
    "NewsChecker",
    "AlertDispatcher",
    "ConsoleChannel",
    "DiscordWebhookChannel",
    "FileChannel",
    "WatchdogEngine",
    "KalshiWatchdogConfig",
    "KalshiWatchdogEngine",
]
