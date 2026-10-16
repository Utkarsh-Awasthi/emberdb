def test_hset_hget(d, conn):
    assert d(conn, "HSET", "h", "f1", "v1", "f2", "v2") == 2
    assert d(conn, "HGET", "h", "f1") == "v1"
    assert d(conn, "HGET", "h", "missing") is None


def test_hset_returns_only_new_field_count(d, conn):
    d(conn, "HSET", "h", "f1", "v1")
    assert d(conn, "HSET", "h", "f1", "updated", "f2", "new") == 1  # only f2 is new
    assert d(conn, "HGET", "h", "f1") == "updated"


def test_hgetall(d, conn):
    d(conn, "HSET", "h", "a", "1", "b", "2")
    assert d(conn, "HGETALL", "h") == {"a": "1", "b": "2"}


def test_hdel(d, conn):
    d(conn, "HSET", "h", "a", "1", "b", "2")
    assert d(conn, "HDEL", "h", "a", "nosuch") == 1
    assert d(conn, "HGETALL", "h") == {"b": "2"}


def test_hdel_last_field_removes_key(d, conn):
    d(conn, "HSET", "h", "a", "1")
    d(conn, "HDEL", "h", "a")
    assert d(conn, "EXISTS", "h") == 0


def test_hexists(d, conn):
    d(conn, "HSET", "h", "a", "1")
    assert d(conn, "HEXISTS", "h", "a") == 1
    assert d(conn, "HEXISTS", "h", "b") == 0


def test_hsetnx(d, conn):
    assert d(conn, "HSETNX", "h", "a", "1") == 1
    assert d(conn, "HSETNX", "h", "a", "2") == 0
    assert d(conn, "HGET", "h", "a") == "1"


def test_hincrby(d, conn):
    d(conn, "HSET", "h", "n", "10")
    assert d(conn, "HINCRBY", "h", "n", "5") == 15
    assert d(conn, "HINCRBY", "h", "new", "3") == 3


def test_hincrbyfloat(d, conn):
    d(conn, "HSET", "h", "n", "10.5")
    assert d(conn, "HINCRBYFLOAT", "h", "n", "0.25") == "10.75"


def test_hkeys_hvals_hlen(d, conn):
    d(conn, "HSET", "h", "a", "1", "b", "2")
    assert sorted(d(conn, "HKEYS", "h")) == ["a", "b"]
    assert sorted(d(conn, "HVALS", "h")) == ["1", "2"]
    assert d(conn, "HLEN", "h") == 2


def test_hmget(d, conn):
    d(conn, "HSET", "h", "a", "1", "b", "2")
    assert d(conn, "HMGET", "h", "a", "missing", "b") == ["1", None, "2"]


def test_hstrlen(d, conn):
    d(conn, "HSET", "h", "a", "hello")
    assert d(conn, "HSTRLEN", "h", "a") == 5
    assert d(conn, "HSTRLEN", "h", "missing") == 0


def test_hrandfield_count(d, conn):
    d(conn, "HSET", "h", "a", "1", "b", "2", "c", "3")
    picked = d(conn, "HRANDFIELD", "h", "2")
    assert len(picked) == 2
    assert set(picked) <= {"a", "b", "c"}
