"""
Alert Dispatcher
==================

Routes anomaly alerts to configured output channels.

Built-in channels:
- ConsoleChannel: colored terminal output
- FileChannel: JSONL append to logs/watchdog/

Extensible via AlertChannel ABC for webhook/Telegram support.
"""

import json
import logging
import os
import sys
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from core.watchdog.models import AnomalyAlert

logger = logging.getLogger(__name__)


def _polymarket_event_url(slug: str) -> Optional[str]:
    slug = (slug or "").strip()
    if not slug:
        return None
    return f"https://polymarket.com/event/{slug}"


class AlertChannel(ABC):
    """Base class for alert output channels."""

    @abstractmethod
    async def send(self, alert: AnomalyAlert) -> None:
        """Send an alert through this channel."""
        ...

    async def start(self) -> None:
        """Initialize the channel (optional)."""
        pass

    async def stop(self) -> None:
        """Cleanup the channel (optional)."""
        pass


class ConsoleChannel(AlertChannel):
    """Prints formatted alerts to stdout with color."""

    # ANSI colors
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    def _color(self, text: str, color: str) -> str:
        """Apply color if stdout is a terminal."""
        if hasattr(sys.stdout, 'isatty') and sys.stdout.isatty():
            return f"{color}{text}{self.RESET}"
        return text

    async def send(self, alert: AnomalyAlert) -> None:
        """Print formatted alert to console."""
        score = alert.suspicion_score

        # Color based on severity
        if score >= 7:
            header_color = self.RED
        elif score >= 5:
            header_color = self.YELLOW
        else:
            header_color = self.CYAN

        direction = getattr(alert, 'direction', 'up')
        sign = "-" if direction == "down" else "+"
        pct_str = f"{sign}{alert.pct_change * 100:.1f}%"
        abs_str = f"${alert.abs_change:.3f}"
        window_str = self._format_window(alert.window_seconds)
        time_str = alert.detected_at.strftime("%b %d %H:%M UTC")
        off_hours_tag = " (OFF HOURS)" if alert.is_off_hours else ""
        direction_label = "SELL-SIDE" if direction == "down" else "BUY-SIDE"

        correlated = getattr(alert, 'correlated_outcomes', 0)
        if alert.news_driven:
            catalyst_label = self._color("NEWS-DRIVEN", f"{self.BOLD}{self.GREEN}")
        else:
            catalyst_label = self._color("UNEXPLAINED", f"{self.BOLD}{self.RED}")

        corr_tag = ""
        if correlated > 0:
            corr_tag = self._color(
                f" [CORRELATED: {correlated} outcomes]",
                f"{self.BOLD}{self.YELLOW}"
            )

        print()
        print(self._color(f"{'=' * 60}", header_color))
        print(self._color(
            f"SUSPICIOUS ACTIVITY [{direction_label}] [Score: {score:.1f}/10]",
            f"{self.BOLD}{header_color}"
        ) + f"  {catalyst_label}{corr_tag}")
        print(self._color(f"{'=' * 60}", header_color))
        print(f"  Market:  {alert.event_title}")
        print(f"  Outcome: {alert.outcome_name} (best of {correlated})" if correlated > 0
              else f"  Outcome: {alert.outcome_name}")
        print(f"  Price:   ${alert.price_before:.3f} -> ${alert.price_after:.3f} "
              f"({pct_str} / {abs_str}) in {window_str}")
        print(f"  Time:    {time_str}{off_hours_tag}")
        print(f"  Volume:  ${alert.event_volume_24h:,.0f} (24h)")

        if alert.news_headlines:
            print(f"  News:    {len(alert.news_headlines)} matching headline(s):")
            for i, headline in enumerate(alert.news_headlines[:3]):
                # Support both NewsHeadline objects and plain strings (legacy)
                if hasattr(headline, 'title'):
                    age = headline.age_minutes
                    age_str = f" ({age:.0f}min ago)" if age is not None else ""
                    pub_str = ""
                    if headline.published_at:
                        pub_str = f" [{headline.published_at.strftime('%H:%M UTC')}]"
                    print(f"           {i+1}. {headline.title[:72]}{pub_str}{age_str}")
                else:
                    print(f"           {i+1}. {headline[:80]}")
        else:
            print(self._color("  News:    No matching headlines found", self.YELLOW))

        event_url = _polymarket_event_url(alert.event_slug)
        if event_url:
            print(f"  Link:    {event_url}")

        print(self._color(f"{'=' * 60}", header_color))
        print()

    def _format_window(self, seconds: int) -> str:
        """Format seconds into human-readable duration."""
        if seconds < 3600:
            return f"{seconds // 60} min"
        elif seconds < 86400:
            hours = seconds / 3600
            return f"{hours:.1f}h"
        else:
            days = seconds / 86400
            return f"{days:.1f}d"


