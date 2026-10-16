from emberdb.protocol import RespError

import pytest


def test_lpush_rpush_order(d, conn):
    d(conn, "RPUSH", "k", "a", "b", "c")
    assert d(conn, "LRANGE", "k", "0", "-1") == ["a", "b", "c"]
    d(conn, "LPUSH", "k", "z")
    assert d(conn, "LRANGE", "k", "0", "-1") == ["z", "a", "b", "c"]


def test_llen(d, conn):
    d(conn, "RPUSH", "k", "a", "b")
    assert d(conn, "LLEN", "k") == 2
    assert d(conn, "LLEN", "missing") == 0


def test_lpop_rpop(d, conn):
    d(conn, "RPUSH", "k", "a", "b", "c")
    assert d(conn, "LPOP", "k") == "a"
    assert d(conn, "RPOP", "k") == "c"
    assert d(conn, "LRANGE", "k", "0", "-1") == ["b"]


def test_lpop_with_count(d, conn):
    d(conn, "RPUSH", "k", "a", "b", "c", "d")
    assert d(conn, "LPOP", "k", "2") == ["a", "b"]


def test_pop_empties_and_deletes_key(d, conn):
    d(conn, "RPUSH", "k", "only")
    d(conn, "LPOP", "k")
    assert d(conn, "EXISTS", "k") == 0


def test_lindex_negative(d, conn):
    d(conn, "RPUSH", "k", "a", "b", "c")
    assert d(conn, "LINDEX", "k", "-1") == "c"
    assert d(conn, "LINDEX", "k", "0") == "a"
    assert d(conn, "LINDEX", "k", "99") is None


def test_lset(d, conn):
    d(conn, "RPUSH", "k", "a", "b", "c")
    d(conn, "LSET", "k", "1", "B")
    assert d(conn, "LRANGE", "k", "0", "-1") == ["a", "B", "c"]


def test_lset_out_of_range_raises(d, conn):
    d(conn, "RPUSH", "k", "a")
    with pytest.raises(RespError):
        d(conn, "LSET", "k", "5", "x")


def test_ltrim(d, conn):
    d(conn, "RPUSH", "k", "a", "b", "c", "d", "e")
    d(conn, "LTRIM", "k", "1", "3")
    assert d(conn, "LRANGE", "k", "0", "-1") == ["b", "c", "d"]


def test_lrem_positive_negative_zero(d, conn):
    d(conn, "RPUSH", "k", "a", "b", "a", "c", "a")
    assert d(conn, "LREM", "k", "2", "a") == 2
    assert d(conn, "LRANGE", "k", "0", "-1") == ["b", "c", "a"]

    d(conn, "DEL", "k")
    d(conn, "RPUSH", "k", "a", "b", "a", "c", "a")
    assert d(conn, "LREM", "k", "-1", "a") == 1
    assert d(conn, "LRANGE", "k", "0", "-1") == ["a", "b", "a", "c"]

    d(conn, "DEL", "k")
    d(conn, "RPUSH", "k", "a", "b", "a")
    assert d(conn, "LREM", "k", "0", "a") == 2
    assert d(conn, "LRANGE", "k", "0", "-1") == ["b"]


def test_linsert_before_after(d, conn):
    d(conn, "RPUSH", "k", "a", "c")
    d(conn, "LINSERT", "k", "BEFORE", "c", "b")
    assert d(conn, "LRANGE", "k", "0", "-1") == ["a", "b", "c"]
    d(conn, "LINSERT", "k", "AFTER", "c", "d")
    assert d(conn, "LRANGE", "k", "0", "-1") == ["a", "b", "c", "d"]


def test_linsert_missing_pivot_returns_minus_one(d, conn):
    d(conn, "RPUSH", "k", "a")
    assert d(conn, "LINSERT", "k", "BEFORE", "nosuch", "x") == -1


def test_lmove(d, conn):
    d(conn, "RPUSH", "src", "a", "b", "c")
    moved = d(conn, "LMOVE", "src", "dst", "RIGHT", "LEFT")
    assert moved == "c"
    assert d(conn, "LRANGE", "src", "0", "-1") == ["a", "b"]
    assert d(conn, "LRANGE", "dst", "0", "-1") == ["c"]


def test_lpushx_rpushx_noop_on_missing_key(d, conn):
    assert d(conn, "LPUSHX", "missing", "x") == 0
    assert d(conn, "RPUSHX", "missing", "x") == 0
    assert d(conn, "EXISTS", "missing") == 0
