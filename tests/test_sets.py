def test_sadd_smembers(d, conn):
    assert d(conn, "SADD", "s", "a", "b", "a") == 2  # dedup counts as one add
    assert d(conn, "SMEMBERS", "s") == {"a", "b"}


def test_srem(d, conn):
    d(conn, "SADD", "s", "a", "b", "c")
    assert d(conn, "SREM", "s", "a", "nosuch") == 1
    assert d(conn, "SMEMBERS", "s") == {"b", "c"}


def test_sismember_smismember(d, conn):
    d(conn, "SADD", "s", "a", "b")
    assert d(conn, "SISMEMBER", "s", "a") == 1
    assert d(conn, "SISMEMBER", "s", "z") == 0
    assert d(conn, "SMISMEMBER", "s", "a", "z", "b") == [1, 0, 1]


def test_scard(d, conn):
    d(conn, "SADD", "s", "a", "b", "c")
    assert d(conn, "SCARD", "s") == 3


def test_sunion_sinter_sdiff(d, conn):
    d(conn, "SADD", "s1", "a", "b", "c")
    d(conn, "SADD", "s2", "b", "c", "d")
    assert d(conn, "SUNION", "s1", "s2") == {"a", "b", "c", "d"}
    assert d(conn, "SINTER", "s1", "s2") == {"b", "c"}
    assert d(conn, "SDIFF", "s1", "s2") == {"a"}


def test_sunionstore_sinterstore_sdiffstore(d, conn):
    d(conn, "SADD", "s1", "a", "b", "c")
    d(conn, "SADD", "s2", "b", "c", "d")
    assert d(conn, "SUNIONSTORE", "dest", "s1", "s2") == 4
    assert d(conn, "SMEMBERS", "dest") == {"a", "b", "c", "d"}
    assert d(conn, "SINTERSTORE", "dest", "s1", "s2") == 2
    assert d(conn, "SMEMBERS", "dest") == {"b", "c"}
    assert d(conn, "SDIFFSTORE", "dest", "s1", "s2") == 1
    assert d(conn, "SMEMBERS", "dest") == {"a"}


def test_spop_count(d, conn):
    d(conn, "SADD", "s", "a", "b", "c")
    popped = d(conn, "SPOP", "s", "2")
    assert len(popped) == 2
    assert d(conn, "SCARD", "s") == 1


def test_spop_empties_and_deletes_key(d, conn):
    d(conn, "SADD", "s", "only")
    d(conn, "SPOP", "s")
    assert d(conn, "EXISTS", "s") == 0


def test_srandmember_does_not_remove(d, conn):
    d(conn, "SADD", "s", "a", "b", "c")
    d(conn, "SRANDMEMBER", "s", "2")
    assert d(conn, "SCARD", "s") == 3  # srandmember never mutates
