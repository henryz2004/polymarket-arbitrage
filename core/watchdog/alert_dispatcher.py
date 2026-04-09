"""
Alert Dispatcher
==================

Routes anomaly alerts to configured output channels.

Built-in channels:
- ConsoleChannel: colored terminal output
- FileChannel: JSONL append to logs/watchdog/

Extensible via AlertChannel ABC for future webhook/Telegram support.
"""

import json
import logging
import sys
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.watchdog.models import AnomalyAlert

logger = logging.getLogger(__name__)


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
                print(f"           {i+1}. {headline[:80]}")
        else:
            print(self._color("  News:    No matching headlines found", self.YELLOW))

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
