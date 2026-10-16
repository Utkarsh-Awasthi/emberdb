import pytest

from emberdb.protocol import RespError, SimpleString


# -- basics ----------------------------------------------------------------

def test_starts_on_main(d, conn):
    assert d(conn, "BRANCH.CURRENT") == "main"
    names = [b["name"] for b in d(conn, "BRANCH.LIST")]
    assert names == ["main"]


def test_create_copies_current_data(d, conn):
    d(conn, "SET", "a", "1")
    d(conn, "SET", "b", "2")
    copied = d(conn, "BRANCH.CREATE", "feature")
    assert copied == 2
    assert d(conn, "BRANCH.CURRENT") == "main"  # create does not switch


def test_create_duplicate_name_raises(d, conn):
    d(conn, "BRANCH.CREATE", "x")
    with pytest.raises(RespError):
        d(conn, "BRANCH.CREATE", "x")


def test_create_rejects_whitespace_name(d, conn):
    with pytest.raises(RespError):
        d(conn, "BRANCH.CREATE", "bad name")


def test_switch_to_unknown_branch_raises(d, conn):
    with pytest.raises(RespError):
        d(conn, "BRANCH.SWITCH", "nope")


# -- isolation -------------------------------------------------------------

def test_writes_on_branch_do_not_touch_main(d, conn):
    d(conn, "SET", "shared", "original")
    d(conn, "BRANCH.CREATE", "feature")
    d(conn, "BRANCH.SWITCH", "feature")

    d(conn, "SET", "shared", "changed")
    d(conn, "SET", "branch_only", "yes")
    assert d(conn, "GET", "shared") == "changed"

    d(conn, "BRANCH.SWITCH", "main")
    assert d(conn, "GET", "shared") == "original"
    assert d(conn, "EXISTS", "branch_only") == 0


def test_branch_sees_fork_point_data(d, conn):
    d(conn, "SET", "pre", "v")
    d(conn, "BRANCH.CREATE", "feature")
    d(conn, "SET", "post", "v")  # written on main AFTER the fork

    d(conn, "BRANCH.SWITCH", "feature")
    assert d(conn, "GET", "pre") == "v"
    assert d(conn, "EXISTS", "post") == 0


def test_deep_copy_not_shared_reference(d, conn):
    d(conn, "RPUSH", "mylist", "a", "b")
    d(conn, "HSET", "myhash", "f", "v")
    d(conn, "BRANCH.CREATE", "feature")
    d(conn, "BRANCH.SWITCH", "feature")

    d(conn, "RPUSH", "mylist", "c")
    d(conn, "HSET", "myhash", "f2", "v2")

    d(conn, "BRANCH.SWITCH", "main")
    assert d(conn, "LRANGE", "mylist", "0", "-1") == ["a", "b"]
    assert d(conn, "HGETALL", "myhash") == {"f": "v"}


def test_create_from_explicit_branch(d, conn):
    d(conn, "SET", "k", "main-value")
    d(conn, "BRANCH.CREATE", "a")
    d(conn, "BRANCH.SWITCH", "a")
    d(conn, "SET", "k", "a-value")
    d(conn, "BRANCH.SWITCH", "main")

    d(conn, "BRANCH.CREATE", "b", "FROM", "a")
    d(conn, "BRANCH.SWITCH", "b")
    assert d(conn, "GET", "k") == "a-value"


def test_branch_isolation_across_multiple_dbs(d, conn):
    d(conn, "SET", "db0key", "v")
    conn.selected_db = 1
    d(conn, "SET", "db1key", "v")
    conn.selected_db = 0

    d(conn, "BRANCH.CREATE", "feature")
    d(conn, "BRANCH.SWITCH", "feature")
    conn.selected_db = 1
    assert d(conn, "GET", "db1key") == "v"
    d(conn, "SET", "db1key", "changed")
    conn.selected_db = 0

    d(conn, "BRANCH.SWITCH", "main")
    conn.selected_db = 1
    assert d(conn, "GET", "db1key") == "v"
    conn.selected_db = 0


# -- drop ------------------------------------------------------------------

def test_drop_branch(d, conn):
    d(conn, "BRANCH.CREATE", "temp")
    d(conn, "BRANCH.DROP", "temp")
    assert "temp" not in [b["name"] for b in d(conn, "BRANCH.LIST")]


