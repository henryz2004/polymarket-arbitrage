"""
Negrisk BBA Data Recorder
===========================

Records every BBA update (WebSocket + CLOB) to JSONL files for
offline backtesting. Captures full order book depth, source,
timing, and event metadata.

Usage:
    recorder = BBARecorder(output_dir="logs/negrisk/recordings")
    recorder.start()

    # Hook into BBA tracker
    tracker.on_price_update = recorder.wrap_callback(tracker.on_price_update)

    # Or use with engine
    engine = NegriskEngine(..., recorder=recorder)

    recorder.stop()

Data format (one JSON object per line):
    {
        "ts": "2026-03-30T12:00:00.123456",    # UTC timestamp
        "mono": 123456.789,                      # monotonic time (for latency)
        "type": "bba_update",                    # record type
        "token_id": "123...",                    # token being updated
        "event_id": "abc...",                    # parent event
        "source": "websocket",                   # websocket | clob | gamma
        "best_bid": 0.45,
        "best_ask": 0.47,
        "bid_size": 100.0,
        "ask_size": 150.0,
        "bid_levels": [{"p": 0.45, "s": 100}, {"p": 0.44, "s": 200}],
        "ask_levels": [{"p": 0.47, "s": 150}, {"p": 0.48, "s": 300}],
        "sequence_id": 42
    }

Event metadata snapshots (written periodically):
    {
        "ts": "...",
        "type": "event_snapshot",
        "events": [
            {
                "event_id": "...",
                "slug": "...",
                "title": "...",
                "platform": "polymarket",
                "volume_24h": 50000,
                "fee_rate_bps": 0,
                "outcomes": [
                    {"outcome_id": "...", "token_id": "...", "name": "Trump", "status": "active"},
                    ...
                ]
            }
        ]
    }
"""

import asyncio
import gzip
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from core.negrisk.models import NegriskEvent, OutcomeBBA

logger = logging.getLogger(__name__)


