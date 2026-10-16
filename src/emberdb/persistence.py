"""JSON snapshot (save/load) and the append-only file (write/replay)."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

from .database import Database
from .valuetypes import JSONValue, VectorValue, ZSet

logger = logging.getLogger("emberdb.persistence")

SNAPSHOT_FORMAT_VERSION = 1


# -- snapshot (RDB-equivalent) --

def _serialize_value(value) -> dict:
    if isinstance(value, JSONValue):
        return {"type": "json", "value": value.doc}
    if isinstance(value, VectorValue):
        return {"type": "vector", "value": {
            "values": value.values, "namespace": value.namespace, "attrs": value.attrs,
        }}
    if isinstance(value, ZSet):
        return {"type": "zset", "value": [[m, s] for m, s in value.items_ascending()]}
    if isinstance(value, str):
        return {"type": "string", "value": value}
    if isinstance(value, deque):
        return {"type": "list", "value": list(value)}
    if isinstance(value, dict):
        return {"type": "hash", "value": value}
    if isinstance(value, set):
        return {"type": "set", "value": list(value)}
    raise ValueError(f"don't know how to snapshot value of type {type(value)!r}")


def _deserialize_value(entry: dict):
    kind = entry["type"]
    raw = entry["value"]
    if kind == "json":
        return JSONValue(raw)
    if kind == "vector":
        return VectorValue(raw["values"], raw["namespace"], raw.get("attrs") or {})
    if kind == "zset":
        zs = ZSet()
        for member, score in raw:
            zs.add(member, score)
        return zs
    if kind == "string":
        return raw
    if kind == "list":
        return deque(raw)
    if kind == "hash":
        return raw
    if kind == "set":
        return set(raw)
    raise ValueError(f"unknown snapshot value type {kind!r}")


def save_snapshot(path: str, databases: list[Database]) -> int:
    """Write a full snapshot atomically (temp file + rename). Returns keys written."""
    payload = {
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "saved_at": time.time(),
        "databases": [],
    }
    key_count = 0
    for db in databases:
        keys_out = {}
        for key in db.all_keys():  # all_keys() already drops lazily-expired keys
            value = db.data.get(key)
            if value is None:
                continue
            entry = _serialize_value(value)
            expire_at = db.expires.get(key)
            if expire_at is not None:
                entry["expire_at"] = expire_at
            keys_out[key] = entry
            key_count += 1
        payload["databases"].append({"index": db.index, "keys": keys_out})

    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".snapshot-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)  # atomic on POSIX
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    logger.info("snapshot saved: %d keys -> %s", key_count, path)
    return key_count


def load_snapshot(path: str, databases: list[Database]) -> int:
    """Load a snapshot into `databases` (expected to already be freshly
    created / empty). Returns the number of keys loaded, or 0 if there was
    no snapshot file to load (this is a normal, expected case on first
    boot — not an error)."""
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    by_index = {db.index: db for db in databases}
    key_count = 0
    for db_payload in payload.get("databases", []):
        db = by_index.get(db_payload["index"])
        if db is None:
            continue
        for key, entry in db_payload["keys"].items():
            value = _deserialize_value(entry)
            db.set_value(key, value)
            if "expire_at" in entry:
                db.set_expire_at(key, entry["expire_at"])
            key_count += 1
    logger.info("snapshot loaded: %d keys <- %s", key_count, path)
    return key_count


# -- append-only log (AOF-equivalent) --
# One JSON array per line: [db_index, "CMD", "arg1", ...] — a JSON-lines
# stream so a partially-written last line (crash mid-write) can be detected
# and discarded on replay without corrupting earlier entries.

@dataclass
class AOFEntry:
    db_index: int
    argv: list[str]


class AOFWriter:
    def __init__(self, path: str, fsync_policy: str = "everysec"):
        self.path = path
        self.fsync_policy = fsync_policy  # "always" | "everysec" | "no"
        directory = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(directory, exist_ok=True)
        self._fh = open(path, "a", encoding="utf-8")
        self._dirty_since_fsync = False
        self._batch: Optional[list[str]] = None  # transaction buffering, see below

    def log(self, db_index: int, argv: list[str]) -> None:
        line = json.dumps([db_index, *argv], ensure_ascii=False)
        if self._batch is not None:
            self._batch.append(line)
            return
        self._write_line(line)

    def _write_line(self, line: str) -> None:
        self._fh.write(line)
        self._fh.write("\n")
        # Hand the bytes to the OS immediately (like Redis's write() per loop),
        # so a process crash loses nothing. fsync timing is what the policy
        # controls: always = every write, everysec = engine's 1s timer, no = OS.
        self._fh.flush()
        self._dirty_since_fsync = True
        if self.fsync_policy == "always":
            self.flush_and_sync()

    # -- transaction support: buffer lines during EXEC, only write on commit --

    def begin_batch(self) -> None:
        self._batch = []

    def commit_batch(self) -> None:
        if self._batch is None:
            return
        lines, self._batch = self._batch, None
        for line in lines:
            self._write_line(line)

    def discard_batch(self) -> None:
        self._batch = None

    def flush_and_sync(self) -> None:
        if not self._dirty_since_fsync:
            return
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._dirty_since_fsync = False

    def close(self) -> None:
        try:
            self.flush_and_sync()
        finally:
            self._fh.close()

    def size_bytes(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0


def replay_aof(path: str, dispatch: Callable[[int, list[str]], None]) -> int:
    """Replay every AOF command through `dispatch(db_index, argv)` (same path live
    traffic uses). Returns commands replayed; a truncated final line is skipped."""
    if not os.path.exists(path):
        return 0
    replayed = 0
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("AOF line %d is truncated/corrupt, skipping rest of file", lineno)
                break
            db_index, *argv = record
            try:
                dispatch(db_index, [str(a) for a in argv])
                replayed += 1
            except Exception:  # noqa: BLE001
                logger.exception("AOF replay failed at line %d: %r", lineno, record)
    logger.info("AOF replay complete: %d commands from %s", replayed, path)
    return replayed
