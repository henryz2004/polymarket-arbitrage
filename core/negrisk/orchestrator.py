"""
Multi-Platform Orchestrator
==============================

Manages multiple NegriskEngine instances across platforms.

Each platform gets its own engine with isolated failure domains.
The orchestrator provides unified lifecycle management and stats aggregation.
"""

import asyncio
import logging
from typing import Optional

from core.negrisk.engine import NegriskEngine
from core.negrisk.models import NegriskOpportunity


logger = logging.getLogger(__name__)


class MultiPlatformOrchestrator:
    """
    Orchestrates neg-risk engines across multiple platforms.

    Usage:
        orchestrator = MultiPlatformOrchestrator()
        orchestrator.register_engine("polymarket", poly_engine)
        orchestrator.register_engine("limitless", limitless_engine)
        await orchestrator.start_all()
    """

    def __init__(self):
        self._engines: dict[str, NegriskEngine] = {}

    def register_engine(self, platform: str, engine: NegriskEngine) -> None:
        """Register a platform engine."""
        self._engines[platform] = engine
        logger.info(f"Registered engine for platform: {platform}")

    async def start_all(self) -> None:
        """Start all registered engines concurrently."""
        if not self._engines:
            logger.warning("No engines registered")
            return

        tasks = []
        for platform, engine in self._engines.items():
            logger.info(f"Starting {platform} engine...")
            tasks.append(engine.start())

        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info(f"All {len(self._engines)} engines started")

    async def stop_all(self) -> None:
        """Stop all registered engines."""
        tasks = []
        for platform, engine in self._engines.items():
            logger.info(f"Stopping {platform} engine...")
            tasks.append(engine.stop())

        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("All engines stopped")

    def get_engine(self, platform: str) -> Optional[NegriskEngine]:
        """Get a specific platform engine."""
        return self._engines.get(platform)

    def get_all_stats(self) -> dict[str, dict]:
        """Get merged stats by platform."""
        stats = {}
        for platform, engine in self._engines.items():
            try:
                stats[platform] = engine.get_stats()
            except Exception as e:
                stats[platform] = {"error": str(e)}
        return stats

    def get_all_opportunities(self) -> list[NegriskOpportunity]:
        """Get all recent opportunities sorted by net_edge across platforms."""
        all_opps = []
        for platform, engine in self._engines.items():
            try:
                opps = engine.get_recent_opportunities()
                all_opps.extend(opps)
            except Exception as e:
                logger.debug(f"Error getting opportunities from {platform}: {e}")

        all_opps.sort(key=lambda o: o.net_edge, reverse=True)
        return all_opps

    @property
    def platforms(self) -> list[str]:
        """List of registered platforms."""
        return list(self._engines.keys())
