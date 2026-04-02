"""
Negrisk Alerter
================

Sends alerts when opportunities are detected via:
- Discord webhook
- Telegram bot
- System sound (macOS only)

Usage:
    alerter = NegriskAlerter(webhook_url="https://discord.com/api/webhooks/...")
    await alerter.send_opportunity_alert(opportunity)
    await alerter.send_health_alert("Scanner reconnected after 30s outage")
"""

import asyncio
import json
import logging
import os
import platform
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from core.negrisk.models import NegriskOpportunity

logger = logging.getLogger(__name__)


class NegriskAlerter:
    """Multi-channel alerting for negrisk opportunities and health events."""

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        telegram_bot_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
        enable_sound: bool = True,
        cooldown_seconds: float = 60.0,
    ):
        """
        Initialize alerter.

        Args:
            webhook_url: Discord/Slack webhook URL (auto-detects format)
            telegram_bot_token: Telegram bot token
            telegram_chat_id: Telegram chat ID to send messages to
            enable_sound: Play system sound on macOS (ignored on other OS)
            cooldown_seconds: Min seconds between alerts for same event
        """
        self.webhook_url = webhook_url or os.environ.get("ALERT_WEBHOOK_URL")
        self.telegram_bot_token = telegram_bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = telegram_chat_id or os.environ.get("TELEGRAM_CHAT_ID")
        self.enable_sound = enable_sound and platform.system() == "Darwin"
        self.cooldown_seconds = cooldown_seconds

        self._last_alert: dict[str, float] = {}
        self._client: Optional[httpx.AsyncClient] = None
        self._is_discord = bool(self.webhook_url and "discord.com" in self.webhook_url)
        self._is_slack = bool(self.webhook_url and "hooks.slack.com" in self.webhook_url)

        channels = []
        if self.webhook_url:
            channels.append("discord" if self._is_discord else "slack" if self._is_slack else "webhook")
        if self.telegram_bot_token and self.telegram_chat_id:
            channels.append("telegram")
        if self.enable_sound:
            channels.append("sound")

        logger.info(f"NegriskAlerter initialized: channels={channels or ['none']}")

    async def _get_client(self) -> httpx.AsyncClient:
        if not self._client:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    def _is_cooled_down(self, key: str) -> bool:
        """Check if enough time has passed since last alert for this key."""
        now = time.monotonic()
        last = self._last_alert.get(key, 0)
        if now - last < self.cooldown_seconds:
            return False
        self._last_alert[key] = now
        return True

    async def send_opportunity_alert(self, opp: NegriskOpportunity) -> None:
        """Send alert for a detected opportunity."""
        key = f"opp:{opp.event.event_id}:{opp.direction.value}"
        if not self._is_cooled_down(key):
            return

        direction = opp.direction.value.upper()
        edge_pct = opp.net_edge * 100
        platform_name = opp.event.platform or "polymarket"

        if platform_name == "limitless":
            market_url = f"https://limitless.exchange/markets/{opp.event.slug}"
        else:
            market_url = f"https://polymarket.com/event/{opp.event.slug}"

        # Build message
        title = f"[{direction}] {opp.event.title[:60]}"
        fields = [
            f"Net Edge: {edge_pct:.2f}%",
            f"Gross Edge: {opp.gross_edge * 100:.2f}%",
            f"Legs: {opp.num_legs}",
            f"Size: {opp.suggested_size:.0f} shares",
            f"Cost: ${opp.total_cost:.2f}",
            f"Profit: ${opp.expected_profit:.2f}",
            f"Volume 24h: ${opp.event.volume_24h:,.0f}",
            f"Platform: {platform_name}",
        ]
        body = "\n".join(fields)

        # Fire all channels concurrently
        tasks = []
        if self.webhook_url:
            tasks.append(self._send_webhook(title, body, market_url, edge_pct))
        if self.telegram_bot_token and self.telegram_chat_id:
            tasks.append(self._send_telegram(title, body, market_url))
        if self.enable_sound:
            self._play_sound(edge_pct)

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def send_health_alert(self, message: str, level: str = "warning") -> None:
        """Send a health/status alert (reconnects, errors, etc)."""
        key = f"health:{message[:50]}"
        if not self._is_cooled_down(key):
            return

        title = f"[HEALTH] {level.upper()}"
        tasks = []
        if self.webhook_url:
            tasks.append(self._send_webhook(title, message, color_override=0xFFA500 if level == "warning" else 0xFF0000))
        if self.telegram_bot_token and self.telegram_chat_id:
            tasks.append(self._send_telegram(title, message))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def send_execution_alert(self, opp: NegriskOpportunity, success: bool, reason: str, cost: float = 0.0) -> None:
        """Send alert for execution result."""
        key = f"exec:{opp.opportunity_id}"
        if not self._is_cooled_down(key):
            return

        status = "SUCCESS" if success else "FAILED"
        title = f"[EXEC {status}] {opp.event.title[:50]}"
        body = (
            f"Direction: {opp.direction.value}\n"
            f"Reason: {reason}\n"
            f"Cost: ${cost:.2f}\n"
            f"Expected Profit: ${opp.expected_profit:.2f}"
        )

        color = 0x00FF00 if success else 0xFF0000
        tasks = []
        if self.webhook_url:
            tasks.append(self._send_webhook(title, body, color_override=color))
        if self.telegram_bot_token and self.telegram_chat_id:
            tasks.append(self._send_telegram(title, body))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_webhook(
        self,
        title: str,
        body: str,
        url: Optional[str] = None,
        edge_pct: float = 0.0,
        color_override: Optional[int] = None,
    ) -> None:
        """Send to Discord or Slack webhook."""
        if not self.webhook_url:
            return

        try:
            client = await self._get_client()

            if self._is_discord:
                # Discord embed format
                color = color_override or (0x00FF00 if edge_pct >= 2.0 else 0xFFFF00 if edge_pct >= 1.0 else 0xFF8C00)
                embed = {
                    "title": title,
                    "description": f"```\n{body}\n```",
                    "color": color,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                if url:
                    embed["url"] = url
                payload = {"embeds": [embed]}
            elif self._is_slack:
                # Slack block format
                blocks = [
                    {"type": "header", "text": {"type": "plain_text", "text": title}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"```{body}```"}},
                ]
                if url:
                    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"<{url}|View Market>"}})
                payload = {"blocks": blocks}
            else:
                # Generic webhook (JSON payload)
                payload = {"title": title, "body": body, "url": url, "timestamp": datetime.now(timezone.utc).isoformat()}

            resp = await client.post(self.webhook_url, json=payload)
            if resp.status_code >= 400:
                logger.warning(f"Webhook returned {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.warning(f"Webhook alert failed: {e}")

    async def _send_telegram(self, title: str, body: str, url: Optional[str] = None) -> None:
        """Send to Telegram bot."""
        if not self.telegram_bot_token or not self.telegram_chat_id:
            return

        try:
            client = await self._get_client()
            text = f"*{title}*\n```\n{body}\n```"
            if url:
                text += f"\n[View Market]({url})"

            api_url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
            resp = await client.post(api_url, json={
                "chat_id": self.telegram_chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            })
            if resp.status_code >= 400:
                logger.warning(f"Telegram returned {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.warning(f"Telegram alert failed: {e}")

    def _play_sound(self, edge_pct: float = 0.0) -> None:
        """Play macOS system sound."""
        try:
            subprocess.Popen(
                ["afplay", "/System/Library/Sounds/Purr.aiff"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if edge_pct >= 2.0:
                # High-edge: speak the alert
                subprocess.Popen(
                    ["say", f"Arbitrage alert. {edge_pct:.0f} percent edge."],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass

    async def close(self) -> None:
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
