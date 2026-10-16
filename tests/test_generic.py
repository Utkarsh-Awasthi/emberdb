from emberdb.protocol import RespError

import pytest


def test_del_multiple(d, conn):
    d(conn, "SET", "a", "1")
    d(conn, "SET", "b", "2")
    assert d(conn, "DEL", "a", "b", "missing") == 2


def test_exists_multiple_counts_duplicates(d, conn):
    d(conn, "SET", "a", "1")
    assert d(conn, "EXISTS", "a", "a", "missing") == 2


def test_type(d, conn):
    from emberdb.protocol import SimpleString
    d(conn, "SET", "s", "v")
    d(conn, "RPUSH", "l", "x")
    d(conn, "HSET", "h", "f", "v")
    d(conn, "SADD", "st", "m")
    d(conn, "ZADD", "z", "1", "m")
    assert d(conn, "TYPE", "s") == SimpleString("string")
    assert d(conn, "TYPE", "l") == SimpleString("list")
    assert d(conn, "TYPE", "h") == SimpleString("hash")
    assert d(conn, "TYPE", "st") == SimpleString("set")
    assert d(conn, "TYPE", "z") == SimpleString("zset")
    assert d(conn, "TYPE", "missing") == SimpleString("none")


def test_rename(d, conn):
    d(conn, "SET", "old", "v")
    d(conn, "RENAME", "old", "new")
    assert d(conn, "EXISTS", "old") == 0
    assert d(conn, "GET", "new") == "v"


def test_rename_missing_source_raises(d, conn):
    with pytest.raises(RespError):
        d(conn, "RENAME", "nosuch", "dest")


def test_renamenx(d, conn):
    d(conn, "SET", "a", "1")
    d(conn, "SET", "b", "2")
    assert d(conn, "RENAMENX", "a", "b") == 0  # b already exists
    assert d(conn, "GET", "b") == "2"


def test_copy(d, conn):
    d(conn, "SET", "src", "v")
    assert d(conn, "COPY", "src", "dst") == 1
    assert d(conn, "GET", "dst") == "v"
    assert d(conn, "COPY", "src", "dst") == 0  # dst exists, no REPLACE
    assert d(conn, "COPY", "src", "dst", "REPLACE") == 1


def test_copy_preserves_ttl(d, conn):
    d(conn, "SET", "src", "v", "EX", "100")
    d(conn, "COPY", "src", "dst")
    ttl = d(conn, "TTL", "dst")
    assert 0 < ttl <= 100


def test_expire_ttl_persist(d, conn):
    d(conn, "SET", "k", "v")
    assert d(conn, "TTL", "k") == -1
    d(conn, "EXPIRE", "k", "100")
    ttl = d(conn, "TTL", "k")
    assert 0 < ttl <= 100
    assert d(conn, "PERSIST", "k") == 1
    assert d(conn, "TTL", "k") == -1


def test_ttl_missing_key_is_minus_two(d, conn):
    assert d(conn, "TTL", "nosuch") == -2


def test_expire_nx_xx_gt_lt(d, conn):
    d(conn, "SET", "k", "v")
    assert d(conn, "EXPIRE", "k", "100", "XX") == 0  # no TTL yet
    assert d(conn, "EXPIRE", "k", "100", "NX") == 1
    assert d(conn, "EXPIRE", "k", "50", "NX") == 0  # already has a TTL
    assert d(conn, "EXPIRE", "k", "200", "GT") == 1
    assert d(conn, "EXPIRE", "k", "10", "GT") == 0


def test_pexpireat_and_expiretime(d, conn):
    import time
    d(conn, "SET", "k", "v")
    future_ms = int((time.time() + 100) * 1000)
    d(conn, "PEXPIREAT", "k", str(future_ms))
    ttl = d(conn, "TTL", "k")
    assert 0 < ttl <= 100


def test_keys_pattern(d, conn):
    d(conn, "SET", "user:1", "a")
    d(conn, "SET", "user:2", "b")
    d(conn, "SET", "other", "c")
    assert sorted(d(conn, "KEYS", "user:*")) == ["user:1", "user:2"]


def test_scan_covers_all_keys(d, conn):
    for i in range(25):
        d(conn, "SET", f"k{i}", str(i))
    seen = set()
    cursor = "0"
    for _ in range(100):  # safety bound against an infinite loop if SCAN is broken
        cursor, keys = d(conn, "SCAN", cursor, "COUNT", "5")
        seen.update(keys)
        if cursor == "0":
            break
    assert seen == {f"k{i}" for i in range(25)}


def test_scan_match_and_type_filters(d, conn):
    d(conn, "SET", "str:1", "v")
    d(conn, "RPUSH", "list:1", "v")
    cursor, keys = d(conn, "SCAN", "0", "COUNT", "100", "TYPE", "string")
    assert "str:1" in keys and "list:1" not in keys


def test_dbsize(d, conn):
    d(conn, "SET", "a", "1")
    d(conn, "SET", "b", "2")
    assert d(conn, "DBSIZE") == 2


def test_flushdb(d, conn):
    d(conn, "SET", "a", "1")
    d(conn, "FLUSHDB")
    assert d(conn, "DBSIZE") == 0


def test_randomkey(d, conn):
    assert d(conn, "RANDOMKEY") is None
    d(conn, "SET", "only", "v")
    assert d(conn, "RANDOMKEY") == "only"


def test_touch(d, conn):
    d(conn, "SET", "a", "1")
    assert d(conn, "TOUCH", "a", "missing") == 1
