"""Leader-follower replication: full resync + live command streaming."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Any, Optional, TYPE_CHECKING

from .persistence import load_snapshot, save_snapshot
from .protocol import encode_command, read_command

if TYPE_CHECKING:
    from .engine import EmberEngine

logger = logging.getLogger("emberdb.replication")


class ReplicationProtocolError(Exception):
    pass


class ReplicationManager:
    def __init__(self, engine: "EmberEngine"):
        self.engine = engine
        self.role = "master"
        self.replicas: dict[int, Any] = {}  # id(conn) -> conn (needs .writer, .addr)
        self._replica_last_db: dict[int, Optional[int]] = {}

        # follower-side state
        self.leader_host: Optional[str] = None
        self.leader_port: Optional[int] = None
        self.link_status = "down"  # "down" | "connecting" | "up"
        self._replica_task: Optional[asyncio.Task] = None

    # -- leader side: replicas attached to us --------------------------------

    def add_replica(self, conn) -> None:
        self.replicas[id(conn)] = conn
        self._replica_last_db[id(conn)] = None
        logger.info("replica attached: %s", getattr(conn, "addr", "?"))

    def remove_replica(self, conn) -> None:
        if self.replicas.pop(id(conn), None) is not None:
            logger.info("replica detached: %s", getattr(conn, "addr", "?"))
        self._replica_last_db.pop(id(conn), None)

    def propagate(self, argv: list[str], db_index: int) -> None:
        if not self.replicas:
            return
        for conn in list(self.replicas.values()):
            try:
                if self._replica_last_db.get(id(conn)) != db_index:
                    conn.writer.write(encode_command(["SELECT", str(db_index)]))
                    self._replica_last_db[id(conn)] = db_index
                conn.writer.write(encode_command(argv))
            except Exception:
                logger.warning("propagation to replica %s failed, dropping it", getattr(conn, "addr", "?"))
                self.remove_replica(conn)

    def full_resync_payload(self) -> bytes:
        """Serialize the whole dataset via persistence.py's own snapshot
        format (a temp file, read back into memory) — reusing the exact
        same code path SAVE/BGSAVE use, so there is only ever one
        implementation of 'what a full dataset dump looks like'."""
        fd, path = tempfile.mkstemp(prefix="emberdb-fullresync-", suffix=".json")
        os.close(fd)
        try:
            save_snapshot(path, self.engine.databases)
            with open(path, "rb") as f:
                return f.read()
        finally:
            os.remove(path)

    # -- follower side: we are replicating FROM another instance ------------

    def become_leader(self) -> None:
        if self._replica_task is not None:
            self._replica_task.cancel()
            self._replica_task = None
        self.role = "master"
        self.leader_host = self.leader_port = None
        self.link_status = "down"

    def replicate_from(self, host: str, port: int) -> None:
        if self._replica_task is not None:
            self._replica_task.cancel()
        self.role = "replica"
        self.leader_host, self.leader_port = host, port
        self.link_status = "connecting"
        self._replica_task = asyncio.create_task(
            self._replica_loop(host, port), name=f"replica-of-{host}:{port}"
        )

    async def _replica_loop(self, host: str, port: int) -> None:
        from .engine import InternalConnState  # deferred: avoids a module-load-time cycle with engine.py
        internal_conn = InternalConnState()
        while self.role == "replica":
            try:
                reader, writer = await asyncio.open_connection(host, port)
                writer.write(encode_command(["SYNC"]))
                await writer.drain()

                snapshot_bytes = await _read_bulk_string(reader)
                fd, path = tempfile.mkstemp(prefix="emberdb-resync-", suffix=".json")
                os.close(fd)
                try:
                    with open(path, "wb") as f:
                        f.write(snapshot_bytes)
                    for db in self.engine.databases:
                        db.flush()
                    loaded = load_snapshot(path, self.engine.databases)
                finally:
                    os.remove(path)

                self.link_status = "up"
                logger.info("full resync from %s:%s complete (%d keys)", host, port, loaded)

                while True:
                    raw_argv = await read_command(reader)
                    if raw_argv is None:
                        raise ConnectionError("leader closed the replication connection")
                    if not raw_argv:
                        continue
                    argv = [a.decode("utf-8", "surrogateescape") for a in raw_argv]
                    try:
                        self.engine.execute(internal_conn, internal_conn.selected_db, argv, is_replay=True)
                    except Exception:
                        logger.exception("failed to apply replicated command: %r", argv)
            except asyncio.CancelledError:
                self.link_status = "down"
                return
            except (OSError, ConnectionError, ReplicationProtocolError) as e:
                self.link_status = "down"
                logger.warning("replication link to %s:%s dropped (%s), retrying in 2s", host, port, e)
                await asyncio.sleep(2.0)


async def _read_bulk_string(reader: asyncio.StreamReader) -> bytes:
    """Read exactly one RESP bulk string off the wire. Used only for the
    full-resync payload: unlike every other message on a replication
    connection, that one is a raw dataset dump, not a command, so we
    bypass read_command() and parse the bulk-string framing directly."""
    header = await reader.readline()
    if not header.startswith(b"$"):
        raise ReplicationProtocolError(f"expected bulk string header for full resync, got {header!r}")
    length = int(header[1:].strip())
    data = await reader.readexactly(length)
    await reader.readexactly(2)  # trailing CRLF
    return data
