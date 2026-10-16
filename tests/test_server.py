import asyncio

import pytest

from emberdb.engine import EmberEngine
from emberdb.protocol import encode_command
from emberdb.server import serve_resp

_next_port = [17600]


def _alloc_port() -> int:
    _next_port[0] += 1
    return _next_port[0]


async def read_reply_raw(reader: asyncio.StreamReader) -> bytes:
    """Independent mini RESP parser — deliberately not using protocol.py,
    so these tests validate the server against a *different* reader than
    the one it uses internally."""
    first = await reader.readline()
    tag = first[0:1]
    if tag in (b"+", b"-", b":"):
        return first
    if tag == b"$":
        n = int(first[1:].strip())
        return first if n == -1 else first + await reader.readexactly(n + 2)
    if tag in (b"*", b">"):
        n = int(first[1:].strip())
        if n == -1:
            return first
        out = first
        for _ in range(n):
            out += await read_reply_raw(reader)
        return out
    if tag == b"%":
        n = int(first[1:].strip())
        out = first
        for _ in range(n * 2):
            out += await read_reply_raw(reader)
        return out
    if tag in (b"_", b"#"):
        return first
    raise AssertionError(f"unrecognized tag: {first!r}")


class ServerHarness:
    """Starts a real EmberEngine + RESP server for one test and tracks
    every opened connection so cleanup can force-close them all —
    asyncio.Server.wait_closed() in this Python version waits for every
    connection handler to finish, so a stray open socket would otherwise
    hang the test indefinitely."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.port = _alloc_port()
        self.connections = []

    async def __aenter__(self):
        cfg = {
            "databases": 4, "appendonly": False,
            "snapshot_path": str(self.tmp_path / "snap.json"),
            "cdc_path": str(self.tmp_path / "cdc.jsonl"),
        }
        self.engine = EmberEngine(cfg)
        await self.engine.start_background_tasks()
        self.server = await serve_resp(self.engine, "127.0.0.1", self.port)
        return self

    async def connect(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.connections.append(writer)
        return reader, writer

    async def __aexit__(self, *exc):
        for w in self.connections:
            try:
                if not w.is_closing():
                    w.close()
            except Exception:
                pass
        for w in self.connections:
            try:
                await asyncio.wait_for(w.wait_closed(), timeout=2.0)
            except Exception:
                pass
        self.server.close()
        await asyncio.wait_for(self.server.wait_closed(), timeout=5.0)
        self.engine.shutdown()


def test_basic_request_response(tmp_path):
    async def body():
        async with ServerHarness(tmp_path) as h:
            reader, writer = await h.connect()
            writer.write(encode_command(["PING"]))
            await writer.drain()
            assert await read_reply_raw(reader) == b"+PONG\r\n"

            writer.write(encode_command(["SET", "foo", "bar"]))
            await writer.drain()
            assert await read_reply_raw(reader) == b"+OK\r\n"

            writer.write(encode_command(["GET", "foo"]))
            await writer.drain()
            assert await read_reply_raw(reader) == b"$3\r\nbar\r\n"

    asyncio.run(body())


def test_error_reply_framing(tmp_path):
    async def body():
        async with ServerHarness(tmp_path) as h:
            reader, writer = await h.connect()
            writer.write(encode_command(["SET", "k", "v"]))
            await writer.drain()
            await read_reply_raw(reader)
            writer.write(encode_command(["LPUSH", "k", "x"]))
            await writer.drain()
            reply = await read_reply_raw(reader)
            assert reply.startswith(b"-WRONGTYPE")

    asyncio.run(body())


def test_pipelining(tmp_path):
    async def body():
        async with ServerHarness(tmp_path) as h:
            reader, writer = await h.connect()
            writer.write(encode_command(["SET", "a", "1"]))
            writer.write(encode_command(["SET", "b", "2"]))
            writer.write(encode_command(["MGET", "a", "b"]))
            await writer.drain()
            assert await read_reply_raw(reader) == b"+OK\r\n"
            assert await read_reply_raw(reader) == b"+OK\r\n"
            assert await read_reply_raw(reader) == b"*2\r\n$1\r\n1\r\n$1\r\n2\r\n"

    asyncio.run(body())


def test_inline_commands(tmp_path):
    async def body():
        async with ServerHarness(tmp_path) as h:
            reader, writer = await h.connect()
            writer.write(b"PING\r\n")
            await writer.drain()
            assert await read_reply_raw(reader) == b"+PONG\r\n"

    asyncio.run(body())


def test_multi_exec_over_the_wire(tmp_path):
    async def body():
        async with ServerHarness(tmp_path) as h:
            reader, writer = await h.connect()
            for cmd in (["MULTI"], ["SET", "tx", "1"], ["INCR", "tx"]):
                writer.write(encode_command(cmd))
            await writer.drain()
            assert await read_reply_raw(reader) == b"+OK\r\n"
            assert await read_reply_raw(reader) == b"+QUEUED\r\n"
            assert await read_reply_raw(reader) == b"+QUEUED\r\n"
            writer.write(encode_command(["EXEC"]))
            await writer.drain()
            assert await read_reply_raw(reader) == b"*2\r\n+OK\r\n:2\r\n"

    asyncio.run(body())


def test_hello_upgrades_to_resp3(tmp_path):
    async def body():
        async with ServerHarness(tmp_path) as h:
            reader, writer = await h.connect()
            writer.write(encode_command(["HELLO", "3"]))
            await writer.drain()
            reply = await read_reply_raw(reader)
            assert reply.startswith(b"%")
            writer.write(encode_command(["GET", "nosuchkey"]))
            await writer.drain()
            assert await read_reply_raw(reader) == b"_\r\n"

    asyncio.run(body())


def test_pubsub_across_connections(tmp_path):
    async def body():
        async with ServerHarness(tmp_path) as h:
            sub_reader, sub_writer = await h.connect()
            pub_reader, pub_writer = await h.connect()

            sub_writer.write(encode_command(["SUBSCRIBE", "news"]))
            await sub_writer.drain()
            confirm = await read_reply_raw(sub_reader)
            assert confirm == b"*3\r\n$9\r\nsubscribe\r\n$4\r\nnews\r\n:1\r\n"

            pub_writer.write(encode_command(["PUBLISH", "news", "breaking"]))
            await pub_writer.drain()
            assert await read_reply_raw(pub_reader) == b":1\r\n"

            pushed = await asyncio.wait_for(read_reply_raw(sub_reader), timeout=2.0)
            assert pushed == b"*3\r\n$7\r\nmessage\r\n$4\r\nnews\r\n$8\r\nbreaking\r\n"

    asyncio.run(body())


def test_quit_closes_connection(tmp_path):
    async def body():
        async with ServerHarness(tmp_path) as h:
            reader, writer = await h.connect()
            writer.write(encode_command(["QUIT"]))
            await writer.drain()
            assert await read_reply_raw(reader) == b"+OK\r\n"
            assert await reader.read(10) == b""

    asyncio.run(body())