def test_cannot_drop_main(d, conn):
    with pytest.raises(RespError):
        d(conn, "BRANCH.DROP", "main")


def test_cannot_drop_checked_out_branch(d, conn):
    d(conn, "BRANCH.CREATE", "temp")
    d(conn, "BRANCH.SWITCH", "temp")
    with pytest.raises(RespError):
        d(conn, "BRANCH.DROP", "temp")


# -- diff ------------------------------------------------------------------

def test_diff_reports_added_modified_deleted(d, conn):
    d(conn, "SET", "keep", "v")
    d(conn, "SET", "mod", "before")
    d(conn, "SET", "gone", "v")
    d(conn, "BRANCH.CREATE", "feature")
    d(conn, "BRANCH.SWITCH", "feature")

    d(conn, "SET", "added", "new")
    d(conn, "SET", "mod", "after")
    d(conn, "DEL", "gone")

    changes = {c["key"]: c["change"] for c in d(conn, "BRANCH.DIFF")}
    assert changes["added"] == "added"
    assert changes["mod"] == "modified"
    assert changes["gone"] == "deleted"
    assert "keep" not in changes


def test_diff_on_main_is_empty(d, conn):
    d(conn, "SET", "a", "1")
    assert d(conn, "BRANCH.DIFF") == []


# -- merge -----------------------------------------------------------------

def test_merge_applies_branch_changes(d, conn):
    d(conn, "SET", "base", "v")
    d(conn, "BRANCH.CREATE", "feature")
    d(conn, "BRANCH.SWITCH", "feature")
    d(conn, "SET", "from_branch", "yes")
    d(conn, "SET", "base", "updated")
    d(conn, "BRANCH.SWITCH", "main")

    assert d(conn, "EXISTS", "from_branch") == 0
    result = d(conn, "BRANCH.MERGE", "feature")
    assert result["merged"] is True
    assert result["applied"] == 2
    assert d(conn, "GET", "from_branch") == "yes"
    assert d(conn, "GET", "base") == "updated"


def test_merge_propagates_deletes(d, conn):
    d(conn, "SET", "doomed", "v")
    d(conn, "BRANCH.CREATE", "feature")
    d(conn, "BRANCH.SWITCH", "feature")
    d(conn, "DEL", "doomed")
    d(conn, "BRANCH.SWITCH", "main")

    d(conn, "BRANCH.MERGE", "feature")
    assert d(conn, "EXISTS", "doomed") == 0


def test_merge_leaves_untouched_target_keys_alone(d, conn):
    d(conn, "SET", "base", "v")
    d(conn, "BRANCH.CREATE", "feature")
    d(conn, "BRANCH.SWITCH", "feature")
    d(conn, "SET", "branch_key", "b")
    d(conn, "BRANCH.SWITCH", "main")
    d(conn, "SET", "main_only", "m")  # target-only change, untouched on branch

    d(conn, "BRANCH.MERGE", "feature")
    assert d(conn, "GET", "main_only") == "m"
    assert d(conn, "GET", "branch_key") == "b"


def test_merge_conflict_fails_by_default_and_changes_nothing(d, conn):
    d(conn, "SET", "contested", "original")
    d(conn, "BRANCH.CREATE", "feature")
    d(conn, "BRANCH.SWITCH", "feature")
    d(conn, "SET", "contested", "branch-version")
    d(conn, "SET", "safe", "branch-only")
    d(conn, "BRANCH.SWITCH", "main")
    d(conn, "SET", "contested", "main-version")

    result = d(conn, "BRANCH.MERGE", "feature")
    assert result["merged"] is False
    assert any(c["key"] == "contested" for c in result["conflicts"])
    # nothing at all should have been applied
    assert d(conn, "GET", "contested") == "main-version"
    assert d(conn, "EXISTS", "safe") == 0


def test_merge_strategy_theirs(d, conn):
    d(conn, "SET", "contested", "original")
    d(conn, "BRANCH.CREATE", "feature")
    d(conn, "BRANCH.SWITCH", "feature")
    d(conn, "SET", "contested", "branch-version")
    d(conn, "BRANCH.SWITCH", "main")
    d(conn, "SET", "contested", "main-version")

    result = d(conn, "BRANCH.MERGE", "feature", "STRATEGY", "theirs")
    assert result["merged"] is True
    assert d(conn, "GET", "contested") == "branch-version"


