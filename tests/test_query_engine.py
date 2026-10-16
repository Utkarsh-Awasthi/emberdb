from emberdb.protocol import RespError

import pytest


def _seed_users(d, conn):
    d(conn, "HSET", "user:1", "email", "bob@example.com", "age", "30")
    d(conn, "HSET", "user:2", "email", "amy@example.com", "age", "25")
    d(conn, "HSET", "user:3", "email", "cid@example.com", "age", "40")


def test_create_index_backfills_existing_keys(d, conn):
    _seed_users(d, conn)
    backfilled = d(conn, "CREATE", "INDEX", "idx_email", "ON", "user:*", "FIELD", "email")
    assert backfilled == 3


def test_find_equality(d, conn):
    _seed_users(d, conn)
    d(conn, "CREATE", "INDEX", "idx_email", "ON", "user:*", "FIELD", "email")
    assert d(conn, "FIND", "idx_email", "WHERE", "email", "=", "bob@example.com") == ["user:1"]


def test_find_numeric_range(d, conn):
    _seed_users(d, conn)
    d(conn, "CREATE", "INDEX", "idx_age", "ON", "user:*", "FIELD", "age")
    assert sorted(d(conn, "FIND", "idx_age", "WHERE", "age", ">", "26")) == ["user:1", "user:3"]
    assert sorted(d(conn, "FIND", "idx_age", "WHERE", "age", "<=", "30")) == ["user:1", "user:2"]


def test_find_with_and_residual_filter(d, conn):
    _seed_users(d, conn)
    d(conn, "CREATE", "INDEX", "idx_age", "ON", "user:*", "FIELD", "age")
    result = d(conn, "FIND", "idx_age", "WHERE", "age", ">", "20", "AND", "email", "=", "amy@example.com")
    assert result == ["user:2"]


def test_index_stays_in_sync_on_update(d, conn):
    _seed_users(d, conn)
    d(conn, "CREATE", "INDEX", "idx_age", "ON", "user:*", "FIELD", "age")
    d(conn, "HSET", "user:2", "age", "99")
    assert sorted(d(conn, "FIND", "idx_age", "WHERE", "age", ">", "26")) == ["user:1", "user:2", "user:3"]


def test_index_removes_key_on_delete(d, conn):
    _seed_users(d, conn)
    d(conn, "CREATE", "INDEX", "idx_email", "ON", "user:*", "FIELD", "email")
    d(conn, "DEL", "user:1")
    assert d(conn, "FIND", "idx_email", "WHERE", "email", "=", "bob@example.com") == []


def test_index_on_json_field(d, conn):
    d(conn, "JSON.SET", "prod:1", "$", '{"category": "shoes", "price": 50}')
    d(conn, "JSON.SET", "prod:2", "$", '{"category": "hats", "price": 20}')
    d(conn, "CREATE", "INDEX", "idx_cat", "ON", "prod:*", "FIELD", "category")
    assert d(conn, "FIND", "idx_cat", "WHERE", "category", "=", "shoes") == ["prod:1"]


def test_find_unknown_index_raises(d, conn):
    with pytest.raises(RespError):
        d(conn, "FIND", "nosuch", "WHERE", "field", "=", "value")


def test_drop_index(d, conn):
    _seed_users(d, conn)
    d(conn, "CREATE", "INDEX", "idx_email", "ON", "user:*", "FIELD", "email")
    assert d(conn, "DROP", "INDEX", "idx_email") == 1
    with pytest.raises(RespError):
        d(conn, "FIND", "idx_email", "WHERE", "email", "=", "bob@example.com")


def test_indexes_listing(d, conn):
    _seed_users(d, conn)
    d(conn, "CREATE", "INDEX", "idx_email", "ON", "user:*", "FIELD", "email")
    listing = d(conn, "INDEXES")
    assert any(i["name"] == "idx_email" for i in listing)


def test_flushdb_clears_index_contents_not_definition(d, conn):
    _seed_users(d, conn)
    d(conn, "CREATE", "INDEX", "idx_email", "ON", "user:*", "FIELD", "email")
    d(conn, "FLUSHDB")
    assert d(conn, "FIND", "idx_email", "WHERE", "email", "=", "bob@example.com") == []
    assert any(i["name"] == "idx_email" for i in d(conn, "INDEXES"))
