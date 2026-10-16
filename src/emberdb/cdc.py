"""Change data capture: an offset-addressable event log (ring buffer + durable file)."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from collections import deque
from typing import Callable, Optional


@dataclass
class ChangeEvent:
    offset: int
    ts: float
    db: int
    event_type: str  # "write" | "expired" | "evicted" | "flush"
    command: str
    key: Optional[str]
    args: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "offset": self.offset, "ts": self.ts, "db": self.db,
            "type": self.event_type, "cmd": self.command,
            "key": self.key, "args": self.args,
        }

    @staticmethod
    def from_dict(d: dict) -> "ChangeEvent":
        return ChangeEvent(
            offset=d["offset"], ts=d["ts"], db=d["db"], event_type=d["type"],
            command=d["cmd"], key=d.get("key"), args=d.get("args", []),
        )


TailCallback = Callable[[ChangeEvent], None]


class ChangeLog:
    def __init__(self, path: Optional[str] = None, ring_capacity: int = 5000):
        self.path = path
        self._ring: deque[ChangeEvent] = deque(maxlen=ring_capacity)
        self._next_offset = 1
        self._subscribers: set[TailCallback] = set()
        self._fh = None
        if path:
            directory = os.path.dirname(os.path.abspath(path)) or "."
            os.makedirs(directory, exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8")
            self._next_offset = self._recover_next_offset()

    def _recover_next_offset(self) -> int:
        """On restart, the next offset must continue from the highest one
        ever written, not reset to 1 — otherwise a consumer's saved offset
        from before the restart could collide with a brand-new event."""
        last = 0
        if self.path and os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        last = json.loads(line)["offset"]
                    except (json.JSONDecodeError, KeyError):
                        continue
        return last + 1

    def append(self, db: int, event_type: str, command: str,
               key: Optional[str], args: Optional[list[str]] = None) -> ChangeEvent:
        event = ChangeEvent(
            offset=self._next_offset, ts=time.time(), db=db,
            event_type=event_type, command=command, key=key, args=args or [],
        )
        self._next_offset += 1
        self._ring.append(event)
        if self._fh is not None:
            self._fh.write(json.dumps(event.to_dict(), ensure_ascii=False))
            self._fh.write("\n")
            self._fh.flush()  # CDC is an audit/integration trail; favor durability over batching
        for cb in list(self._subscribers):
            try:
                cb(event)
            except Exception:
                # A misbehaving subscriber must never block or crash the
                # write path that produced this event in the first place.
                pass
        return event

    def since(self, offset: int, limit: int = 1000) -> list[ChangeEvent]:
        """All events with event.offset > `offset`, oldest first, capped at
        `limit`. Serves from the in-memory ring when possible (the common
        case: a consumer that's basically caught up); falls back to
        scanning the file on disk for older offsets."""
        if self._ring and offset >= self._ring[0].offset - 1:
            return [e for e in self._ring if e.offset > offset][:limit]
        # Cold path: the requested offset is older than anything still in
        # the ring buffer, so replay from the file.
        results = []
        if self.path and os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d["offset"] > offset:
                        results.append(ChangeEvent.from_dict(d))
                        if len(results) >= limit:
                            break
        return results

    def tail_subscribe(self, callback: TailCallback) -> None:
        self._subscribers.add(callback)

    def tail_unsubscribe(self, callback: TailCallback) -> None:
        self._subscribers.discard(callback)

    @property
    def current_offset(self) -> int:
        return self._next_offset - 1

    def status(self) -> dict:
        return {
            "current_offset": self.current_offset,
            "events_in_memory": len(self._ring),
            "live_subscribers": len(self._subscribers),
            "file": self.path,
        }

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