def test_merge_strategy_ours(d, conn):
    d(conn, "SET", "contested", "original")
    d(conn, "BRANCH.CREATE", "feature")
    d(conn, "BRANCH.SWITCH", "feature")
    d(conn, "SET", "contested", "branch-version")
    d(conn, "SET", "uncontested", "branch-only")
    d(conn, "BRANCH.SWITCH", "main")
    d(conn, "SET", "contested", "main-version")

    result = d(conn, "BRANCH.MERGE", "feature", "STRATEGY", "ours")
    assert result["merged"] is True
    assert d(conn, "GET", "contested") == "main-version"   # target wins
    assert d(conn, "GET", "uncontested") == "branch-only"  # non-conflicting still applied


def test_merge_into_explicit_target(d, conn):
    d(conn, "SET", "base", "v")
    d(conn, "BRANCH.CREATE", "source")
    d(conn, "BRANCH.CREATE", "dest")
    d(conn, "BRANCH.SWITCH", "source")
    d(conn, "SET", "new", "v")
    d(conn, "BRANCH.SWITCH", "main")

    d(conn, "BRANCH.MERGE", "source", "INTO", "dest")
    assert d(conn, "EXISTS", "new") == 0  # main untouched
    d(conn, "BRANCH.SWITCH", "dest")
    assert d(conn, "GET", "new") == "v"


def test_merge_into_self_raises(d, conn):
    d(conn, "BRANCH.CREATE", "feature")
    with pytest.raises(RespError):
        d(conn, "BRANCH.MERGE", "main")


def test_unknown_strategy_raises(d, conn):
    d(conn, "BRANCH.CREATE", "feature")
    with pytest.raises(RespError):
        d(conn, "BRANCH.MERGE", "feature", "STRATEGY", "bogus")


def test_merge_is_not_replayed_twice(d, conn):
    d(conn, "BRANCH.CREATE", "feature")
    d(conn, "BRANCH.SWITCH", "feature")
    d(conn, "SET", "k", "v")
    d(conn, "BRANCH.SWITCH", "main")

    first = d(conn, "BRANCH.MERGE", "feature")
    assert first["applied"] == 1
    d(conn, "DEL", "k")
    second = d(conn, "BRANCH.MERGE", "feature")
    assert second["applied"] == 0  # re-baselined, nothing new to carry over


# -- interaction with other subsystems --------------------------------------

def test_ttl_survives_branch_and_merge(d, conn):
    d(conn, "SET", "k", "v", "EX", "500")
    d(conn, "BRANCH.CREATE", "feature")
    d(conn, "BRANCH.SWITCH", "feature")
    ttl = d(conn, "TTL", "k")
    assert 0 < ttl <= 500


def test_indexes_rebuilt_after_switch(d, conn):
    d(conn, "HSET", "user:1", "email", "a@example.com")
    d(conn, "CREATE", "INDEX", "idx", "ON", "user:*", "FIELD", "email")
    assert d(conn, "FIND", "idx", "WHERE", "email", "=", "a@example.com") == ["user:1"]

    d(conn, "BRANCH.CREATE", "feature")
    d(conn, "BRANCH.SWITCH", "feature")
    d(conn, "HSET", "user:2", "email", "b@example.com")
    assert d(conn, "FIND", "idx", "WHERE", "email", "=", "b@example.com") == ["user:2"]

    d(conn, "BRANCH.SWITCH", "main")
    # user:2 only exists on the branch, so the index must no longer report it
    assert d(conn, "FIND", "idx", "WHERE", "email", "=", "b@example.com") == []
    assert d(conn, "FIND", "idx", "WHERE", "email", "=", "a@example.com") == ["user:1"]


def test_indexes_rebuilt_after_merge(d, conn):
    d(conn, "HSET", "user:1", "email", "a@example.com")
    d(conn, "CREATE", "INDEX", "idx", "ON", "user:*", "FIELD", "email")
    d(conn, "BRANCH.CREATE", "feature")
    d(conn, "BRANCH.SWITCH", "feature")
    d(conn, "HSET", "user:2", "email", "b@example.com")
    d(conn, "BRANCH.SWITCH", "main")

    d(conn, "BRANCH.MERGE", "feature")
    assert d(conn, "FIND", "idx", "WHERE", "email", "=", "b@example.com") == ["user:2"]


