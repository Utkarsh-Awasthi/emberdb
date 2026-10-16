"""Command counters, hit/miss ratio, ops/sec history, slowlog."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class SlowlogEntry:
    ts: float
    duration_ms: float
    argv: list[str]


class Metrics:
    def __init__(self, slowlog_threshold_ms: float = 10.0, slowlog_capacity: int = 128):
        self.start_time = time.time()
        self.commands_processed = 0
        self.keyspace_hits = 0
        self.keyspace_misses = 0
        self.connected_clients = 0
        self.total_connections_received = 0
        self.expired_keys = 0
        self.evicted_keys = 0
        self.command_counts: dict[str, int] = {}

        self.slowlog_threshold_ms = slowlog_threshold_ms
        self.slowlog: deque[SlowlogEntry] = deque(maxlen=slowlog_capacity)

        # Rolling ops/sec history, sampled once a second by engine.py's
        # background tick — this is exactly what feeds the dashboard's
        # sparkline (see dashboard/app.js).
        self._ops_history: deque[int] = deque(maxlen=120)  # 2 minutes at 1Hz
        self._last_sample_count = 0

    def record_command(self, argv: list[str], duration_ms: float) -> None:
        self.commands_processed += 1
        name = argv[0].upper() if argv else "?"
        self.command_counts[name] = self.command_counts.get(name, 0) + 1
        if duration_ms >= self.slowlog_threshold_ms:
            self.slowlog.append(SlowlogEntry(ts=time.time(), duration_ms=duration_ms, argv=list(argv)))

    def record_hit(self) -> None:
        self.keyspace_hits += 1

    def record_miss(self) -> None:
        self.keyspace_misses += 1

    def record_connect(self) -> None:
        self.connected_clients += 1
        self.total_connections_received += 1

    def record_disconnect(self) -> None:
        self.connected_clients = max(0, self.connected_clients - 1)

    def sample_ops_per_second(self) -> int:
        """Call once a second (engine.py's tick task). Returns the ops/sec
        for the interval that just elapsed and appends it to the rolling
        history used for the dashboard sparkline."""
        delta = self.commands_processed - self._last_sample_count
        self._last_sample_count = self.commands_processed
        self._ops_history.append(delta)
        return delta

    def uptime_seconds(self) -> float:
        return time.time() - self.start_time

    def hit_rate(self) -> float:
        total = self.keyspace_hits + self.keyspace_misses
        return (self.keyspace_hits / total) if total else 0.0

    def snapshot(self) -> dict:
        return {
            "uptime_seconds": round(self.uptime_seconds(), 1),
            "commands_processed": self.commands_processed,
            "connected_clients": self.connected_clients,
            "total_connections_received": self.total_connections_received,
            "keyspace_hits": self.keyspace_hits,
            "keyspace_misses": self.keyspace_misses,
            "hit_rate": round(self.hit_rate(), 4),
            "expired_keys": self.expired_keys,
            "evicted_keys": self.evicted_keys,
            "ops_per_sec": self._ops_history[-1] if self._ops_history else 0,
            "ops_history": list(self._ops_history),
            "top_commands": sorted(self.command_counts.items(), key=lambda kv: -kv[1])[:10],
        }

    def slowlog_entries(self, count: int = 10) -> list[dict]:
        return [
            {"ts": e.ts, "duration_ms": round(e.duration_ms, 3), "argv": e.argv}
            for e in list(self.slowlog)[-count:][::-1]
        ]

    def slowlog_reset(self) -> None:
        self.slowlog.clear()
