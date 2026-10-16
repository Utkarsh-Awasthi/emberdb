import asyncio
import base64
import hashlib
import json
import os
import struct

from emberdb.engine import EmberEngine
from emberdb.http_api import WS_MAGIC, serve_http

_next_port = [17700]


def _alloc_port() -> int:
    _next_port[0] += 1
    return _next_port[0]


class HttpHarness:
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
        self.server = await serve_http(self.engine, "127.0.0.1", self.port)
        return self

    async def request(self, method, path, body=None, headers=None):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.connections.append(writer)
        body_bytes = json.dumps(body).encode() if body is not None else b""
        hdrs = dict(headers or {})
        if body is not None:
            hdrs["Content-Type"] = "application/json"
            hdrs["Content-Length"] = str(len(body_bytes))
        req = f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n" + "".join(
            f"{k}: {v}\r\n" for k, v in hdrs.items()) + "\r\n"
        writer.write(req.encode() + body_bytes)
        await writer.drain()

        status = int((await reader.readline()).split(b" ")[1])
        resp_headers = {}
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b""):
                break
            k, _, v = line.decode().partition(":")
            resp_headers[k.strip().lower()] = v.strip()
        resp_body = b""
        if "content-length" in resp_headers:
            resp_body = await reader.readexactly(int(resp_headers["content-length"]))
        return status, resp_headers, resp_body

    async def get_json(self, path, headers=None):
        status, _, body = await self.request("GET", path, headers=headers)
        return status, (json.loads(body) if body else None)

    async def post_json(self, path, payload, headers=None):
        status, _, body = await self.request("POST", path, body=payload, headers=headers)
        return status, (json.loads(body) if body else None)

    async def ws_connect(self, path):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.connections.append(writer)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        writer.write(req.encode())
        await writer.drain()
        status_line = await reader.readline()
        assert b"101" in status_line, status_line
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b""):
                break
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


async def ws_read_frame(reader):
    b0, b1 = await reader.readexactly(2)
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack(">H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", await reader.readexactly(8))[0]
    payload = await reader.readexactly(length)
    return b0 & 0x0F, payload


def test_static_file_serving(tmp_path):
    async def body():
        async with HttpHarness(tmp_path) as h:
            status, headers, page = await h.request("GET", "/")
            assert status == 200 and b"EmberDB" in page
            assert "text/html" in headers["content-type"]

            status, _, _ = await h.request("GET", "/does-not-exist")
            assert status == 404

    asyncio.run(body())


def test_command_endpoint_success_and_error(tmp_path):
    async def body():
        async with HttpHarness(tmp_path) as h:
            status, res = await h.post_json("/api/v1/command", {"db": 0, "argv": ["SET", "k", "v"]})
            assert status == 200 and res["reply"] == "OK"

            status, res = await h.post_json("/api/v1/command", {"db": 0, "argv": ["RPUSH", "k", "x"]})
            assert status == 400 and "WRONGTYPE" in res["error"]

    asyncio.run(body())


def test_stats_endpoint(tmp_path):
    async def body():
        async with HttpHarness(tmp_path) as h:
            await h.post_json("/api/v1/command", {"db": 0, "argv": ["SET", "k", "v"]})
            status, stats = await h.get_json("/api/v1/stats")
            assert status == 200
            assert "metrics" in stats and "replication" in stats

    asyncio.run(body())


def test_keyspace_and_heatmap_endpoints(tmp_path):
    async def body():
        async with HttpHarness(tmp_path) as h:
            await h.post_json("/api/v1/command", {"db": 0, "argv": ["SET", "dash:1", "v"]})
            await h.post_json("/api/v1/command", {"db": 0, "argv": ["GET", "dash:1"]})

            status, ks = await h.get_json("/api/v1/keyspace?db=0&pattern=dash:*")
            assert status == 200
            entry = next(e for e in ks["keys"] if e["key"] == "dash:1")
            assert entry["access_count"] >= 1

            status, hm = await h.get_json("/api/v1/heatmap?db=0")
            assert status == 200 and len(hm["cells"]) >= 1

    asyncio.run(body())


def test_key_detail_endpoint(tmp_path):
    async def body():
        async with HttpHarness(tmp_path) as h:
            await h.post_json("/api/v1/command", {"db": 0, "argv": ["HSET", "h", "f", "v"]})
            status, detail = await h.get_json("/api/v1/keys/h?db=0")
            assert status == 200 and detail["found"] and detail["type"] == "hash"

            status, missing = await h.get_json("/api/v1/keys/nosuch?db=0")
            assert status == 200 and missing["found"] is False

    asyncio.run(body())


def test_bearer_token_auth_gating(tmp_path):
    async def body():
        async with HttpHarness(tmp_path) as h:
            h.engine.acl.set_password("default", "s3cret")
            status, _ = await h.get_json("/api/v1/stats")
            assert status == 401
            status, _ = await h.get_json("/api/v1/stats", headers={"Authorization": "Bearer wrong"})
            assert status == 401
            status, stats = await h.get_json("/api/v1/stats", headers={"Authorization": "Bearer s3cret"})
            assert status == 200 and "metrics" in stats

    asyncio.run(body())


def test_cors_preflight(tmp_path):
    async def body():
        async with HttpHarness(tmp_path) as h:
            status, headers, _ = await h.request("OPTIONS", "/api/v1/stats")
            assert status == 204
            assert headers.get("access-control-allow-origin") == "*"

    asyncio.run(body())


def test_websocket_live_tick_and_cdc_push(tmp_path):
    async def body():
        async with HttpHarness(tmp_path) as h:
            reader, writer = await h.ws_connect("/ws")
            opcode, payload = await asyncio.wait_for(ws_read_frame(reader), timeout=3.0)
            assert opcode == 0x1
            msg = json.loads(payload)
            assert msg["type"] == "tick" and "stats" in msg

            await h.post_json("/api/v1/command", {"db": 0, "argv": ["SET", "ws-trigger", "v"]})
            saw_event = False
            deadline = asyncio.get_event_loop().time() + 3.0
            while asyncio.get_event_loop().time() < deadline:
                _, payload = await asyncio.wait_for(ws_read_frame(reader), timeout=3.0)
                msg = json.loads(payload)
                if any(e.get("key") == "ws-trigger" for e in msg.get("cdc_events", [])):
                    saw_event = True
                    break
            assert saw_event

    asyncio.run(body())