def test_json_and_zset_branch_independently(d, conn):
    d(conn, "JSON.SET", "doc", "$", '{"n": 1}')
    d(conn, "ZADD", "z", "1", "a")
    d(conn, "BRANCH.CREATE", "feature")
    d(conn, "BRANCH.SWITCH", "feature")
    d(conn, "JSON.SET", "doc", "$.n", "99")
    d(conn, "ZADD", "z", "5", "b")

    d(conn, "BRANCH.SWITCH", "main")
    assert d(conn, "JSON.GET", "doc", "$.n") == "1"
    assert d(conn, "ZRANGE", "z", "0", "-1") == ["a"]


def test_branch_list_reports_key_counts(d, conn):
    d(conn, "SET", "a", "1")
    d(conn, "BRANCH.CREATE", "feature")
    d(conn, "BRANCH.SWITCH", "feature")
    d(conn, "SET", "b", "2")

    listing = {b["name"]: b for b in d(conn, "BRANCH.LIST")}
    assert listing["feature"]["keys"] == 2
    assert listing["main"]["keys"] == 1
    assert listing["feature"]["current"] is True
    assert listing["main"]["current"] is False


# -- durability semantics: branches are ephemeral ---------------------------

def test_branch_writes_do_not_reach_the_aof(engine, d, conn):
    d(conn, "SET", "on_main", "v")
    d(conn, "BRANCH.CREATE", "feature")
    d(conn, "BRANCH.SWITCH", "feature")
    d(conn, "SET", "on_branch", "v")
    engine.aof.flush_and_sync()

    with open(engine.aof_path) as f:
        contents = f.read()
    assert "on_main" in contents
    assert "on_branch" not in contents


def test_snapshot_always_captures_main_not_the_live_branch(engine, d, conn):
    import json
    d(conn, "SET", "main_key", "v")
    d(conn, "BRANCH.CREATE", "feature")
    d(conn, "BRANCH.SWITCH", "feature")
    d(conn, "SET", "branch_key", "v")

    engine.save_snapshot_now()
    with open(engine.snapshot_path) as f:
        snap = json.load(f)
    keys = []
    for db in snap.get("databases", []):
        keys += list(db.get("keys", {}).keys())
    assert "main_key" in keys
    assert "branch_key" not in keys


def test_branch_writes_are_not_replicated(engine, d, conn):
    class FakeWriter:
        def __init__(self):
            self.chunks = []

        def write(self, data):
            self.chunks.append(data)

    class FakeReplica:
        def __init__(self):
            self.writer = FakeWriter()
            self.addr = "127.0.0.1:0"

    replica = FakeReplica()
    engine.replication.add_replica(replica)
    try:
        d(conn, "SET", "on_main", "v")
        d(conn, "BRANCH.CREATE", "feature")
        d(conn, "BRANCH.SWITCH", "feature")
        d(conn, "SET", "on_branch", "v")
        sent = b"".join(replica.writer.chunks)
        assert b"on_main" in sent
        assert b"on_branch" not in sent
    finally:
        engine.replication.remove_replica(replica)


def _restart(engine):
    """Shut down and bring up a fresh engine on the same files (AOF replay only)."""
    from emberdb.engine import EmberEngine
    engine.aof.flush_and_sync()
    cfg = dict(engine.config)
    engine.shutdown()
    fresh = EmberEngine(cfg)
    fresh.load_from_disk()
    return fresh


def test_merge_into_main_survives_restart_via_aof(engine, d, conn):
    d(conn, "SET", "base", "v")
    d(conn, "BRANCH.CREATE", "feature")
    d(conn, "BRANCH.SWITCH", "feature")
    d(conn, "SET", "merged_key", "from-branch")
    d(conn, "DEL", "base")
    d(conn, "BRANCH.SWITCH", "main")
    d(conn, "BRANCH.MERGE", "feature")

    fresh = _restart(engine)
    try:
        c2 = type(conn)()
        assert fresh.dispatch_from_client(c2, ["GET", "merged_key"]) == "from-branch"
        assert fresh.dispatch_from_client(c2, ["EXISTS", "base"]) == 0
    finally:
        fresh.shutdown()


