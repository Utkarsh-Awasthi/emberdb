from emberdb.protocol import RespError

import pytest


def test_subscribe_confirmation_and_delivery(d, conn):
    sub = type(conn)()
    reply = d(sub, "SUBSCRIBE", "news")
    assert reply.frames == [["subscribe", "news", 1]]

    receivers = d(conn, "PUBLISH", "news", "hello")
    assert receivers == 1
    assert sub.messages == [("news", "hello")]


def test_publish_with_no_subscribers_returns_zero(d, conn):
    assert d(conn, "PUBLISH", "empty-channel", "msg") == 0


def test_multiple_subscribers_all_receive(d, conn):
    sub1, sub2 = type(conn)(), type(conn)()
    d(sub1, "SUBSCRIBE", "room")
    d(sub2, "SUBSCRIBE", "room")
    receivers = d(conn, "PUBLISH", "room", "hi all")
    assert receivers == 2
    assert sub1.messages == [("room", "hi all")]
    assert sub2.messages == [("room", "hi all")]


def test_psubscribe_pattern_match(d, conn):
    sub = type(conn)()
    d(sub, "PSUBSCRIBE", "room:*")
    d(conn, "PUBLISH", "room:1", "hello")
    d(conn, "PUBLISH", "other:1", "should not match")
    assert sub.messages == [("room:1", "hello")]


def test_unsubscribe_stops_delivery(d, conn):
    sub = type(conn)()
    d(sub, "SUBSCRIBE", "news")
    d(sub, "UNSUBSCRIBE", "news")
    d(conn, "PUBLISH", "news", "after unsubscribe")
    assert sub.messages == []


def test_unsubscribe_all_when_no_args(d, conn):
    sub = type(conn)()
    d(sub, "SUBSCRIBE", "a", "b")
    reply = d(sub, "UNSUBSCRIBE")
    assert len(reply.frames) == 2
    assert sub.channels == set()


def test_pubsub_channels_numsub_numpat(d, conn):
    sub = type(conn)()
    d(sub, "SUBSCRIBE", "chan1")
    d(sub, "PSUBSCRIBE", "pat:*")
    assert d(conn, "PUBSUB", "CHANNELS") == ["chan1"]
    assert d(conn, "PUBSUB", "NUMSUB", "chan1") == ["chan1", 1]
    assert d(conn, "PUBSUB", "NUMPAT") == 1


def test_subscriber_mode_restricts_other_commands(d, conn):
    d(conn, "SUBSCRIBE", "chan")
    with pytest.raises(RespError):
        d(conn, "GET", "somekey")
    # PING must still work while subscribed
    from emberdb.protocol import SimpleString
    assert d(conn, "PING") == SimpleString("PONG")


def test_disconnect_cleans_up_subscriptions(engine, d, conn):
    sub = type(conn)()
    d(sub, "SUBSCRIBE", "news")
    assert engine.pubsub.numsub("news") == 1
    engine.unregister_client(sub)
    assert engine.pubsub.numsub("news") == 0
