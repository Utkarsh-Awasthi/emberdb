import asyncio

import pytest

from emberdb.protocol import (
    MultiReply,
    ProtocolError,
    RespError,
    RespPush,
    SimpleString,
    Verbatim,
    encode_command,
    encode_reply,
    read_command,
)


def test_simple_types_resp2():
    assert encode_reply(SimpleString("OK")) == b"+OK\r\n"
    assert encode_reply("hello") == b"$5\r\nhello\r\n"
    assert encode_reply(42) == b":42\r\n"
    assert encode_reply(None) == b"$-1\r\n"
    assert encode_reply([]) == b"*0\r\n"
    assert encode_reply(["a", "b"]) == b"*2\r\n$1\r\na\r\n$1\r\nb\r\n"


def test_error_reply():
    err = RespError("bad things happened", code="WRONGTYPE")
    assert encode_reply(err) == b"-WRONGTYPE bad things happened\r\n"
    generic = RespError("oops")
    assert encode_reply(generic) == b"-ERR oops\r\n"


def test_bool_is_checked_before_int():
    # bool is an int subclass in Python; encoder must special-case it first.
    assert encode_reply(True) == b":1\r\n"
    assert encode_reply(False) == b":0\r\n"


def test_resp3_upgrades():
    assert encode_reply(None, protocol_version=3) == b"_\r\n"
    assert encode_reply(True, protocol_version=3) == b"#t\r\n"
    assert encode_reply(False, protocol_version=3) == b"#f\r\n"
    assert encode_reply(3.5, protocol_version=3) == b",3.5\r\n"
    assert encode_reply({"a": 1}, protocol_version=3) == b"%1\r\n$1\r\na\r\n:1\r\n"


def test_dict_resp2_flattens_to_array():
    assert encode_reply({"a": 1}, protocol_version=2) == b"*2\r\n$1\r\na\r\n:1\r\n"


def test_set_resp3_vs_resp2():
    assert encode_reply({"x"}, protocol_version=3) == b"~1\r\n$1\r\nx\r\n"
    assert encode_reply({"x"}, protocol_version=2) == b"*1\r\n$1\r\nx\r\n"


def test_verbatim_degrades_on_resp2():
    v = Verbatim("hello", fmt="txt")
    assert encode_reply(v, protocol_version=3) == b"=9\r\ntxt:hello\r\n"
    assert encode_reply(v, protocol_version=2) == b"$5\r\nhello\r\n"


def test_push_uses_correct_marker_per_protocol():
    push = RespPush(["message", "chan", "hi"])
    assert encode_reply(push, protocol_version=3).startswith(b">3\r\n")
    assert encode_reply(push, protocol_version=2).startswith(b"*3\r\n")


def test_multi_reply_concatenates_independent_frames():
    mr = MultiReply([["subscribe", "a", 1], ["subscribe", "b", 2]])
    encoded = encode_reply(mr)
    assert encoded == b"*3\r\n$9\r\nsubscribe\r\n$1\r\na\r\n:1\r\n" b"*3\r\n$9\r\nsubscribe\r\n$1\r\nb\r\n:2\r\n"


def test_encode_command_round_trips_through_read_command():
    async def run():
        argv = ["SET", "foo", "bar baz"]
        data = encode_command(argv)
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        result = await read_command(reader)
        return [b.decode() for b in result]

    assert asyncio.run(run()) == ["SET", "foo", "bar baz"]


def test_read_command_inline_style():
    async def run():
        reader = asyncio.StreamReader()
        reader.feed_data(b"PING\r\n")
        reader.feed_eof()
        return await read_command(reader)

    assert asyncio.run(run()) == [b"PING"]


def test_read_command_eof_returns_none():
    async def run():
        reader = asyncio.StreamReader()
        reader.feed_eof()
        return await read_command(reader)

    assert asyncio.run(run()) is None


def test_read_command_rejects_garbage_multibulk_length():
    async def run():
        reader = asyncio.StreamReader()
        reader.feed_data(b"*notanumber\r\n")
        reader.feed_eof()
        with pytest.raises(ProtocolError):
            await read_command(reader)

    asyncio.run(run())
