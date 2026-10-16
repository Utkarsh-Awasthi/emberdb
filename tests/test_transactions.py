from emberdb.protocol import RespError

import pytest


def test_multi_exec_happy_path(d, conn):
    d(conn, "SET", "counter", "10")
    d(conn, "MULTI")
    d(conn, "INCR", "counter")
    d(conn, "INCR", "counter")
    result = d(conn, "EXEC")
    assert result == [11, 12]
    assert d(conn, "GET", "counter") == "12"


def test_exec_without_multi_raises(d, conn):
    with pytest.raises(RespError):
        d(conn, "EXEC")


def test_multi_cannot_nest(d, conn):
    d(conn, "MULTI")
    with pytest.raises(RespError):
        d(conn, "MULTI")
    d(conn, "DISCARD")  # clean up so the connection isn't left mid-transaction


def test_discard(d, conn):
    d(conn, "SET", "k", "orig")
    d(conn, "MULTI")
    d(conn, "SET", "k", "changed")
    d(conn, "DISCARD")
    assert d(conn, "GET", "k") == "orig"
    assert conn.in_multi is False


def test_rollback_is_all_or_nothing(engine, d, conn):
    d(conn, "SET", "a", "orig")
    d(conn, "MULTI")
    d(conn, "SET", "a", "changed")
    d(conn, "LPUSH", "a", "x")  # queues fine, fails at runtime: a holds a string
    d(conn, "SET", "c", "unreached")
    with pytest.raises(RespError) as exc:
        d(conn, "EXEC")
    assert exc.value.code == "EXECABORT"
    assert d(conn, "GET", "a") == "orig", "earlier command in the same failed transaction must roll back"
    assert d(conn, "EXISTS", "c") == 0, "command after the failure must never have applied"


def test_rollback_excludes_aof(engine, d, conn):
    d(conn, "MULTI")
    d(conn, "SET", "will_rollback", "1")
    d(conn, "LPUSH", "will_rollback", "x")  # forces rollback: will_rollback now holds a string
    with pytest.raises(RespError):
        d(conn, "EXEC")
    engine.aof.flush_and_sync()
    with open(engine.aof_path) as f:
        lines = f.readlines()
    assert not any("will_rollback" in line for line in lines)


def test_queue_time_error_causes_execabort(d, conn):
    d(conn, "MULTI")
    d(conn, "SET", "x", "1")
    with pytest.raises(RespError):
        d(conn, "NOTACOMMAND")
    with pytest.raises(RespError) as exc:
        d(conn, "EXEC")
    assert exc.value.code == "EXECABORT"


def test_watch_aborts_exec_on_external_change(engine, d, conn):
    other = type(conn)()
    d(conn, "SET", "watched", "v1")
    d(conn, "WATCH", "watched")
    d(conn, "MULTI")
    d(conn, "SET", "watched", "v2")
    d(other, "SET", "watched", "changed_elsewhere")
    result = d(conn, "EXEC")
    assert result is None
    assert d(conn, "GET", "watched") == "changed_elsewhere"


def test_watch_does_not_abort_if_key_unchanged(d, conn):
    d(conn, "SET", "k", "v")
    d(conn, "WATCH", "k")
    d(conn, "MULTI")
    d(conn, "SET", "k", "v2")
    result = d(conn, "EXEC")
    assert result == ["OK"] or result[0] is not None
    assert d(conn, "GET", "k") == "v2"


def test_unwatch(d, conn):
    other = type(conn)()
    d(conn, "SET", "k", "v")
    d(conn, "WATCH", "k")
    d(conn, "UNWATCH")
    d(conn, "MULTI")
    d(conn, "SET", "k", "v2")
    d(other, "SET", "k", "changed")
    result = d(conn, "EXEC")
    assert result is not None  # unwatched, so the external change shouldn't abort it
    assert d(conn, "GET", "k") == "v2"
