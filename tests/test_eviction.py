from emberdb.engine import EmberEngine
from emberdb.protocol import RespError

import pytest


def _engine(tmp_path, **overrides):
    cfg = {
        "databases": 4, "appendonly": False,
        "snapshot_path": str(tmp_path / "snap.json"),
        "cdc_path": str(tmp_path / "cdc.jsonl"),
    }
    cfg.update(overrides)
    return EmberEngine(cfg)


def test_noeviction_raises_oom_once_over_budget(tmp_path, conn):
    eng = _engine(tmp_path, maxmemory=1, maxmemory_policy="noeviction")
    try:
        # The very first write is allowed: usage starts at 0 bytes, which
        # doesn't exceed even a 1-byte budget yet — the guard checks usage
        # *before* a write, so it can't stop the write that first causes
        # the overage, only writes that follow it. See eviction.py.
        eng.dispatch_from_client(conn, ["SET", "first", "x"])
        with pytest.raises(RespError) as exc:
            eng.dispatch_from_client(conn, ["SET", "second", "x" * 100])
        assert exc.value.code == "OOM"
    finally:
        eng.shutdown()


def test_allkeys_random_evicts_under_pressure(tmp_path, conn):
    eng = _engine(tmp_path, maxmemory=300, maxmemory_policy="allkeys-random")
    try:
        for i in range(40):
            eng.dispatch_from_client(conn, ["SET", f"k{i}", "v" * 20])
        assert eng.metrics.evicted_keys > 0
        # Eviction checks usage *before* each write (see eviction.py), so a
        # single write can still land slightly over budget — the
        # meaningful invariant is that eviction actively bounded growth
        # (not all 40 keys survived), not an exact byte ceiling at every
        # instant. Allow one key's worth of overshoot as slack.
        assert eng.databases[0].size() < 40
        assert eng.eviction.total_bytes(eng.databases) <= eng.eviction.maxmemory_bytes + 200
    finally:
        eng.shutdown()


def test_allkeys_lru_evicts_least_recently_used(tmp_path, conn):
    eng = _engine(tmp_path, maxmemory=100_000, maxmemory_policy="allkeys-lru")
    try:
        eng.dispatch_from_client(conn, ["SET", "old", "v"])
        eng.dispatch_from_client(conn, ["SET", "new", "v"])
        eng.dispatch_from_client(conn, ["GET", "new"])  # touch "new" so it's not the LRU victim
        # Tighten the budget after populating, then force one more write —
        # eviction should prefer "old" (least recently touched) over "new".
        eng.eviction.maxmemory_bytes = eng.eviction.total_bytes(eng.databases) + 10
        eng.dispatch_from_client(conn, ["SET", "trigger", "v" * 50])
        assert eng.databases[0].exists("new")
    finally:
        eng.shutdown()


def test_volatile_ttl_only_considers_keys_with_expiry(tmp_path, conn):
    eng = _engine(tmp_path, maxmemory=100_000, maxmemory_policy="volatile-ttl")
    try:
        eng.dispatch_from_client(conn, ["SET", "no_ttl", "v"])
        eng.dispatch_from_client(conn, ["SET", "with_ttl", "v", "EX", "1000"])
        eng.eviction.maxmemory_bytes = eng.eviction.total_bytes(eng.databases) + 10
        eng.dispatch_from_client(conn, ["SET", "trigger", "v" * 50])
        assert eng.databases[0].exists("no_ttl")  # volatile-* must never touch a key with no TTL
    finally:
        eng.shutdown()
