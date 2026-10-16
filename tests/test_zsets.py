def test_zadd_zscore(d, conn):
    d(conn, "ZADD", "z", "1", "a", "2", "b", "3", "c")
    assert d(conn, "ZSCORE", "z", "b") == "2"
    assert d(conn, "ZCARD", "z") == 3


def test_zadd_nx_does_not_update_existing(d, conn):
    d(conn, "ZADD", "z", "1", "a")
    d(conn, "ZADD", "z", "NX", "99", "a")
    assert d(conn, "ZSCORE", "z", "a") == "1"


def test_zadd_xx_only_updates_existing(d, conn):
    d(conn, "ZADD", "z", "XX", "5", "new")
    assert d(conn, "ZSCORE", "z", "new") is None


def test_zadd_gt_lt(d, conn):
    d(conn, "ZADD", "z", "5", "a")
    d(conn, "ZADD", "z", "GT", "3", "a")  # 3 < 5, should not update
    assert d(conn, "ZSCORE", "z", "a") == "5"
    d(conn, "ZADD", "z", "GT", "10", "a")
    assert d(conn, "ZSCORE", "z", "a") == "10"
    d(conn, "ZADD", "z", "LT", "20", "a")  # 20 > 10, should not update
    assert d(conn, "ZSCORE", "z", "a") == "10"
    d(conn, "ZADD", "z", "LT", "2", "a")
    assert d(conn, "ZSCORE", "z", "a") == "2"


def test_zadd_ch_counts_changed(d, conn):
    d(conn, "ZADD", "z", "1", "a")
    changed = d(conn, "ZADD", "z", "CH", "2", "a", "3", "b")
    assert changed == 2  # a's score changed, b is new


def test_zadd_incr(d, conn):
    d(conn, "ZADD", "z", "5", "a")
    new_score = d(conn, "ZADD", "z", "INCR", "3", "a")
    assert new_score == "8"


def test_zrange_by_rank(d, conn):
    d(conn, "ZADD", "z", "1", "a", "2", "b", "3", "c")
    assert d(conn, "ZRANGE", "z", "0", "-1") == ["a", "b", "c"]
    assert d(conn, "ZRANGE", "z", "0", "1") == ["a", "b"]


def test_zrevrange(d, conn):
    d(conn, "ZADD", "z", "1", "a", "2", "b", "3", "c")
    assert d(conn, "ZREVRANGE", "z", "0", "-1") == ["c", "b", "a"]


def test_zrange_withscores(d, conn):
    d(conn, "ZADD", "z", "1", "a", "2", "b")
    assert d(conn, "ZRANGE", "z", "0", "-1", "WITHSCORES") == ["a", "1", "b", "2"]


def test_zrangebyscore(d, conn):
    d(conn, "ZADD", "z", "1", "a", "2", "b", "3", "c", "4", "d")
    assert d(conn, "ZRANGEBYSCORE", "z", "2", "3") == ["b", "c"]
    assert d(conn, "ZRANGEBYSCORE", "z", "(1", "3") == ["b", "c"]
    assert d(conn, "ZRANGEBYSCORE", "z", "-inf", "+inf") == ["a", "b", "c", "d"]


def test_zrangebyscore_limit(d, conn):
    d(conn, "ZADD", "z", "1", "a", "2", "b", "3", "c", "4", "d")
    assert d(conn, "ZRANGEBYSCORE", "z", "-inf", "+inf", "LIMIT", "1", "2") == ["b", "c"]


def test_zcount(d, conn):
    d(conn, "ZADD", "z", "1", "a", "2", "b", "3", "c")
    assert d(conn, "ZCOUNT", "z", "1", "2") == 2


def test_zrank_zrevrank(d, conn):
    d(conn, "ZADD", "z", "1", "a", "2", "b", "3", "c")
    assert d(conn, "ZRANK", "z", "b") == 1
    assert d(conn, "ZREVRANK", "z", "b") == 1
    assert d(conn, "ZRANK", "z", "missing") is None


def test_zincrby(d, conn):
    d(conn, "ZADD", "z", "5", "a")
    assert d(conn, "ZINCRBY", "z", "3", "a") == "8"


def test_zrem(d, conn):
    d(conn, "ZADD", "z", "1", "a", "2", "b")
    assert d(conn, "ZREM", "z", "a", "nosuch") == 1
    assert d(conn, "ZRANGE", "z", "0", "-1") == ["b"]


def test_zpopmin_zpopmax(d, conn):
    d(conn, "ZADD", "z", "1", "a", "2", "b", "3", "c")
    assert d(conn, "ZPOPMIN", "z") == ["a", "1"]
    assert d(conn, "ZPOPMAX", "z") == ["c", "3"]
    assert d(conn, "ZRANGE", "z", "0", "-1") == ["b"]


def test_zmscore(d, conn):
    d(conn, "ZADD", "z", "1", "a", "2", "b")
    assert d(conn, "ZMSCORE", "z", "a", "missing", "b") == ["1", None, "2"]