def test_merge_of_complex_types_survives_restart(engine, d, conn):
    d(conn, "BRANCH.CREATE", "feature")
    d(conn, "BRANCH.SWITCH", "feature")
    d(conn, "HSET", "h", "f1", "v1", "f2", "v2")
    d(conn, "ZADD", "z", "1", "a", "2", "b")
    d(conn, "RPUSH", "l", "x", "y")
    d(conn, "SADD", "s", "m1", "m2")
    d(conn, "JSON.SET", "j", "$", '{"n": 5}')
    d(conn, "SET", "ttl_key", "v", "EX", "500")
    d(conn, "BRANCH.SWITCH", "main")
    d(conn, "BRANCH.MERGE", "feature")

    fresh = _restart(engine)
    try:
        c2 = type(conn)()
        D = lambda *a: fresh.dispatch_from_client(c2, list(a))
        assert D("HGETALL", "h") == {"f1": "v1", "f2": "v2"}
        assert D("ZRANGE", "z", "0", "-1", "WITHSCORES") == ["a", "1", "b", "2"]
        assert D("LRANGE", "l", "0", "-1") == ["x", "y"]
        assert D("SMEMBERS", "s") == {"m1", "m2"}
        assert D("JSON.GET", "j", "$.n") == "5"
        assert 0 < D("TTL", "ttl_key") <= 500
    finally:
        fresh.shutdown()


def test_branch_commands_never_logged_to_aof(engine, d, conn):
    d(conn, "SET", "a", "1")
    d(conn, "BRANCH.CREATE", "f")
    d(conn, "BRANCH.SWITCH", "f")
    d(conn, "BRANCH.SWITCH", "main")
    d(conn, "BRANCH.MERGE", "f")
    d(conn, "BRANCH.DROP", "f")
    engine.aof.flush_and_sync()
    with open(engine.aof_path) as f:
        assert "BRANCH." not in f.read()


def test_writes_after_branch_workflow_replay_into_main(engine, d, conn):
    """Replay must not end up on a branch and misroute later main writes."""
    d(conn, "BRANCH.CREATE", "f")
    d(conn, "BRANCH.SWITCH", "f")
    d(conn, "SET", "branch_only", "x")
    d(conn, "BRANCH.SWITCH", "main")
    d(conn, "SET", "later_main_write", "y")

    fresh = _restart(engine)
    try:
        c2 = type(conn)()
        assert fresh.branches.current == "main"
        assert fresh.dispatch_from_client(c2, ["GET", "later_main_write"]) == "y"
        assert fresh.dispatch_from_client(c2, ["EXISTS", "branch_only"]) == 0
    finally:
        fresh.shutdown()


def test_merge_into_main_is_replicated(engine, d, conn):
    class FakeWriter:
        def __init__(self):
            self.chunks = []

        def write(self, data):
            self.chunks.append(data)

    class FakeReplica:
        def __init__(self):
            self.writer = FakeWriter()
            self.addr = "127.0.0.1:0"

    d(conn, "BRANCH.CREATE", "f")
    d(conn, "BRANCH.SWITCH", "f")
    d(conn, "SET", "merged_key", "v")
    d(conn, "BRANCH.SWITCH", "main")

    replica = FakeReplica()
    engine.replication.add_replica(replica)
    try:
        d(conn, "BRANCH.MERGE", "f")
        sent = b"".join(replica.writer.chunks)
        assert b"merged_key" in sent
        assert b"BRANCH." not in sent
    finally:
        engine.replication.remove_replica(replica)


def test_merge_into_non_main_branch_is_not_persisted(engine, d, conn):
    d(conn, "BRANCH.CREATE", "src")
    d(conn, "BRANCH.CREATE", "dst")
    d(conn, "BRANCH.SWITCH", "src")
    d(conn, "SET", "only_on_branches", "v")
    d(conn, "BRANCH.SWITCH", "main")
    d(conn, "BRANCH.MERGE", "src", "INTO", "dst")
    engine.aof.flush_and_sync()
    with open(engine.aof_path) as f:
        assert "only_on_branches" not in f.read()