class BBARecorder:
    """
    Records BBA updates to JSONL files for offline backtesting.

    Hooks into the registry's update_outcome_bba method to capture
    every price update with full depth data.
    """

    def __init__(
        self,
        output_dir: str = "logs/negrisk/recordings",
        compress: bool = False,
        snapshot_interval_seconds: float = 300.0,
        flush_interval_seconds: float = 5.0,
        max_file_size_mb: float = 100.0,
    ):
        """
        Initialize the BBA recorder.

        Args:
            output_dir: Directory to write recording files
            compress: Use gzip compression (.jsonl.gz)
            snapshot_interval_seconds: How often to write event metadata snapshots
            flush_interval_seconds: How often to flush the write buffer
            max_file_size_mb: Rotate file when it exceeds this size
        """
        self.output_dir = Path(output_dir)
        self.compress = compress
        self.snapshot_interval = snapshot_interval_seconds
        self.flush_interval = flush_interval_seconds
        self.max_file_size = max_file_size_mb * 1024 * 1024  # Convert to bytes

        self._file = None
        self._file_path: Optional[Path] = None
        self._running = False
        self._write_buffer: list[str] = []
        self._buffer_lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None
        self._snapshot_task: Optional[asyncio.Task] = None

        # Stats
        self._records_written: int = 0
        self._bytes_written: int = 0
        self._start_time: Optional[datetime] = None

        # Registry reference (set when attached)
        self._registry = None

    def start(self) -> None:
        """Start recording."""
        if self._running:
            return

        self._running = True
        self._start_time = datetime.utcnow()

        # Create output directory
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Open recording file
        self._open_new_file()

        # Write session header
        header = {
            "ts": datetime.utcnow().isoformat(),
            "type": "session_start",
            "version": 1,
            "compress": self.compress,
            "snapshot_interval": self.snapshot_interval,
        }
        self._write_record(header)

        logger.info(f"BBA Recorder started: {self._file_path}")

    def stop(self) -> None:
        """Stop recording and flush remaining data."""
        if not self._running:
            return

        self._running = False

        # Write session footer
        footer = {
            "ts": datetime.utcnow().isoformat(),
            "type": "session_end",
            "records_written": self._records_written,
            "bytes_written": self._bytes_written,
            "duration_seconds": (datetime.utcnow() - self._start_time).total_seconds()
            if self._start_time
            else 0,
        }
        self._write_record(footer)

        # Flush and close
        self._flush_buffer_sync()
        if self._file:
            self._file.close()
            self._file = None

        logger.info(
            f"BBA Recorder stopped: {self._records_written} records, "
            f"{self._bytes_written / 1024 / 1024:.1f} MB written"
        )

    async def start_async_tasks(self) -> None:
        """Start async background tasks (flush + snapshot loops)."""
        self._flush_task = asyncio.create_task(
            self._flush_loop(), name="bba_recorder_flush"
        )
        if self._registry:
            self._snapshot_task = asyncio.create_task(
                self._snapshot_loop(), name="bba_recorder_snapshot"
            )

    async def stop_async_tasks(self) -> None:
        """Stop async background tasks."""
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

        if self._snapshot_task:
            self._snapshot_task.cancel()
            try:
                await self._snapshot_task
            except asyncio.CancelledError:
                pass

    def attach_registry(self, registry) -> None:
        """
        Attach to a registry to intercept BBA updates.

        Monkey-patches registry.update_outcome_bba to record every update.
        """
        self._registry = registry

        # Save original method
        original_update = registry.update_outcome_bba

        def recording_update(
            token_id: str,
            best_bid=None,
            best_ask=None,
            bid_size=None,
            ask_size=None,
            sequence_id=None,
            source: str = "unknown",
            bid_levels=None,
            ask_levels=None,
            **kwargs,
        ):
            # Record the update
            if self._running:
                self._record_bba_update(
                    token_id=token_id,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    bid_size=bid_size,
                    ask_size=ask_size,
                    sequence_id=sequence_id,
                    source=source,
                    bid_levels=bid_levels,
                    ask_levels=ask_levels,
                )

            # Call original
            return original_update(
                token_id=token_id,
                best_bid=best_bid,
                best_ask=best_ask,
                bid_size=bid_size,
                ask_size=ask_size,
                sequence_id=sequence_id,
                source=source,
                bid_levels=bid_levels,
                ask_levels=ask_levels,
                **kwargs,
            )

        registry.update_outcome_bba = recording_update
        logger.info("BBA Recorder attached to registry")

    def record_opportunity(self, opportunity) -> None:
        """Record a detected opportunity."""
        if not self._running:
            return

        record = {
            "ts": datetime.utcnow().isoformat(),
            "mono": time.monotonic(),
            "type": "opportunity",
            "opportunity_id": opportunity.opportunity_id,
            "event_id": opportunity.event.event_id,
            "direction": opportunity.direction.value,
            "sum_of_prices": opportunity.sum_of_prices,
            "gross_edge": opportunity.gross_edge,
            "net_edge": opportunity.net_edge,
            "suggested_size": opportunity.suggested_size,
            "num_legs": opportunity.num_legs,
            "detection_latency_ms": opportunity.detection_latency_ms,
            "legs": [
                {
                    "token_id": leg.get("token_id", ""),
                    "side": leg.get("side", ""),
                    "price": leg.get("price", 0),
                    "size": leg.get("size", 0),
                }
                for leg in opportunity.legs
            ],
        }
        self._write_record(record)

    def record_rejection(
        self,
        event_id: str,
        reason: str,
        details: Optional[dict] = None,
    ) -> None:
        """Record a rejection (why an opportunity was NOT taken)."""
        if not self._running:
            return

        record = {
            "ts": datetime.utcnow().isoformat(),
            "mono": time.monotonic(),
            "type": "rejection",
            "event_id": event_id,
            "reason": reason,
        }
        if details:
            record["details"] = details
        self._write_record(record)

    def _record_bba_update(
        self,
        token_id: str,
        best_bid=None,
        best_ask=None,
        bid_size=None,
        ask_size=None,
        sequence_id=None,
        source: str = "unknown",
        bid_levels=None,
        ask_levels=None,
    ) -> None:
        """Record a BBA update."""
        # Find event_id for this token
        event_id = None
        if self._registry:
            result = self._registry.get_event_by_token(token_id)
            if result:
                event_obj, _ = result
                event_id = event_obj.event_id

        record = {
            "ts": datetime.utcnow().isoformat(),
            "mono": time.monotonic(),
            "type": "bba_update",
            "token_id": token_id,
            "event_id": event_id,
            "source": source,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "bid_size": bid_size,
            "ask_size": ask_size,
            "sequence_id": sequence_id,
        }

        # Include depth levels if available
        if bid_levels:
            record["bid_levels"] = [
                {"p": level.price, "s": level.size} for level in bid_levels
            ]
        if ask_levels:
            record["ask_levels"] = [
                {"p": level.price, "s": level.size} for level in ask_levels
            ]

        self._write_record(record)

    def _write_event_snapshot(self) -> None:
        """Write a snapshot of all tracked events and their current state."""
        if not self._registry or not self._running:
            return

        events = []
        for event in self._registry.get_all_events():
            event_data = {
                "event_id": event.event_id,
                "slug": event.slug,
                "title": event.title,
                "platform": event.platform,
                "volume_24h": event.volume_24h,
                "fee_rate_bps": event.fee_rate_bps,
                "neg_risk": event.neg_risk,
                "priority_score": event.priority_score,
                "hours_to_resolution": event.hours_to_resolution,
                "end_date": event.end_date.isoformat() if event.end_date else None,
                "outcomes": [
                    {
                        "outcome_id": o.outcome_id,
                        "token_id": o.token_id,
                        "name": o.name,
                        "status": o.status.value,
                        "best_bid": o.bba.best_bid,
                        "best_ask": o.bba.best_ask,
                        "bid_size": o.bba.bid_size,
                        "ask_size": o.bba.ask_size,
                        "source": o.bba.source,
                        "last_updated": o.bba.last_updated.isoformat(),
                    }
                    for o in event.outcomes
                ],
            }
            events.append(event_data)

        record = {
            "ts": datetime.utcnow().isoformat(),
            "type": "event_snapshot",
            "num_events": len(events),
            "events": events,
        }
        self._write_record(record)
        logger.debug(f"BBA Recorder: event snapshot written ({len(events)} events)")

    def _write_record(self, record: dict) -> None:
        """Write a record to the buffer."""
        line = json.dumps(record, default=str) + "\n"
        self._write_buffer.append(line)

        # Auto-flush if buffer gets large (>1000 records)
        if len(self._write_buffer) >= 1000:
            self._flush_buffer_sync()

    def _flush_buffer_sync(self) -> None:
        """Flush the write buffer to disk (synchronous)."""
        if not self._write_buffer or not self._file:
            return

        try:
            data = "".join(self._write_buffer)
            self._file.write(data)
            self._file.flush()

            byte_count = len(data.encode("utf-8"))
            self._bytes_written += byte_count
            self._records_written += len(self._write_buffer)
            self._write_buffer.clear()

            # Check file rotation
            if self._bytes_written >= self.max_file_size:
                self._rotate_file()
        except Exception as e:
            logger.error(f"BBA Recorder flush error: {e}")

    def _open_new_file(self) -> None:
        """Open a new recording file."""
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        ext = ".jsonl.gz" if self.compress else ".jsonl"
        self._file_path = self.output_dir / f"bba_recording_{timestamp}{ext}"

        if self.compress:
            self._file = gzip.open(self._file_path, "wt", encoding="utf-8")
        else:
            self._file = open(self._file_path, "w", encoding="utf-8")

        self._bytes_written = 0

    def _rotate_file(self) -> None:
        """Close current file and open a new one."""
        logger.info(
            f"BBA Recorder: rotating file ({self._bytes_written / 1024 / 1024:.1f} MB)"
        )
        if self._file:
            self._file.close()
        self._open_new_file()

    async def _flush_loop(self) -> None:
        """Periodically flush the write buffer."""
        while self._running:
            try:
                await asyncio.sleep(self.flush_interval)
                self._flush_buffer_sync()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"BBA Recorder flush loop error: {e}")

    async def _snapshot_loop(self) -> None:
        """Periodically write event metadata snapshots."""
        # Wait for initial data to populate
        await asyncio.sleep(30)

        while self._running:
            try:
                self._write_event_snapshot()
                await asyncio.sleep(self.snapshot_interval)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"BBA Recorder snapshot loop error: {e}")
                await asyncio.sleep(60)

    def get_stats(self) -> dict:
        """Get recorder statistics."""
        runtime = 0.0
        if self._start_time:
            runtime = (datetime.utcnow() - self._start_time).total_seconds()

        return {
            "running": self._running,
            "file": str(self._file_path) if self._file_path else None,
            "records_written": self._records_written,
            "bytes_written": self._bytes_written,
            "mb_written": round(self._bytes_written / 1024 / 1024, 2),
            "buffer_size": len(self._write_buffer),
            "runtime_seconds": round(runtime, 1),
            "records_per_second": round(self._records_written / max(runtime, 1), 1),
        }
