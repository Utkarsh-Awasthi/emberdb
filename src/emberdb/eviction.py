"""maxmemory enforcement: the 8 Redis eviction policies."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .database import Database
from .protocol import RespError


class EvictionPolicy(str, Enum):
    NOEVICTION = "noeviction"
    ALLKEYS_LRU = "allkeys-lru"
    VOLATILE_LRU = "volatile-lru"
    ALLKEYS_LFU = "allkeys-lfu"
    VOLATILE_LFU = "volatile-lfu"
    ALLKEYS_RANDOM = "allkeys-random"
    VOLATILE_RANDOM = "volatile-random"
    VOLATILE_TTL = "volatile-ttl"


@dataclass
class EvictionStats:
    evicted_total: int = 0
    oom_denials: int = 0


class EvictionManager:
    def __init__(self, maxmemory_bytes: int = 0, policy: str = "noeviction", sample_size: int = 8):
        self.maxmemory_bytes = maxmemory_bytes
        self.policy = EvictionPolicy(policy)
        self.sample_size = sample_size  # candidates sampled per eviction round, like Redis's own default
        self.stats = EvictionStats()

    def total_bytes(self, databases: list[Database]) -> int:
        return sum(db.approx_bytes for db in databases)

    def enforce(self, databases: list[Database], on_evicted=None) -> None:
        """Call before a write. Raises OOM if over budget and can't free enough; else evicts."""
        if self.maxmemory_bytes <= 0:
            return  # unlimited
        guard = 0
        while self.total_bytes(databases) > self.maxmemory_bytes:
            guard += 1
            if guard > 10_000:
                break  # safety valve: e.g. a single key bigger than maxmemory
            if self.policy == EvictionPolicy.NOEVICTION:
                self.stats.oom_denials += 1
                raise RespError(
                    "command not allowed when used memory > 'maxmemory'",
                    code="OOM",
                )
            victim = self._pick_victim(databases)
            if victim is None:  # nothing eligible (e.g. volatile-* with no TTL'd keys)
                self.stats.oom_denials += 1
                raise RespError(
                    "command not allowed when used memory > 'maxmemory'",
                    code="OOM",
                )
            db_index, key = victim
            databases[db_index].delete(key)
            self.stats.evicted_total += 1
            if on_evicted is not None:
                on_evicted(db_index, key)

    # -- candidate selection -------------------------------------------------

    def _candidate_pool(self, db: Database) -> list[str]:
        if self.policy in (EvictionPolicy.VOLATILE_LRU, EvictionPolicy.VOLATILE_LFU,
                            EvictionPolicy.VOLATILE_RANDOM, EvictionPolicy.VOLATILE_TTL):
            return db.volatile_keys()
        return list(db.data.keys())

    def _pick_victim(self, databases: list[Database]) -> Optional[tuple[int, str]]:
        # samples a few random candidates rather than ranking the whole keyspace, like Redis
        pools = [(db.index, db) for db in databases if self._candidate_pool(db)]
        if not pools:
            return None
        db_index, db = random.choice(pools)
        pool = self._candidate_pool(db)
        sample = random.sample(pool, min(self.sample_size, len(pool)))

        if self.policy in (EvictionPolicy.ALLKEYS_RANDOM, EvictionPolicy.VOLATILE_RANDOM):
            return db_index, random.choice(sample)

        if self.policy in (EvictionPolicy.ALLKEYS_LRU, EvictionPolicy.VOLATILE_LRU):
            best = min(sample, key=lambda k: db.meta[k].last_access if k in db.meta else 0.0)
            return db_index, best

        if self.policy in (EvictionPolicy.ALLKEYS_LFU, EvictionPolicy.VOLATILE_LFU):
            best = min(sample, key=lambda k: db.meta[k].access_count if k in db.meta else 0)
            return db_index, best

        if self.policy == EvictionPolicy.VOLATILE_TTL:
            best = min(sample, key=lambda k: db.expires.get(k, float("inf")))
            return db_index, best

        return db_index, sample[0]

    # -- introspection used by INFO / CONFIG -------------------------------

    def describe(self) -> dict:
        return {
            "maxmemory": self.maxmemory_bytes,
            "maxmemory_policy": self.policy.value,
            "evicted_keys": self.stats.evicted_total,
            "oom_denials": self.stats.oom_denials,
        }
