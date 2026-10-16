from emberdb.protocol import RespError

import pytest


def test_vector_set_and_search_exact_match_ranks_first(d, conn):
    d(conn, "VECTOR.SET", "shoes", "shoe:1", "VALUES", "1", "0", "0")
    d(conn, "VECTOR.SET", "shoes", "shoe:2", "VALUES", "0.9", "0.1", "0")
    d(conn, "VECTOR.SET", "shoes", "shoe:3", "VALUES", "0", "1", "0")
    hits = d(conn, "VECTOR.SEARCH", "shoes", "VALUES", "1", "0", "0")
    # flat [key, score, key, score, ...] format
    assert hits[0] == "shoe:1"


def test_vector_search_k_limits_results(d, conn):
    for i in range(10):
        d(conn, "VECTOR.SET", "ns", f"v{i}", "VALUES", str(i), "0", "0")
    hits = d(conn, "VECTOR.SEARCH", "ns", "VALUES", "5", "0", "0", "K", "3")
    keys = hits[0::2]
    assert len(keys) == 3


def test_vector_dimension_mismatch_raises(d, conn):
    d(conn, "VECTOR.SET", "ns", "a", "VALUES", "1", "2", "3")
    with pytest.raises(RespError):
        d(conn, "VECTOR.SET", "ns", "b", "VALUES", "1", "2")


def test_vector_search_dimension_mismatch_raises(d, conn):
    d(conn, "VECTOR.SET", "ns", "a", "VALUES", "1", "2", "3")
    with pytest.raises(RespError):
        d(conn, "VECTOR.SEARCH", "ns", "VALUES", "1", "2")


def test_vector_del(d, conn):
    d(conn, "VECTOR.SET", "ns", "a", "VALUES", "1", "0")
    assert d(conn, "VECTOR.DEL", "ns", "a") == 1
    assert d(conn, "VECTOR.DEL", "ns", "a") == 0


def test_vector_euclidean_metric_orders_by_distance_ascending(d, conn):
    d(conn, "VECTOR.SET", "ns", "near", "VALUES", "1", "1")
    d(conn, "VECTOR.SET", "ns", "far", "VALUES", "10", "10")
    hits = d(conn, "VECTOR.SEARCH", "ns", "VALUES", "0", "0", "METRIC", "euclidean")
    assert hits[0] == "near"  # smaller distance should rank first


def test_vector_describe(d, conn):
    d(conn, "VECTOR.SET", "ns1", "a", "VALUES", "1", "2")
    d(conn, "VECTOR.SET", "ns1", "b", "VALUES", "3", "4")
    described = d(conn, "VECTOR.DESCRIBE")
    entry = next(x for x in described if x["namespace"] == "ns1")
    assert entry["dimensions"] == 2
    assert entry["count"] == 2


def test_vector_set_with_attrs(d, conn):
    reply = d(conn, "VECTOR.SET", "ns", "a", "VALUES", "1", "2", "ATTRS", '{"color": "red"}')
    from emberdb.protocol import SimpleString
    assert reply == SimpleString("OK")
