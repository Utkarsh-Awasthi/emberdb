#!/usr/bin/env python3
"""Interactive RESP client, standalone (works against any RESP server)."""

from __future__ import annotations

import argparse
import shlex
import socket
import sys

try:
    import readline  # noqa: F401 — importing it is enough to get history/editing in input()
except ImportError:
    pass  # not available on some platforms (e.g. stock Windows Python); the REPL still works


class RespCliError(Exception):
    pass


def encode_command(argv: list[str]) -> bytes:
    out = bytearray(f"*{len(argv)}\r\n".encode())
    for arg in argv:
        b = arg.encode("utf-8", "surrogateescape")
        out += f"${len(b)}\r\n".encode() + b + b"\r\n"
    return bytes(out)


def read_reply(f):
    line = f.readline()
    if not line:
        raise ConnectionError("server closed the connection")
    tag, rest = line[0:1], line[1:-2]

    if tag == b"+":
        return rest.decode("utf-8", "replace")
    if tag == b"-":
        return RespCliError(rest.decode("utf-8", "replace"))
    if tag == b":":
        return int(rest)
    if tag == b",":
        return float(rest)
    if tag == b"#":
        return rest == b"t"
    if tag == b"_":
        return None
    if tag == b"$" or tag == b"=":
        n = int(rest.split(b":")[0] if tag == b"=" else rest)
        if n == -1:
            return None
        data = f.read(n + 2)[:-2]
        return data.decode("utf-8", "surrogateescape")
    if tag in (b"*", b">", b"~"):
        n = int(rest)
        if n == -1:
            return None
        return [read_reply(f) for _ in range(n)]
    if tag == b"%":
        n = int(rest)
        return {read_reply(f): read_reply(f) for _ in range(n)}
    raise RespCliError(f"unrecognized RESP reply tag: {tag!r}")


def format_reply(reply, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(reply, RespCliError):
        return f"(error) {reply}"
    if reply is None:
        return "(nil)"
    if isinstance(reply, bool):
        return "(true)" if reply else "(false)"
    if isinstance(reply, int):
        return f"(integer) {reply}"
    if isinstance(reply, float):
        return f"(double) {reply}"
    if isinstance(reply, str):
        return f'"{reply}"'
    if isinstance(reply, list):
        if not reply:
            return "(empty array)"
        lines = [f"{pad}{i + 1}) {format_reply(v, indent + 1)}" for i, v in enumerate(reply)]
        return "\n".join(lines)
    if isinstance(reply, dict):
        if not reply:
            return "(empty map)"
        lines = []
        for i, (k, v) in enumerate(reply.items()):
            lines.append(f"{pad}{i + 1}) {format_reply(k, indent + 1)}")
            lines.append(f"{pad}   {format_reply(v, indent + 1)}")
        return "\n".join(lines)
    return str(reply)


class EmberCliSession:
    def __init__(self, host: str, port: int):
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.settimeout(None)
        self.f = self.sock.makefile("rb")

    def call(self, argv: list[str]):
        self.sock.sendall(encode_command(argv))
        return read_reply(self.f)

    def close(self) -> None:
        try:
            self.f.close()
            self.sock.close()
        except OSError:
            pass


def tokenize(line: str) -> list[str]:
    try:
        return shlex.split(line)
    except ValueError:
        return line.split()  # unbalanced quotes: fall back to naive whitespace split rather than crash


def run_repl(session: EmberCliSession, host: str, port: int, db: int) -> None:
    prompt_db = f"[{db}]" if db else ""
    print(f"EmberDB CLI connected to {host}:{port}{prompt_db}. Ctrl-D or 'quit' to exit.")
    while True:
        try:
            line = input(f"{host}:{port}{prompt_db}> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        argv = tokenize(line)
        if not argv:
            continue
        if argv[0].upper() in ("QUIT", "EXIT"):
            break
        try:
            reply = session.call(argv)
        except (ConnectionError, OSError) as e:
            print(f"connection error: {e}")
            break
        if argv[0].upper() == "SELECT" and not isinstance(reply, RespCliError):
            try:
                db = int(argv[1])
                prompt_db = f"[{db}]" if db else ""
            except (IndexError, ValueError):
                pass
        print(format_reply(reply))


def main() -> None:
    p = argparse.ArgumentParser(prog="ember_cli", description="Interactive RESP client for EmberDB")
    p.add_argument("-H", "--host", default="127.0.0.1")
    p.add_argument("-p", "--port", type=int, default=7379)
    p.add_argument("-a", "--password", default=None)
    p.add_argument("-n", "--db", type=int, default=0)
    p.add_argument("command", nargs="*", help="run one command and exit instead of starting the REPL")
    args = p.parse_args()

    session = EmberCliSession(args.host, args.port)
    try:
        if args.password:
            reply = session.call(["AUTH", args.password])
            if isinstance(reply, RespCliError):
                print(f"(error) {reply}", file=sys.stderr)
                sys.exit(1)
        if args.db:
            session.call(["SELECT", str(args.db)])

        if args.command:
            reply = session.call(args.command)
            print(format_reply(reply))
            sys.exit(1 if isinstance(reply, RespCliError) else 0)

        if not sys.stdin.isatty():
            for line in sys.stdin:
                argv = tokenize(line)
                if not argv:
                    continue
                print(format_reply(session.call(argv)))
            return

        run_repl(session, args.host, args.port, args.db)
    finally:
        session.close()


if __name__ == "__main__":
    main()
