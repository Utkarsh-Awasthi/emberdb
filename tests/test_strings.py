from emberdb.protocol import RespError, SimpleString

import pytest


def test_set_get_roundtrip(d, conn):
    assert d(conn, "SET", "k", "v") == SimpleString("OK")
    assert d(conn, "GET", "k") == "v"


def test_get_missing_key_is_nil(d, conn):
    assert d(conn, "GET", "nope") is None


def test_set_nx_xx(d, conn):
    assert d(conn, "SET", "k", "1", "NX") == SimpleString("OK")
    assert d(conn, "SET", "k", "2", "NX") is None
    assert d(conn, "GET", "k") == "1"
    assert d(conn, "SET", "other", "x", "XX") is None
    assert d(conn, "SET", "k", "3", "XX") == SimpleString("OK")
    assert d(conn, "GET", "k") == "3"


def test_set_get_flag_returns_old_value(d, conn):
    d(conn, "SET", "k", "old")
    assert d(conn, "SET", "k", "new", "GET") == "old"
    assert d(conn, "GET", "k") == "new"


def test_set_ex_sets_ttl(d, conn):
    d(conn, "SET", "k", "v", "EX", "100")
    ttl = d(conn, "TTL", "k")
    assert 0 < ttl <= 100


def test_set_keepttl_preserves_existing_ttl(d, conn):
    d(conn, "SET", "k", "v", "EX", "100")
    d(conn, "SET", "k", "v2", "KEEPTTL")
    ttl = d(conn, "TTL", "k")
    assert 0 < ttl <= 100


def test_set_without_keepttl_clears_ttl(d, conn):
    d(conn, "SET", "k", "v", "EX", "100")
    d(conn, "SET", "k", "v2")
    assert d(conn, "TTL", "k") == -1


def test_incr_decr(d, conn):
    assert d(conn, "INCR", "counter") == 1
    assert d(conn, "INCR", "counter") == 2
    assert d(conn, "DECR", "counter") == 1
    assert d(conn, "INCRBY", "counter", "10") == 11
    assert d(conn, "DECRBY", "counter", "5") == 6


def test_incr_on_non_numeric_raises(d, conn):
    d(conn, "SET", "k", "not a number")
    with pytest.raises(RespError):
        d(conn, "INCR", "k")


def test_incrbyfloat(d, conn):
    d(conn, "SET", "k", "10.5")
    result = d(conn, "INCRBYFLOAT", "k", "0.1")
    assert result == "10.6"


def test_append_and_strlen(d, conn):
    assert d(conn, "APPEND", "k", "hello") == 5
    assert d(conn, "APPEND", "k", " world") == 11
    assert d(conn, "GET", "k") == "hello world"
    assert d(conn, "STRLEN", "k") == 11


def test_getrange(d, conn):
    d(conn, "SET", "k", "Hello World")
    assert d(conn, "GETRANGE", "k", "0", "4") == "Hello"
    assert d(conn, "GETRANGE", "k", "-5", "-1") == "World"
    assert d(conn, "GETRANGE", "k", "0", "-1") == "Hello World"


def test_setrange(d, conn):
    d(conn, "SET", "k", "Hello World")
    d(conn, "SETRANGE", "k", "6", "Redis")
    assert d(conn, "GET", "k") == "Hello Redis"


def test_setrange_extends_with_null_bytes(d, conn):
    d(conn, "SETRANGE", "k", "5", "hi")
    val = d(conn, "GET", "k")
    assert val == "\x00\x00\x00\x00\x00hi"


def test_mset_mget(d, conn):
    d(conn, "MSET", "a", "1", "b", "2", "c", "3")
    assert d(conn, "MGET", "a", "b", "c", "missing") == ["1", "2", "3", None]


def test_msetnx_all_or_nothing(d, conn):
    d(conn, "SET", "b", "existing")
    result = d(conn, "MSETNX", "a", "1", "b", "2")
    assert result == 0
    assert d(conn, "GET", "a") is None  # nothing should have been set


def test_setnx(d, conn):
    assert d(conn, "SETNX", "k", "1") == 1
    assert d(conn, "SETNX", "k", "2") == 0
    assert d(conn, "GET", "k") == "1"


def test_getdel(d, conn):
    d(conn, "SET", "k", "v")
    assert d(conn, "GETDEL", "k") == "v"
    assert d(conn, "EXISTS", "k") == 0


def test_getex_persist(d, conn):
    d(conn, "SET", "k", "v", "EX", "100")
    d(conn, "GETEX", "k", "PERSIST")
    assert d(conn, "TTL", "k") == -1


def test_wrongtype_error(d, conn):
    d(conn, "RPUSH", "k", "a")
    with pytest.raises(RespError) as exc_info:
        d(conn, "GET", "k")
    assert exc_info.value.code == "WRONGTYPE"
