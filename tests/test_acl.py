from emberdb.protocol import RespError

import pytest


def test_no_password_means_no_auth_required(d, conn):
    assert d(conn, "GET", "anything") is None  # not blocked


def test_requirepass_blocks_unauthenticated_commands(engine, conn):
    engine.acl.set_password("default", "s3cret")
    conn.authenticated = False
    with pytest.raises(RespError) as exc:
        engine.dispatch_from_client(conn, ["GET", "k"])
    assert exc.value.code == "NOAUTH"


def test_auth_with_correct_password_succeeds(engine, conn):
    engine.acl.set_password("default", "s3cret")
    conn.authenticated = False
    reply = engine.dispatch_from_client(conn, ["AUTH", "s3cret"])
    from emberdb.protocol import SimpleString
    assert reply == SimpleString("OK")
    assert conn.authenticated is True


def test_auth_with_wrong_password_fails(engine, conn):
    engine.acl.set_password("default", "s3cret")
    with pytest.raises(RespError) as exc:
        engine.dispatch_from_client(conn, ["AUTH", "wrong"])
    assert exc.value.code == "WRONGPASS"


def test_ping_allowed_without_auth(engine, conn):
    engine.acl.set_password("default", "s3cret")
    conn.authenticated = False
    from emberdb.protocol import SimpleString
    assert engine.dispatch_from_client(conn, ["PING"]) == SimpleString("PONG")


def test_restricted_user_command_pattern(engine, conn):
    engine.acl.create_or_update_user("ro", password="pw", command_patterns={"GET", "AUTH"})
    conn.username = "ro"
    conn.authenticated = False
    engine.dispatch_from_client(conn, ["AUTH", "ro", "pw"])
    assert engine.dispatch_from_client(conn, ["GET", "k"]) is None
    with pytest.raises(RespError) as exc:
        engine.dispatch_from_client(conn, ["SET", "k", "v"])
    assert exc.value.code == "NOPERM"


def test_restricted_user_key_pattern(engine, conn):
    engine.acl.create_or_update_user("cache_only", password="pw",
                                      command_patterns={"GET", "SET", "AUTH"}, key_pattern="cache:*")
    conn.username = "cache_only"
    conn.authenticated = False
    engine.dispatch_from_client(conn, ["AUTH", "cache_only", "pw"])
    engine.dispatch_from_client(conn, ["SET", "cache:1", "v"])  # allowed
    with pytest.raises(RespError) as exc:
        engine.dispatch_from_client(conn, ["SET", "other:1", "v"])
    assert exc.value.code == "NOPERM"


def test_disabled_user_cannot_authenticate(engine):
    engine.acl.create_or_update_user("disabled_user", password="pw", enabled=False)
    assert engine.acl.authenticate("disabled_user", "pw") is False


def test_cannot_delete_default_user(engine):
    with pytest.raises(RespError):
        engine.acl.delete_user("default")


def test_acl_whoami_and_list_via_dispatch(d, conn):
    assert d(conn, "ACL", "WHOAMI") == "default"
    listing = d(conn, "ACL", "LIST")
    assert any("default" in line for line in listing)


def test_config_set_requirepass_actually_takes_effect(engine, d, conn):
    from emberdb.protocol import RespError as RE
    d(conn, "CONFIG", "SET", "requirepass", "newpass")
    assert engine.acl.needs_auth_by_default() is True
    other = type(conn)()
    other.authenticated = False
    with pytest.raises(RE) as exc:
        d(other, "GET", "k")
    assert exc.value.code == "NOAUTH"
    d(other, "AUTH", "newpass")
    assert d(other, "GET", "k") is None  # authenticated now, just no such key

    # clearing it via an empty value must also take effect
    d(conn, "CONFIG", "SET", "requirepass", "")
    assert engine.acl.needs_auth_by_default() is False
