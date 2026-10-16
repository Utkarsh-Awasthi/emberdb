#!/usr/bin/env python3
"""Throughput/latency benchmark for any RESP server."""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import string
import time


def encode_command(argv: list[str]) -> bytes:
    out = bytearray(f"*{len(argv)}\r\n".encode())
    for arg in argv:
        b = arg.encode("utf-8", "surrogateescape")
        out += f"${len(b)}\r\n".encode() + b + b"\r\n"
    return bytes(out)


async def read_reply(reader: asyncio.StreamReader) -> bytes:
    line = await reader.readline()
    if not line:
        raise ConnectionError("server closed the connection")
    tag = line[0:1]
    if tag in (b"+", b"-", b":", b"_", b"#"):
        return line
    if tag == b"$":
        n = int(line[1:-2])
        return line if n == -1 else line + await reader.readexactly(n + 2)
    if tag in (b"*", b">"):
        n = int(line[1:-2])
        if n == -1:
            return line
        out = line
        for _ in range(n):
            out += await read_reply(reader)
        return out
    if tag == b"%":
        n = int(line[1:-2])
        out = line
        for _ in range(n * 2):
            out += await read_reply(reader)
        return out
    return line


def _random_value(size: int) -> str:
    return "".join(random.choices(string.ascii_letters, k=size))


def _cmd_set(key: str, value_size: int) -> list[str]:
    return ["SET", key, _random_value(value_size)]


def _cmd_get(key: str, value_size: int) -> list[str]:
    return ["GET", key]


def _cmd_incr(key: str, value_size: int) -> list[str]:
    return ["INCR", key]


def _cmd_hset(key: str, value_size: int) -> list[str]:
    return ["HSET", key, "field", _random_value(value_size)]


# each command type gets its own key namespace below, so e.g. INCR never
# lands on a key SET last wrote a non-numeric string into
COMMAND_BUILDERS = {"set": _cmd_set, "get": _cmd_get, "incr": _cmd_incr, "hset": _cmd_hset}


async def worker(host: str, port: int, builders: list, ops: int, key_space: int,
                  value_size: int, latencies: list, errors: list) -> None:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        for _ in range(ops):
            name, builder = random.choice(builders)
            # get/set share a namespace (read-back); incr/hset get their own
            shared_types = {"get", "set"}
            namespace = "rw" if name in shared_types else name
            key = f"bench:{namespace}:{random.randint(0, key_space - 1)}"
            argv = builder(key, value_size)
            start = time.perf_counter()
            writer.write(encode_command(argv))
            await writer.drain()
            reply = await read_reply(reader)
            latencies.append(time.perf_counter() - start)
            if reply.startswith(b"-"):
                errors[0] += 1
    finally:
        writer.close()


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(int(len(sorted_values) * p), len(sorted_values) - 1)
    return sorted_values[idx] * 1000


async def run(args: argparse.Namespace) -> None:
    builders = [(t.strip(), COMMAND_BUILDERS[t.strip()]) for t in args.types.split(",")]
    ops_per_worker = max(1, args.requests // args.clients)
    per_worker_latencies: list[list[float]] = [[] for _ in range(args.clients)]
    errors = [0]

    print(f"Connecting {args.clients} clients to {args.host}:{args.port} "
          f"({ops_per_worker * args.clients} total requests, commands: {args.types})...")

    start = time.perf_counter()
    await asyncio.gather(*[
        worker(args.host, args.port, builders, ops_per_worker, args.keyspace, args.value_size,
               per_worker_latencies[i], errors)
        for i in range(args.clients)
    ])
    elapsed = time.perf_counter() - start

    latencies = sorted(l for worker_lats in per_worker_latencies for l in worker_lats)
    total_ops = len(latencies)

    print()
    print("=" * 56)
    print(f"  EmberDB / RESP benchmark — {args.host}:{args.port}")
    print("=" * 56)
    print(f"  commands:       {args.types}")
    print(f"  total requests: {total_ops}")
    print(f"  concurrency:    {args.clients}")
    print(f"  errors:         {errors[0]}")
    print(f"  total time:     {elapsed:.2f}s")
    print(f"  throughput:     {total_ops / elapsed:,.0f} ops/sec")
    if latencies:
        print(f"  latency (ms):   min={latencies[0] * 1000:.3f}  "
              f"avg={statistics.mean(latencies) * 1000:.3f}  "
              f"p50={_percentile(latencies, 0.50):.3f}  "
              f"p95={_percentile(latencies, 0.95):.3f}  "
              f"p99={_percentile(latencies, 0.99):.3f}  "
              f"max={latencies[-1] * 1000:.3f}")
    print("=" * 56)
    print("Note: pure-Python single-loop server — see this file's module docstring before comparing to Redis.")


def main() -> None:
    p = argparse.ArgumentParser(description="Throughput/latency benchmark for EmberDB or any RESP server")
    p.add_argument("-H", "--host", default="127.0.0.1")
    p.add_argument("-p", "--port", type=int, default=7379)
    p.add_argument("-n", "--requests", type=int, default=10000)
    p.add_argument("-c", "--clients", type=int, default=20)
    p.add_argument("-t", "--types", default="set,get", help="comma-separated: set,get,incr,hset")
    p.add_argument("--keyspace", type=int, default=1000, help="distinct keys to spread load across")
    p.add_argument("--value-size", type=int, default=64)
    args = p.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
