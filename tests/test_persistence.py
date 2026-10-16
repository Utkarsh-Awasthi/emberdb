from emberdb.engine import EmberEngine


def _engine(tmp_path, **overrides):
    cfg = {
        "databases": 4, "appendonly": True,
        "snapshot_path": str(tmp_path / "snap.json"),
        "aof_path": str(tmp_path / "test.aof.jsonl"),
        "cdc_path": str(tmp_path / "cdc.jsonl"),
    }
    cfg.update(overrides)
    return EmberEngine(cfg)


def test_snapshot_round_trip_all_types(tmp_path, conn):
    eng = _engine(tmp_path)
    try:
        eng.dispatch_from_client(conn, ["SET", "s", "hello"])
        eng.dispatch_from_client(conn, ["RPUSH", "l", "a", "b"])
        eng.dispatch_from_client(conn, ["HSET", "h", "f", "v"])
        eng.dispatch_from_client(conn, ["SADD", "st", "m1", "m2"])
        eng.dispatch_from_client(conn, ["ZADD", "z", "1", "a", "2", "b"])
        eng.dispatch_from_client(conn, ["JSON.SET", "j", "$", '{"x":1}'])
        eng.dispatch_from_client(conn, ["SET", "with_ttl", "v", "EX", "1000"])
        eng.save_snapshot_now()
    finally:
        eng.shutdown()

    reloaded = _engine(tmp_path, aof_path=str(tmp_path / "unused.aof.jsonl"))
    try:
        reloaded.load_from_disk()
        c2 = type(conn)()
        assert reloaded.dispatch_from_client(c2, ["GET", "s"]) == "hello"
        assert reloaded.dispatch_from_client(c2, ["LRANGE", "l", "0", "-1"]) == ["a", "b"]
        assert reloaded.dispatch_from_client(c2, ["HGET", "h", "f"]) == "v"
        assert reloaded.dispatch_from_client(c2, ["SMEMBERS", "st"]) == {"m1", "m2"}
        assert reloaded.dispatch_from_client(c2, ["ZSCORE", "z", "b"]) == "2"
        assert reloaded.dispatch_from_client(c2, ["JSON.GET", "j", "$.x"]) == "1"
        ttl = reloaded.dispatch_from_client(c2, ["TTL", "with_ttl"])
        assert 0 < ttl <= 1000
    finally:
        reloaded.shutdown()


def test_aof_replay_reconstructs_state(tmp_path, conn):
    eng = _engine(tmp_path)
    try:
        eng.dispatch_from_client(conn, ["SET", "a", "1"])
        eng.dispatch_from_client(conn, ["INCR", "a"])
        eng.dispatch_from_client(conn, ["RPUSH", "l", "x", "y"])
        eng.dispatch_from_client(conn, ["DEL", "l"])
        eng.dispatch_from_client(conn, ["RPUSH", "l", "z"])
        eng.aof.flush_and_sync()
    finally:
        eng.shutdown()

    replayed = _engine(tmp_path, snapshot_path=str(tmp_path / "no-such-snapshot.json"))
    try:
        replayed.load_from_disk()
        c2 = type(conn)()
        assert replayed.dispatch_from_client(c2, ["GET", "a"]) == "2"
        assert replayed.dispatch_from_client(c2, ["LRANGE", "l", "0", "-1"]) == ["z"]
    finally:
        replayed.shutdown()


def test_expire_propagates_as_absolute_pexpireat_in_aof(tmp_path, conn):
    """See persistence.py's docstring: relative-time commands must be
    rewritten to an absolute timestamp before hitting the AOF, otherwise
    replay would grant a *fresh* TTL rather than honoring the original
    expiry moment."""
    eng = _engine(tmp_path)
    try:
        eng.dispatch_from_client(conn, ["SET", "k", "v"])
        eng.dispatch_from_client(conn, ["EXPIRE", "k", "100"])
        eng.aof.flush_and_sync()
        with open(eng.aof_path) as f:
            lines = f.readlines()
        assert any("PEXPIREAT" in line for line in lines)
        assert not any('"EXPIRE"' in line for line in lines)
    finally:
        eng.shutdown()


def test_transaction_commit_writes_all_lines_atomically(tmp_path, conn):
    eng = _engine(tmp_path)
    try:
        eng.dispatch_from_client(conn, ["MULTI"])
        eng.dispatch_from_client(conn, ["SET", "a", "1"])
        eng.dispatch_from_client(conn, ["SET", "b", "2"])
        eng.dispatch_from_client(conn, ["EXEC"])
        eng.aof.flush_and_sync()
        with open(eng.aof_path) as f:
            lines = f.readlines()
        assert sum(1 for l in lines if '"a"' in l) == 1
        assert sum(1 for l in lines if '"b"' in l) == 1
    finally:
        eng.shutdown()


# -- crash durability: read the file WITHOUT calling flush_and_sync() first,
# which is exactly what a real crash / kill -9 skips --------------------------

def test_aof_write_visible_on_disk_without_explicit_flush(tmp_path, conn):
    eng = _engine(tmp_path)
    try:
        eng.dispatch_from_client(conn, ["SET", "crash_safe", "v"])
        with open(eng.aof_path) as f:  # no flush_and_sync() — simulates a crash
            assert "crash_safe" in f.read()
    finally:
        eng.shutdown()


def test_aof_survives_simulated_crash_and_replays(tmp_path, conn):
    eng = _engine(tmp_path)
    eng.dispatch_from_client(conn, ["SET", "a", "1"])
    eng.dispatch_from_client(conn, ["RPUSH", "l", "x"])
    # deliberately no shutdown()/close(): the process "dies" here
    fresh = _engine(tmp_path, snapshot_path=str(tmp_path / "none.json"))
    try:
        fresh.load_from_disk()
        c2 = type(conn)()
        assert fresh.dispatch_from_client(c2, ["GET", "a"]) == "1"
        assert fresh.dispatch_from_client(c2, ["LRANGE", "l", "0", "-1"]) == ["x"]
    finally:
        fresh.shutdown()
        eng.aof._fh.close()


def test_everysec_background_task_fsyncs(tmp_path, conn):
    import asyncio

    async def body():
        eng = _engine(tmp_path, aof_fsync="everysec")
        try:
            await eng.start_background_tasks()
            eng.dispatch_from_client(conn, ["SET", "k", "v"])
            assert eng.aof._dirty_since_fsync is True
            await asyncio.sleep(1.3)
            assert eng.aof._dirty_since_fsync is False  # the 1s task fsynced it
        finally:
            eng.shutdown()

    asyncio.run(body())
