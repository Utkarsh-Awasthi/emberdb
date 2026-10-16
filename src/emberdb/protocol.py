"""RESP2/RESP3 wire codec. Only module that touches socket bytes directly."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Optional, Union


class ProtocolError(Exception):
    """Malformed RESP on the wire; fatal for the connection."""


@dataclass(frozen=True)
class SimpleString:
    """Force '+...\\r\\n' encoding instead of a bulk string."""
    value: str


@dataclass(frozen=True)
class Verbatim:
    """RESP3 verbatim string (INFO, etc.)."""
    value: str
    fmt: str = "txt"


@dataclass(frozen=True)
class RespPush:
    """RESP3 out-of-band push (pub/sub, CDC tail)."""
    items: list


@dataclass(frozen=True)
class MultiReply:
    """Several independent top-level replies from one command (e.g. SUBSCRIBE a b c)."""
    frames: list


@dataclass(frozen=True)
class ReplicaSyncStart:
    """Sentinel for SYNC; server.py streams the full-resync payload instead of encoding this."""


class RespError(Exception):
    """Command-level error -> '-CODE message\\r\\n'."""

    def __init__(self, message: str, code: str = "ERR"):
        self.code = code
        self.message = message
        super().__init__(f"{code} {message}")

    def reply_text(self) -> str:
        if self.code == "ERR":
            return f"ERR {self.message}"
        return f"{self.code} {self.message}"


CRLF = b"\r\n"


def encode_reply(value: Any, protocol_version: int = 2) -> bytes:
    """Encode one Python value as a RESP reply. RESP3-only types degrade to RESP2 equivalents."""
    out = bytearray()
    _encode_into(value, protocol_version, out)
    return bytes(out)


def _encode_into(value: Any, proto: int, out: bytearray) -> None:
    if isinstance(value, RespError):
        out += f"-{value.reply_text()}\r\n".encode("utf-8", "replace")
    elif isinstance(value, SimpleString):
        out += f"+{value.value}\r\n".encode("utf-8", "replace")
    elif isinstance(value, Verbatim):
        if proto >= 3:
            payload = value.value.encode("utf-8")
            out += f"={len(payload) + 4}\r\n{value.fmt}:".encode("utf-8") + payload + CRLF
        else:
            _encode_into(value.value, proto, out)
    elif isinstance(value, RespPush):
        marker = b">" if proto >= 3 else b"*"
        out += marker + str(len(value.items)).encode() + CRLF
        for item in value.items:
            _encode_into(item, proto, out)
    elif isinstance(value, MultiReply):
        for frame in value.frames:
            _encode_into(frame, proto, out)
    elif value is None:
        out += b"_\r\n" if proto >= 3 else b"$-1\r\n"
    elif isinstance(value, bool):
        # NOTE: must precede the `int` check — bool is an int subclass.
        if proto >= 3:
            out += b"#t\r\n" if value else b"#f\r\n"
        else:
            out += b":1\r\n" if value else b":0\r\n"
    elif isinstance(value, int):
        out += f":{value}\r\n".encode()
    elif isinstance(value, float):
        if proto >= 3:
            out += f",{_format_double(value)}\r\n".encode()
        else:
            _encode_bulk_string(_format_double(value).encode(), out)
    elif isinstance(value, (bytes, bytearray)):
        _encode_bulk_string(bytes(value), out)
    elif isinstance(value, str):
        _encode_bulk_string(value.encode("utf-8"), out)
    elif isinstance(value, dict):
        if proto >= 3:
            out += f"%{len(value)}\r\n".encode()
            for k, v in value.items():
                _encode_into(k, proto, out)
                _encode_into(v, proto, out)
        else:
            flat = []
            for k, v in value.items():
                flat.append(k)
                flat.append(v)
            _encode_array(flat, proto, out)
    elif isinstance(value, (set, frozenset)):
        items = list(value)
        if proto >= 3:
            out += f"~{len(items)}\r\n".encode()
            for item in items:
                _encode_into(item, proto, out)
        else:
            _encode_array(items, proto, out)
    elif isinstance(value, (list, tuple)):
        _encode_array(list(value), proto, out)
    else:
        # command handler returned something unexpected; stringify rather than crash
        _encode_bulk_string(str(value).encode("utf-8"), out)


def _encode_bulk_string(data: bytes, out: bytearray) -> None:
    out += f"${len(data)}\r\n".encode()
    out += data
    out += CRLF


def _encode_array(items: list, proto: int, out: bytearray) -> None:
    out += f"*{len(items)}\r\n".encode()
    for item in items:
        _encode_into(item, proto, out)


def _format_double(f: float) -> str:
    if f != f:  # NaN
        return "nan"
    if f == float("inf"):
        return "inf"
    if f == float("-inf"):
        return "-inf"
    if f == int(f) and abs(f) < 1e15:
        return str(int(f))
    return repr(f)


# -- decoding: bytes off the wire -> argv -------------------------------

async def read_command(reader: asyncio.StreamReader) -> Optional[list[bytes]]:
    """Read one command's argv. None on EOF; raises ProtocolError on bad input."""
    first = await reader.readline()
    if first == b"":
        return None
    if not first.endswith(b"\n"):
        raise ProtocolError("unexpected EOF")
    first = first.rstrip(b"\r\n")

    if not first:
        return []

    if first[0:1] == b"*":
        return await _read_multibulk(reader, first)
    else:
        return first.split()  # inline command (telnet/nc style)


async def _read_multibulk(reader: asyncio.StreamReader, header: bytes) -> list[bytes]:
    try:
        count = int(header[1:])
    except ValueError:
        raise ProtocolError(f"invalid multibulk length '{header[1:]!r}'")
    if count <= 0:
        return []
    if count > 1024 * 1024:
        raise ProtocolError("invalid multibulk length")

    args: list[bytes] = []
    for _ in range(count):
        line = await reader.readline()
        if not line.endswith(b"\n"):
            raise ProtocolError("unexpected EOF in multibulk")
        line = line.rstrip(b"\r\n")
        if line[0:1] != b"$":
            raise ProtocolError(f"expected '$', got '{line[:1]!r}'")
        try:
            length = int(line[1:])
        except ValueError:
            raise ProtocolError("invalid bulk length")
        if length == -1:
            args.append(b"")
            continue
        if length < 0 or length > 512 * 1024 * 1024:
            raise ProtocolError("invalid bulk length")
        payload = await reader.readexactly(length + 2)  # data + trailing CRLF
        args.append(payload[:-2])
    return args


def encode_command(argv: list[Union[str, bytes]]) -> bytes:
    """Encode an argv as a RESP request (for replication, the CLI, the shard proxy)."""
    out = bytearray()
    out += f"*{len(argv)}\r\n".encode()
    for a in argv:
        b = a.encode("utf-8") if isinstance(a, str) else bytes(a)
        _encode_bulk_string(b, out)
    return bytes(out)
