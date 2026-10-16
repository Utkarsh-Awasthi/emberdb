#!/usr/bin/env python3
"""Consistent-hash sharding proxy across independent EmberDB instances. Single-key commands only."""

from __future__ import annotations

import argparse
import asyncio
import bisect
import hashlib

# -- standalone RESP codec (no dependency on the emberdb package, same
# spirit as cli/ember_cli.py — this tool works against any RESP server) --


def encode_command(argv: list[bytes]) -> bytes:
    out = bytearray(f"*{len(argv)}\r\n".encode())
    for arg in argv:
        b = arg if isinstance(arg, bytes) else str(arg).encode("utf-8", "surrogateescape")
        out += f"${len(b)}\r\n".encode() + b + b"\r\n"
    return bytes(out)


async def read_command(reader: asyncio.StreamReader):
    line = await reader.readline()
    if not line:
        return None
    line = line.rstrip(b"\r\n")
    if not line:
        return []
    if line[0:1] != b"*":
        return line.split()  # inline command, telnet/nc style
    count = int(line[1:])
    argv = []
    for _ in range(count):
        header = await reader.readline()
        n = int(header[1:].rstrip(b"\r\n"))
        data = await reader.readexactly(n + 2)
        argv.append(data[:-2])
    return argv


async def read_raw_reply(reader: asyncio.StreamReader) -> bytes:
    """Read one full RESP reply as raw bytes — the proxy relays replies
    byte-for-byte without interpreting them, so it never needs a full
    reply *decoder*, only enough framing awareness to know where one
    reply ends and the next begins."""
    first = await reader.readline()
    if not first:
        raise ConnectionError("shard connection closed")
    tag = first[0:1]
    if tag in (b"+", b"-", b":", b"_", b"#"):
        return first
    if tag == b"$":
        n = int(first[1:-2])
        return first if n == -1 else first + await reader.readexactly(n + 2)
    if tag in (b"*", b">"):
        n = int(first[1:-2])
        if n == -1:
            return first
        out = first
        for _ in range(n):
            out += await read_raw_reply(reader)
        return out
    if tag == b"%":
        n = int(first[1:-2])
        out = first
        for _ in range(n * 2):
            out += await read_raw_reply(reader)
        return out
    return first


# -- consistent hashing ------------------------------------------------------

class ConsistentHashRing:
    def __init__(self, nodes: list[str], vnodes: int = 150):
        self.vnodes = vnodes
        self._ring: dict[int, str] = {}
        self._sorted_hashes: list[int] = []
        for node in nodes:
            self.add_node(node)

    @staticmethod
    def _hash(key: str) -> int:
        return int(hashlib.md5(key.encode()).hexdigest(), 16)

    def add_node(self, node: str) -> None:
        for i in range(self.vnodes):
            self._ring[self._hash(f"{node}#{i}")] = node
        self._sorted_hashes = sorted(self._ring)

    def remove_node(self, node: str) -> None:
        for i in range(self.vnodes):
            self._ring.pop(self._hash(f"{node}#{i}"), None)
        self._sorted_hashes = sorted(self._ring)

    def get_node(self, key: str) -> str:
        if not self._sorted_hashes:
            raise RuntimeError("no shards configured")
        h = self._hash(key)
        idx = bisect.bisect(self._sorted_hashes, h) % len(self._sorted_hashes)
        return self._ring[self._sorted_hashes[idx]]


# -- proxying -----------------------------------------------------------------

UNROUTABLE = {
    "MULTI", "EXEC", "DISCARD", "WATCH", "UNWATCH", "SELECT",
    "SUBSCRIBE", "PSUBSCRIBE", "UNSUBSCRIBE", "PUNSUBSCRIBE", "PUBLISH",
    "FLUSHALL", "FLUSHDB", "SYNC", "REPLICAOF", "SLAVEOF",
}
NO_KEY_COMMANDS = {"PING", "ECHO", "TIME", "INFO", "COMMAND", "DBSIZE"}


class ShardConnection:
    def __init__(self, address: str):
        host, port = address.rsplit(":", 1)
        self.host, self.port = host, int(port)
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None

    async def ensure_connected(self) -> None:
        if self.writer is None or self.writer.is_closing():
            self.reader, self.writer = await asyncio.open_connection(self.host, self.port)

    async def call(self, argv: list[bytes]) -> bytes:
        await self.ensure_connected()
        self.writer.write(encode_command(argv))
        await self.writer.drain()
        return await read_raw_reply(self.reader)


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                         ring: ConsistentHashRing, shard_conns: dict[str, ShardConnection]) -> None:
    try:
        while True:
            argv = await read_command(reader)
            if argv is None:
                break
            if not argv:
                continue
            name = argv[0].decode("utf-8", "replace").upper()

            if name in UNROUTABLE:
                writer.write(b"-ERR command not supported by shard_proxy "
                              b"(see tools/shard_proxy.py module docstring for scope)\r\n")
                await writer.drain()
                continue

            if name in NO_KEY_COMMANDS:
                target = next(iter(shard_conns))
            elif len(argv) < 2:
                writer.write(b"-ERR shard_proxy: this command needs a key as its first argument\r\n")
                await writer.drain()
                continue
            else:
                key = argv[1].decode("utf-8", "surrogateescape")
                target = ring.get_node(key)

            try:
                reply = await shard_conns[target].call(argv)
            except (ConnectionError, OSError) as e:
                writer.write(f"-ERR shard {target} unreachable: {e}\r\n".encode())
                await writer.drain()
                continue

            writer.write(reply)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()


async def main_async(args: argparse.Namespace) -> None:
    ring = ConsistentHashRing(args.shard)
    shard_conns = {addr: ShardConnection(addr) for addr in args.shard}

    async def on_connect(reader, writer):
        await handle_client(reader, writer, ring, shard_conns)

    server = await asyncio.start_server(on_connect, args.listen_host, args.listen_port)
    print(f"shard_proxy listening on {args.listen_host}:{args.listen_port}")
    print(f"routing across {len(args.shard)} shards: {', '.join(args.shard)}")
    print("NOTE: single-key commands only — see this file's module docstring for full scope.")
    async with server:
        await server.serve_forever()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--listen-host", default="127.0.0.1")
    p.add_argument("--listen-port", type=int, default=8000)
    p.add_argument("--shard", action="append", required=True, help="host:port of a backend instance, repeatable")
    args = p.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
