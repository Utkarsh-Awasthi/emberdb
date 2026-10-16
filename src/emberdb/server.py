"""asyncio RESP TCP server."""

from __future__ import annotations

import asyncio
import logging
import time

from .engine import EmberEngine
from .protocol import (
    ProtocolError,
    ReplicaSyncStart,
    RespError,
    encode_reply,
    read_command,
)

logger = logging.getLogger("emberdb.server")

# Commands allowed to run before authentication succeeds, mirrored here only
# for the pre-flight check in _handle_connection's error logging — the real
# enforcement is CommandSpec.no_auth_required, checked inside engine.execute().


class ClientConnection:
    """Everything commands/*.py handlers can read or mutate through
    `ctx.conn_state`, plus the raw reader/writer this server needs to
    actually move bytes."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, engine: EmberEngine):
        self.reader = reader
        self.writer = writer
        self.engine = engine

        peer = writer.get_extra_info("peername")
        self.addr = f"{peer[0]}:{peer[1]}" if peer else "unknown"

        self.client_id = 0  # assigned by engine.register_client()
        self.name = ""
        self.selected_db = 0
        self.protocol_version = 2
        self.username = "default"
        self.authenticated = not engine.acl.needs_auth_by_default()
        self.connected_at = time.time()

        self.in_multi = False
        self.multi_queue: list[list[str]] = []
        self.multi_error = False
        self.watched: dict = {}

        self.channels: set = set()
        self.patterns: set = set()

        self.cdc_tail_callback = None
        self.cdc_tail_active = False
        self.lib_info: dict = {}

        self.is_replica_sink = False
        self.should_close = False

    # -- Subscriber protocol (pubsub.py) & CDC push target -------------------

    def deliver(self, channel: str, message: str) -> None:
        from .protocol import RespPush
        self._push(RespPush(["message", channel, message]))

    def deliver_cdc_event(self, event: dict) -> None:
        from .protocol import RespPush
        self._push(RespPush(["cdc", event]))

    def _push(self, value) -> None:
        try:
            self.writer.write(encode_reply(value, self.protocol_version))
        except Exception:
            logger.debug("failed to push to %s (likely already disconnected)", self.addr)


async def _handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, engine: EmberEngine) -> None:
    conn = ClientConnection(reader, writer, engine)
    engine.register_client(conn)
    logger.info("client connected: id=%d addr=%s", conn.client_id, conn.addr)

    try:
        while True:
            try:
                raw_argv = await read_command(reader)
            except ProtocolError as e:
                logger.warning("protocol error from %s: %s — closing connection", conn.addr, e)
                break
            except asyncio.IncompleteReadError:
                break  # peer disconnected mid-frame

            if raw_argv is None:
                break  # clean EOF
            if not raw_argv:
                continue  # blank inline line — tolerated, nothing to do

            argv = [a.decode("utf-8", "surrogateescape") for a in raw_argv]

            try:
                reply = engine.dispatch_from_client(conn, argv)
            except RespError as e:
                writer.write(encode_reply(e, conn.protocol_version))
                await writer.drain()
                continue
            except Exception:
                logger.exception("unexpected error handling command %r from %s", argv, conn.addr)
                writer.write(encode_reply(RespError("internal error"), conn.protocol_version))
                await writer.drain()
                continue

            if isinstance(reply, ReplicaSyncStart):
                await _send_full_resync(writer, engine)
                conn.is_replica_sink = True
                continue

            writer.write(encode_reply(reply, conn.protocol_version))
            await writer.drain()

            if conn.should_close:
                break
    finally:
        engine.unregister_client(conn)
        logger.info("client disconnected: id=%d addr=%s", conn.client_id, conn.addr)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def _send_full_resync(writer: asyncio.StreamWriter, engine: EmberEngine) -> None:
    payload = engine.replication.full_resync_payload()
    writer.write(f"${len(payload)}\r\n".encode() + payload + b"\r\n")
    await writer.drain()


async def serve_resp(engine: EmberEngine, host: str, port: int) -> asyncio.AbstractServer:
    """Start the RESP TCP listener. Returns the running asyncio.Server so
    the caller (__main__.py) can hold a reference for graceful shutdown."""

    async def _on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handle_connection(reader, writer, engine)

    server = await asyncio.start_server(_on_connect, host, port)
    addrs = ", ".join(str(sock.getsockname()) for sock in server.sockets)
    logger.info("EmberDB RESP server listening on %s", addrs)
    return server