class FileChannel(AlertChannel):
    """Appends JSON lines to a JSONL file in logs/watchdog/."""

    def __init__(self, log_dir: Optional[Path] = None):
        self.log_dir = log_dir or Path("logs/watchdog")
        self._current_date: Optional[str] = None
        self._current_file: Optional[Path] = None

    async def start(self) -> None:
        """Create log directory."""
        self.log_dir.mkdir(parents=True, exist_ok=True)

    async def send(self, alert: AnomalyAlert) -> None:
        """Append alert as JSON line."""
        date_str = datetime.utcnow().strftime("%Y%m%d")

        # Rotate file daily
        if date_str != self._current_date:
            self._current_date = date_str
            self._current_file = self.log_dir / f"alerts_{date_str}.jsonl"

        try:
            with open(self._current_file, "a") as f:
                f.write(json.dumps(alert.to_dict()) + "\n")
        except Exception as e:
            logger.error(f"Failed to write alert to file: {e}")


class DiscordWebhookChannel(AlertChannel):
    """Sends formatted watchdog alerts to a Discord webhook."""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self._client: Optional[httpx.AsyncClient] = None

    @classmethod
    def from_env(cls) -> Optional["DiscordWebhookChannel"]:
        webhook_url = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
        if not webhook_url:
            return None
        if "discord.com/api/webhooks/" not in webhook_url:
            logger.warning("ALERT_WEBHOOK_URL is set but does not look like a Discord webhook")
            return None
        return cls(webhook_url)

    async def start(self) -> None:
        if not self._client:
            self._client = httpx.AsyncClient(timeout=10.0)

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def send(self, alert: AnomalyAlert) -> None:
        if not self._client:
            await self.start()

        direction = getattr(alert, "direction", "up")
        direction_label = "SELL-SIDE" if direction == "down" else "BUY-SIDE"
        catalyst_label = "NEWS-DRIVEN" if alert.news_driven else "UNEXPLAINED"
        color = 0xE74C3C if alert.suspicion_score >= 7 else 0xF1C40F if alert.suspicion_score >= 5 else 0x3498DB
        event_url = _polymarket_event_url(alert.event_slug)

        description = (
            f"**Outcome:** {alert.outcome_name}\n"
            f"**Move:** ${alert.price_before:.3f} -> ${alert.price_after:.3f}\n"
            f"**Change:** {alert.pct_change * 100:.1f}% / ${alert.abs_change:.3f}\n"
            f"**Window:** {self._format_window(alert.window_seconds)}\n"
            f"**Direction:** {direction_label}\n"
            f"**Catalyst:** {catalyst_label}\n"
            f"**Off Hours:** {'Yes' if alert.is_off_hours else 'No'}\n"
            f"**24h Volume:** ${alert.event_volume_24h:,.0f}"
        )
        if getattr(alert, "correlated_outcomes", 0) > 0:
            description += f"\n**Correlated Outcomes:** {alert.correlated_outcomes}"
        if alert.news_headlines:
            top = []
            for headline in alert.news_headlines[:3]:
                top.append(f"- {headline.title if hasattr(headline, 'title') else headline}")
            description += "\n**Headlines:**\n" + "\n".join(top)

        payload = {
            "embeds": [
                {
                    "title": f"Watchdog Alert [{alert.suspicion_score:.1f}/10]",
                    "url": event_url,
                    "description": description,
                    "color": color,
                    "timestamp": alert.detected_at.replace(tzinfo=timezone.utc).isoformat(),
                    "fields": [
                        {"name": "Event", "value": alert.event_title[:1024] or "n/a", "inline": False},
                        {"name": "Slug", "value": alert.event_slug[:1024] or "n/a", "inline": False},
                        {"name": "Market Link", "value": event_url or "n/a", "inline": False},
                    ],
                }
            ]
        }

        try:
            resp = await self._client.post(self.webhook_url, json=payload)
            if resp.status_code >= 400:
                logger.warning(f"Discord webhook returned {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.warning(f"Discord webhook alert failed: {e}")

    def _format_window(self, seconds: int) -> str:
        if seconds < 3600:
            return f"{seconds // 60} min"
        if seconds < 86400:
            return f"{seconds / 3600:.1f}h"
        return f"{seconds / 86400:.1f}d"


class AlertDispatcher:
    """Routes alerts to all configured channels."""

    def __init__(self, channels: Optional[list[AlertChannel]] = None):
        self.channels: list[AlertChannel] = channels or []

    async def start(self) -> None:
        """Start all channels."""
        for channel in self.channels:
            await channel.start()

    async def stop(self) -> None:
        """Stop all channels."""
        for channel in self.channels:
            await channel.stop()

    def add_channel(self, channel: AlertChannel) -> None:
        """Add an output channel."""
        self.channels.append(channel)

    async def dispatch(self, alert: AnomalyAlert) -> None:
        """Send alert to all channels."""
        for channel in self.channels:
            try:
                await channel.send(alert)
            except Exception as e:
                logger.error(f"Alert channel {type(channel).__name__} failed: {e}")
