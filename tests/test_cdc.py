import time


def test_write_commands_emit_cdc_events(d, conn):
    d(conn, "SET", "a", "1")
    d(conn, "SET", "b", "2")
    events = d(conn, "CDC.SINCE", "0")
    keys = [e["key"] for e in events]
    assert "a" in keys and "b" in keys


def test_since_offset_excludes_earlier_events(d, conn):
    d(conn, "SET", "a", "1")
    first_batch = d(conn, "CDC.SINCE", "0")
    last_offset = first_batch[-1]["offset"]
    d(conn, "SET", "b", "2")
    later = d(conn, "CDC.SINCE", str(last_offset))
    assert all(e["offset"] > last_offset for e in later)
    assert any(e["key"] == "b" for e in later)


def test_read_only_commands_do_not_emit_cdc_events(d, conn):
    d(conn, "SET", "a", "1")
    before = len(d(conn, "CDC.SINCE", "0"))
    d(conn, "GET", "a")
    d(conn, "EXISTS", "a")
    after = len(d(conn, "CDC.SINCE", "0"))
    assert before == after


def test_cdc_tail_live_push(d, conn):
    tail_conn = type(conn)()
    d(tail_conn, "CDC.TAIL")
    d(conn, "SET", "tailed", "v")
    time.sleep(0.02)
    assert any(e.get("key") == "tailed" for e in tail_conn.cdc_events)


def test_cdc_untail_stops_delivery(d, conn):
    tail_conn = type(conn)()
    d(tail_conn, "CDC.TAIL")
    d(tail_conn, "CDC.UNTAIL")
    d(conn, "SET", "after_untail", "v")
    time.sleep(0.02)
    assert not any(e.get("key") == "after_untail" for e in tail_conn.cdc_events)


def test_cdc_status(d, conn):
    d(conn, "SET", "a", "1")
    status = d(conn, "CDC.STATUS")
    assert status["current_offset"] >= 1


def test_flush_emits_flush_type_event(d, conn):
    d(conn, "SET", "a", "1")
    d(conn, "FLUSHDB")
    events = d(conn, "CDC.SINCE", "0")
    assert any(e["type"] == "flush" for e in events)
