"""Background active-expiration sweep (passive/lazy expiry lives in database.py)."""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from .database import Database

logger = logging.getLogger("emberdb.expiry")

OnExpiredCallback = Callable[[int, str], None]


class ActiveExpirer:
    def __init__(
        self,
        databases: list[Database],
        on_expired: Optional[OnExpiredCallback] = None,
        interval_seconds: float = 0.1,
        sample_limit: int = 200,
    ):
        self.databases = databases
        self.on_expired = on_expired
        self.interval_seconds = interval_seconds
        self.sample_limit = sample_limit
        self._task: Optional[asyncio.Task] = None
        self.cycles_run = 0
        self.total_expired = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="active-expire-cycle")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        try:
            while True:
                self.run_once()
                await asyncio.sleep(self.interval_seconds)
        except asyncio.CancelledError:
            pass

    def run_once(self) -> int:
        """Sweep every database once. Returns how many keys were purged.
        Exposed as a plain sync method (not just the async loop) so tests
        can call it deterministically without sleeping."""
        expired_count = 0
        for db in self.databases:
            removed = db.sweep_expired(limit=self.sample_limit)
            for key in removed:
                expired_count += 1
                if self.on_expired is not None:
                    try:
                        self.on_expired(db.index, key)
                    except Exception:  # noqa: BLE001 - a bad hook must not kill the cycle
                        logger.exception("on_expired callback failed for db=%s key=%s", db.index, key)
        self.cycles_run += 1
        self.total_expired += expired_count
        return expired_count
