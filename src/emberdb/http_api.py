"""Hand-rolled HTTP/1.1 + WebSocket server: REST API and dashboard, no external deps."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import struct
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from .engine import EmberEngine, InternalConnState
from .protocol import RespError

logger = logging.getLogger("emberdb.http")

DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard")
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # fixed GUID from RFC 6455 itself

# Tracked so shutdown can force-close them (Server.wait_closed() waits for
# every connection to finish, and a WebSocket stays open indefinitely).
_active_websockets: set = set()


def close_all_websockets() -> None:
    for writer in list(_active_websockets):
        try:
            writer.close()
        except Exception:
            pass


# -- HTTP/1.1 parsing --

class HttpRequest:
    def __init__(self, method: str, path: str, query: dict, headers: dict, body: bytes):
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body

    def json_body(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("request body is not valid JSON")


async def _read_http_request(reader: asyncio.StreamReader) -> Optional[HttpRequest]:
    request_line = await reader.readline()
    if not request_line:
        return None  # peer closed before sending anything
    try:
        method, target, _version = request_line.decode("latin-1").strip().split(" ", 2)
    except ValueError:
        raise ValueError(f"malformed request line: {request_line!r}")

    headers: dict = {}
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        key, _, value = line.decode("latin-1").partition(":")
        headers[key.strip().lower()] = value.strip()

    body = b""
    content_length = headers.get("content-length")
    if content_length:
        body = await reader.readexactly(int(content_length))

    split = urlsplit(target)
    query = {k: v[0] for k, v in parse_qs(split.query).items()}
    return HttpRequest(method.upper(), split.path, query, headers, body)


_STATUS_TEXT = {
    200: "OK", 101: "Switching Protocols", 204: "No Content",
    400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
    405: "Method Not Allowed", 500: "Internal Server Error",
}


def _send_http_response(writer: asyncio.StreamWriter, status: int, body: bytes = b"",
                         content_type: str = "text/plain; charset=utf-8",
                         extra_headers: Optional[dict] = None) -> None:
    reason = _STATUS_TEXT.get(status, "OK")
    headers = [
        f"HTTP/1.1 {status} {reason}",
        f"Content-Type: {content_type}",
        f"Content-Length: {len(body)}",
        "Connection: close",
        "Access-Control-Allow-Origin: *",
    ]
    for k, v in (extra_headers or {}).items():
        headers.append(f"{k}: {v}")
    writer.write(("\r\n".join(headers) + "\r\n\r\n").encode("latin-1") + body)


def _json_response(writer: asyncio.StreamWriter, status: int, payload: Any) -> None:
    body = json.dumps(payload, default=str).encode("utf-8")
    _send_http_response(writer, status, body, content_type="application/json")


# -- WebSocket handshake + frame codec --

def _websocket_accept_key(client_key: str) -> str:
    digest = hashlib.sha1((client_key + WS_MAGIC).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def ws_encode_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """Encode one server->client frame (unmasked, per RFC 6455)."""
    header = bytearray()
    header.append(0x80 | opcode)  # FIN=1, single frame
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    return bytes(header) + payload


class WebSocketClosed(Exception):
    pass


async def ws_read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    """Decode one client->server frame. Rejects unmasked frames (spec requires masking)."""
    first2 = await reader.readexactly(2)
    b0, b1 = first2[0], first2[1]
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack(">H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", await reader.readexactly(8))[0]
    if not masked:
        raise WebSocketClosed("client frame was not masked (protocol violation)")
    mask_key = await reader.readexactly(4)
    masked_payload = await reader.readexactly(length)
    payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(masked_payload))
    if opcode == 0x8:  # close
        raise WebSocketClosed("client sent a close frame")
    return opcode, payload


async def _websocket_handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                                req: HttpRequest) -> bool:
    key = req.headers.get("sec-websocket-key")
    if req.headers.get("upgrade", "").lower() != "websocket" or not key:
        _send_http_response(writer, 400, b"expected a WebSocket upgrade request")
        return False
    accept = _websocket_accept_key(key)
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    )
    writer.write(response.encode("latin-1"))
    await writer.drain()
    return True


# -- auth: bearer token, same password as RESP --

def _http_authorized(engine: EmberEngine, req: HttpRequest) -> bool:
    if not engine.acl.needs_auth_by_default():
        return True
    auth = req.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else req.query.get("token", "")
    return engine.acl.authenticate("default", token)


# -- routing --

def _http_conn() -> InternalConnState:
    return InternalConnState()


async def _handle_request(req: HttpRequest, reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter, engine: EmberEngine) -> None:
    if req.method == "OPTIONS":
        _send_http_response(writer, 204, extra_headers={
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
        })
        return

    if req.path == "/ws":
        if not _http_authorized(engine, req):
            _send_http_response(writer, 401, b"unauthorized")
            return
        if await _websocket_handshake(reader, writer, req):
            await _run_websocket_session(reader, writer, engine)
        return

    if req.path.startswith("/api/"):
        if not _http_authorized(engine, req):
            _json_response(writer, 401, {"error": "unauthorized"})
            return
        await _handle_api(req, writer, engine)
        return

    await _serve_static(req, writer)


async def _serve_static(req: HttpRequest, writer: asyncio.StreamWriter) -> None:
    path = req.path if req.path != "/" else "/index.html"
    safe_name = os.path.basename(path)  # deliberately flat: no subdirectories, no ../ traversal surface
    fs_path = os.path.join(DASHBOARD_DIR, safe_name)
    if not os.path.isfile(fs_path):
        _send_http_response(writer, 404, b"not found")
        return
    content_type, _ = mimetypes.guess_type(fs_path)
    with open(fs_path, "rb") as f:
        body = f.read()
    _send_http_response(writer, 200, body, content_type=content_type or "application/octet-stream")


async def _handle_api(req: HttpRequest, writer: asyncio.StreamWriter, engine: EmberEngine) -> None:
    try:
        if req.path == "/api/v1/stats" and req.method == "GET":
            _json_response(writer, 200, _build_stats(engine))
            return

        if req.path == "/api/v1/keyspace" and req.method == "GET":
            db_index = int(req.query.get("db", "0"))
            pattern = req.query.get("pattern", "*")
            cursor = int(req.query.get("cursor", "0"))
            count = int(req.query.get("count", "50"))
            result = engine.execute(_http_conn(), db_index, ["SCAN", str(cursor), "MATCH", pattern, "COUNT", str(count)])
            next_cursor, keys = result.reply
            entries = []
            db = engine.databases[db_index]
            now = time.time()
            for k in keys:
                meta = db.meta.get(k)
                entries.append({
                    "key": k, "type": _type_of(db, k),
                    "ttl": db.ttl_seconds(k),
                    "last_access_seconds_ago": (now - meta.last_access) if meta else None,
                    "access_count": meta.access_count if meta else 0,
                })
            _json_response(writer, 200, {"cursor": next_cursor, "keys": entries})
            return

        if req.path == "/api/v1/heatmap" and req.method == "GET":
            db_index = int(req.query.get("db", "0"))
            limit = int(req.query.get("limit", "200"))
            _json_response(writer, 200, {"cells": _build_heatmap(engine, db_index, limit)})
            return

        if req.path.startswith("/api/v1/keys/") and req.method == "GET":
            key = req.path[len("/api/v1/keys/"):]
            db_index = int(req.query.get("db", "0"))
            _json_response(writer, 200, _describe_key(engine, db_index, key))
            return

        if req.path == "/api/v1/command" and req.method == "POST":
            payload = req.json_body() or {}
            db_index = int(payload.get("db", 0))
            argv = payload.get("argv")
            if not isinstance(argv, list) or not argv:
                _json_response(writer, 400, {"error": "body must be {'db': int, 'argv': [str, ...]}"})
                return
            result = engine.execute(_http_conn(), db_index, [str(a) for a in argv])
            _json_response(writer, 200, {"reply": _jsonify(result.reply)})
            return

        if req.path == "/api/v1/cdc" and req.method == "GET":
            offset = int(req.query.get("offset", "0"))
            limit = int(req.query.get("limit", "100"))
            events = engine.cdc.since(offset, limit)
            _json_response(writer, 200, {"events": [e.to_dict() for e in events]})
            return

        if req.path == "/api/v1/indexes" and req.method == "GET":
            _json_response(writer, 200, {"indexes": engine.indexes.list_indexes()})
            return

        if req.path == "/api/v1/branches" and req.method == "GET":
            _json_response(writer, 200, {
                "current": engine.branches.current,
                "branches": engine.branches.list_branches(),
                "diff": engine.branches.diff(),
            })
            return

        _json_response(writer, 404, {"error": "no such endpoint"})
    except RespError as e:
        _json_response(writer, 400, {"error": e.reply_text()})
    except (ValueError, KeyError, IndexError) as e:
        _json_response(writer, 400, {"error": str(e)})
    except Exception:
        logger.exception("unhandled error serving %s %s", req.method, req.path)
        _json_response(writer, 500, {"error": "internal error"})


def _jsonify(value: Any) -> Any:
    """Engine reply -> JSON-safe value: sets to lists, RESP wrappers unwrapped."""
    from .protocol import SimpleString, Verbatim
    if isinstance(value, (SimpleString, Verbatim)):
        return value.value
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, list):
        return [_jsonify(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonify(v) for k, v in value.items()}
    if isinstance(value, RespError):
        return {"error": value.reply_text()}
    return value


def _type_of(db, key: str) -> str:
    from .valuetypes import type_name_of
    return type_name_of(db.peek(key))


def _describe_key(engine: EmberEngine, db_index: int, key: str) -> dict:
    db = engine.databases[db_index]
    value = db.peek(key)
    if value is None:
        return {"key": key, "found": False}
    type_name = _type_of(db, key)
    conn = _http_conn()
    dump_by_type = {
        "string": lambda: engine.execute(conn, db_index, ["GET", key]).reply,
        "list": lambda: engine.execute(conn, db_index, ["LRANGE", key, "0", "-1"]).reply,
        "hash": lambda: engine.execute(conn, db_index, ["HGETALL", key]).reply,
        "set": lambda: sorted(engine.execute(conn, db_index, ["SMEMBERS", key]).reply),
        "zset": lambda: engine.execute(conn, db_index, ["ZRANGE", key, "0", "-1", "WITHSCORES"]).reply,
        "json": lambda: json.loads(engine.execute(conn, db_index, ["JSON.GET", key]).reply),
    }
    dump_fn = dump_by_type.get(type_name)
    return {
        "key": key, "found": True, "type": type_name,
        "ttl": db.ttl_seconds(key),
        "value": _jsonify(dump_fn()) if dump_fn else None,
    }


def _build_heatmap(engine: EmberEngine, db_index: int, limit: int) -> list[dict]:
    """Walk SCAN across the whole keyspace (up to `limit`), returning per-key access data."""
    db = engine.databases[db_index]
    now = time.time()
    cells = []
    cursor = 0
    conn = _http_conn()
    while len(cells) < limit:
        result = engine.execute(conn, db_index, ["SCAN", str(cursor), "COUNT", "200"])
        cursor, keys = result.reply
        for k in keys:
            meta = db.meta.get(k)
            if meta is None:
                continue
            cells.append({
                "key": k,
                "last_access_seconds_ago": now - meta.last_access,
                "access_count": meta.access_count,
            })
            if len(cells) >= limit:
                break
        if cursor == 0:
            break
    return cells


def _build_stats(engine: EmberEngine) -> dict:
    return {
        "metrics": engine.metrics.snapshot(),
        "eviction": engine.eviction.describe(),
        "replication": {
            "role": engine.replication.role,
            "connected_replicas": len(engine.replication.replicas),
            "link_status": engine.replication.link_status,
        },
        "keyspace": [
            {"db": i, "keys": db.size(), "expires": len(db.expires), "bytes": db.approx_bytes}
            for i, db in enumerate(engine.databases) if db.size() > 0
        ],
        "cdc": engine.cdc.status(),
        "branch": {
            "current": engine.branches.current,
            "count": len(engine.branches.meta),
            "dirty": len(engine.branches.diff()),
        },
        "vectors": engine.vectors.describe(),
        "hooks": engine.hooks.list_hooks(),
        "uptime_seconds": time.time() - engine.start_time,
        "version": engine.version,
    }


# -- WebSocket session: pushes stats + CDC tail --

async def _run_websocket_session(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                                  engine: EmberEngine) -> None:
    _active_websockets.add(writer)
    cdc_events: list[dict] = []

    def _on_cdc_event(event) -> None:
        cdc_events.append(event.to_dict())

    engine.cdc.tail_subscribe(_on_cdc_event)

    async def _reader_task() -> None:
        # drain frames; no client->server protocol beyond the handshake
        while True:
            await ws_read_frame(reader)

    reader_coro = asyncio.ensure_future(_reader_task())
    try:
        while True:
            payload = {"type": "tick", "stats": _build_stats(engine)}
            if cdc_events:
                payload["cdc_events"] = cdc_events[:50]
                cdc_events.clear()
            data = json.dumps(payload, default=str).encode("utf-8")
            writer.write(ws_encode_frame(data))
            await writer.drain()

            done, _pending = await asyncio.wait({reader_coro}, timeout=1.0)
            if reader_coro in done:
                reader_coro.result()  # re-raise WebSocketClosed / connection errors
    except (WebSocketClosed, ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        _active_websockets.discard(writer)
        engine.cdc.tail_unsubscribe(_on_cdc_event)
        reader_coro.cancel()


# -- entry point --

async def serve_http(engine: EmberEngine, host: str, port: int) -> asyncio.AbstractServer:
    async def _on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            req = await _read_http_request(reader)
            if req is not None:
                await _handle_request(req, reader, writer, engine)
        except (ValueError, asyncio.IncompleteReadError) as e:
            try:
                _send_http_response(writer, 400, str(e).encode())
            except Exception:
                pass
        except Exception:
            logger.exception("unhandled error in HTTP connection")
        finally:
            try:
                await writer.drain()
            except Exception:
                pass
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(_on_connect, host, port)
    addrs = ", ".join(str(sock.getsockname()) for sock in server.sockets)
    logger.info("EmberDB HTTP/dashboard server listening on %s", addrs)
    return server
