"""The in-memory keyspace. Every command handler reads/writes through Database's
methods, never the dicts directly, so expiry/version/size accounting stays centralized."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .memsize import estimate_size, PER_KEY_OVERHEAD


@dataclass
class KeyMeta:
    created_at: float = field(default_factory=time.time)
    last_access: float = field(default_factory=time.time)
    access_count: int = 0
    version: int = 0  # bumped on every write; read by WATCH
    size_estimate: int = PER_KEY_OVERHEAD


class Database:
    """One logical numbered database (SELECT 0..N)."""

    def __init__(self, index: int):
        self.index = index
        self.data: dict[str, Any] = {}
        self.expires: dict[str, float] = {}
        self.meta: dict[str, KeyMeta] = {}
        self.approx_bytes: int = 0
        self.write_clock = 0

    # -- expiry --------------------------------------------------------

    def is_expired(self, key: str, now: Optional[float] = None) -> bool:
        exp = self.expires.get(key)
        return exp is not None and exp <= (now if now is not None else time.time())

    def expire_if_needed(self, key: str) -> bool:
        """Lazy expiry, called on every read/write. True if it was expired and just got purged."""
        if self.is_expired(key):
            self._purge(key)
            return True
        return False

    def _purge(self, key: str) -> None:
        self.data.pop(key, None)
        self.expires.pop(key, None)
        m = self.meta.pop(key, None)
        if m is not None:
            self.approx_bytes -= m.size_estimate

    # -- core read/write -------------------------------------------------

    def get(self, key: str) -> Any:
        self.expire_if_needed(key)
        value = self.data.get(key)
        if value is not None:
            m = self.meta.get(key)
            if m:
                m.last_access = time.time()
                m.access_count += 1
        return value

    def peek(self, key: str) -> Any:
        """Like get(), but doesn't update access stats."""
        self.expire_if_needed(key)
        return self.data.get(key)

    def exists(self, key: str) -> bool:
        self.expire_if_needed(key)
        return key in self.data

    def set_value(self, key: str, value: Any, keep_ttl: bool = False) -> None:
        self.data[key] = value
        if not keep_ttl:
            self.expires.pop(key, None)
        meta = self.meta.get(key)
        if meta is None:
            meta = KeyMeta()
            self.meta[key] = meta
        else:
            self.approx_bytes -= meta.size_estimate
        meta.version += 1
        meta.last_access = time.time()
        meta.size_estimate = PER_KEY_OVERHEAD + estimate_size(value)
        self.approx_bytes += meta.size_estimate
        self.write_clock += 1

    def touch_version(self, key: str) -> None:
        """Bump version/size for an in-place mutation (LPUSH, HSET, ...) without touching TTL."""
        meta = self.meta.get(key)
        if meta is None:
            meta = KeyMeta()
            self.meta[key] = meta
        else:
            self.approx_bytes -= meta.size_estimate
        meta.version += 1
        meta.last_access = time.time()
        value = self.data.get(key)
        meta.size_estimate = PER_KEY_OVERHEAD + (estimate_size(value) if value is not None else 0)
        self.approx_bytes += meta.size_estimate
        self.write_clock += 1

    def touch_access(self, key: str) -> None:
        """Update access stats for a read not already covered by get()."""
        m = self.meta.get(key)
        if m:
            m.last_access = time.time()
            m.access_count += 1

    def delete(self, key: str) -> bool:
        existed = self.exists(key)
        if existed:
            self._purge(key)
            self.write_clock += 1
        return existed

    def version_of(self, key: str) -> int:
        """WATCH records this; EXEC re-checks it."""
        self.expire_if_needed(key)
        m = self.meta.get(key)
        return m.version if m else -1

    def rename(self, src: str, dst: str) -> None:
        from .protocol import RespError  # local import: avoids a module-load

        self.expire_if_needed(src)
        if src not in self.data:
            raise RespError("no such key")
        if dst in self.meta:
            self.approx_bytes -= self.meta[dst].size_estimate
        self.data[dst] = self.data.pop(src)
        if src in self.expires:
            self.expires[dst] = self.expires.pop(src)
        else:
            self.expires.pop(dst, None)
        self.meta[dst] = self.meta.pop(src, KeyMeta())
        self.meta[dst].version += 1
        self.write_clock += 1

    # -- TTL management ----------------------------------------------------

    def set_expire_at(self, key: str, when_epoch: float) -> bool:
        if not self.exists(key):
            return False
        self.expires[key] = when_epoch
        return True

    def persist(self, key: str) -> bool:
        if key in self.expires:
            del self.expires[key]
            return True
        return False

    def ttl_seconds(self, key: str) -> Optional[float]:
        """None = no TTL. Negative sentinels (-1/-2) are the command layer's job."""
        if not self.exists(key):
            return None
        exp = self.expires.get(key)
        if exp is None:
            return None
        return max(0.0, exp - time.time())

    # -- bulk / introspection ----------------------------------------------

    def size(self) -> int:
        return len(self.data)

    def all_keys(self) -> list[str]:
        # Snapshot the key list first: expire_if_needed can mutate self.data
        # while we iterate, which would raise "dictionary changed size".
        for k in list(self.data.keys()):
            self.expire_if_needed(k)
        return list(self.data.keys())

    def flush(self) -> None:
        self.data.clear()
        self.expires.clear()
        self.meta.clear()
        self.approx_bytes = 0
        self.write_clock += 1

    def volatile_keys(self) -> list[str]:
        return [k for k in self.expires if not self.is_expired(k)]

    def sweep_expired(self, limit: int = 200) -> list[str]:
        """Actively purge up to `limit` expired keys; returns the removed keys."""
        now = time.time()
        removed = []
        for key, exp in list(self.expires.items()):
            if len(removed) >= limit:
                break
            if exp <= now:
                self._purge(key)
                removed.append(key)
        return removed
