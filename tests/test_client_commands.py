from emberdb.protocol import RespError, SimpleString

import pytest


def test_client_setname_getname(d, conn):
    assert d(conn, "CLIENT", "SETNAME", "myconn") == SimpleString("OK")
    assert d(conn, "CLIENT", "GETNAME") == "myconn"


def test_client_id_is_positive(d, conn):
    conn.client_id = 7
    assert d(conn, "CLIENT", "ID") == 7


def test_client_setinfo(d, conn):
    assert d(conn, "CLIENT", "SETINFO", "LIB-NAME", "redis-py") == SimpleString("OK")
    assert d(conn, "CLIENT", "SETINFO", "LIB-VER", "8.1.0") == SimpleString("OK")
    info = d(conn, "CLIENT", "INFO")
    assert "lib-name=redis-py" in info
    assert "lib-ver=8.1.0" in info


def test_client_setinfo_rejects_unknown_attribute(d, conn):
    with pytest.raises(RespError):
        d(conn, "CLIENT", "SETINFO", "BOGUS-ATTR", "x")


def test_client_no_evict_no_touch_pause_unpause_are_accepted_noops(d, conn):
    for args in (["NO-EVICT", "ON"], ["NO-TOUCH", "ON"], ["PAUSE", "100"], ["UNPAUSE"]):
        assert d(conn, "CLIENT", *args) == SimpleString("OK")


def test_client_maint_notifications_accepted(d, conn):
    """redis-py 8.x sends `CLIENT MAINT_NOTIFICATIONS ON moving-endpoint-type
    internal-ip` automatically on every connect (an Enterprise/Cloud
    maintenance-failover opt-in). A standalone server has no such
    notifications to send, but must still answer OK rather than reject the
    subcommand, or every redis-py connection logs a spurious error and a
    stricter client could fail outright. Verified against a real redis-py
    connection with a wire-level proxy; this is the regression test."""
    reply = d(conn, "CLIENT", "MAINT_NOTIFICATIONS", "ON",
              "moving-endpoint-type", "internal-ip")
    assert reply == SimpleString("OK")


def test_client_unknown_subcommand_still_rejected(d, conn):
    with pytest.raises(RespError):
        d(conn, "CLIENT", "NOT-A-REAL-SUBCOMMAND")


def test_client_list_includes_connected_clients(engine, d, conn):
    engine.register_client(conn)
    listing = d(conn, "CLIENT", "LIST")
    assert str(conn.client_id) in listing.value
