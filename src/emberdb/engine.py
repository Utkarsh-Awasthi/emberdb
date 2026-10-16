"""EmberEngine: central command dispatch and every subsystem it ties together."""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from . import __version__ as _EMBERDB_VERSION
from .acl import ACLManager
from .branching import BranchManager, MAIN_BRANCH
from .cdc import ChangeLog
from .commands import COMMAND_TABLE, CommandContext, check_arity
from .config import DEFAULT_CONFIG
from .database import Database
from .eviction import EvictionManager
from .expiry import ActiveExpirer
from .hooks import HookManager
from .indexing import IndexManager
from .metrics import Metrics
from .persistence import AOFWriter, load_snapshot, replay_aof, save_snapshot
from .protocol import RespError, SimpleString
from .pubsub import PubSubBroker
from .replication import ReplicationManager
from .vectorsearch import VectorSearchEngine

logger = logging.getLogger("emberdb.engine")

_CONTROL_COMMANDS_ALWAYS_IMMEDIATE = {"MULTI", "EXEC", "DISCARD", "WATCH", "UNWATCH", "RESET", "QUIT"}


@dataclass
class ExecResult:
    reply: Any
    written_keys: list[str]
    propagate_argv: Optional[list[str]]  # only populated when defer_side_effects=True
    flushed: bool
    command_name: str


class InternalConnState:
    """Stand-in connection for AOF replay and replication (no real socket)."""

    def __init__(self):
        self.selected_db = 0
        self.username = "default"
        self.protocol_version = 2
        self.authenticated = True
        self.in_multi = False
        self.multi_queue: list[list[str]] = []
        self.multi_error = False
        self.watched: dict = {}
        self.channels: set = set()
        self.patterns: set = set()
        self.client_id = -1
        self.addr = "internal"
        self.name = ""
        self.should_close = False

    def deliver(self, channel: str, message: str) -> None:
        pass  # internal contexts are never pub/sub targets

    def deliver_cdc_event(self, event: dict) -> None:
        pass


class EmberEngine:
    def __init__(self, config: dict):
        self.config = {**DEFAULT_CONFIG, **config}  # merged: caller may pass a sparse dict
        self.version = _EMBERDB_VERSION
        self.start_time = time.time()
        self.last_save_time = 0.0

        self.databases = [Database(i) for i in range(self.config["databases"])]

        self.pubsub = PubSubBroker()
        self.cdc = ChangeLog(path=self.config.get("cdc_path") or None,
                              ring_capacity=self.config["cdc_ring_capacity"])
        self.indexes = IndexManager(self.databases)
        self.vectors = VectorSearchEngine()

        self.acl = ACLManager()
        if self.config.get("requirepass"):
            self.acl.set_password("default", self.config["requirepass"])

        self.metrics = Metrics(slowlog_threshold_ms=self.config["slowlog_threshold_ms"])
        self.hooks = HookManager()
        self.eviction = EvictionManager(
            maxmemory_bytes=self.config["maxmemory"],
            policy=self.config["maxmemory_policy"],
        )
        self.expirer = ActiveExpirer(
            self.databases, on_expired=self._on_expired,
            interval_seconds=self.config["active_expire_interval"],
        )
        self.replication = ReplicationManager(self)
        self.branches = BranchManager(self.databases)

        self.snapshot_path = self.config["snapshot_path"]
        self.aof_path = self.config["aof_path"]
        self.aof: Optional[AOFWriter] = None
        if self.config["appendonly"]:
            self.aof = AOFWriter(self.aof_path, fsync_policy=self.config["aof_fsync"])

        self.clients: dict[int, Any] = {}
        self._next_client_id = 1
        self._internal_conn = InternalConnState()

    # -- startup: snapshot, then AOF on top ----------------------------------

    def load_from_disk(self) -> None:
        if os.path.exists(self.snapshot_path):
            n = load_snapshot(self.snapshot_path, self.databases)
            logger.info("loaded %d keys from snapshot %s", n, self.snapshot_path)
        if self.config.get("appendonly", False) and os.path.exists(self.aof_path):
            replayed = replay_aof(self.aof_path, self._replay_dispatch)
            logger.info("replayed %d commands from AOF %s", replayed, self.aof_path)

    def _replay_dispatch(self, db_index: int, argv: list[str]) -> None:
        self.execute(self._internal_conn, db_index, argv, is_replay=True)

    async def start_background_tasks(self) -> None:
        self.expirer.start()
        asyncio.create_task(self._metrics_tick_loop(), name="metrics-tick")
        if self.aof is not None and self.aof.fsync_policy == "everysec":
            asyncio.create_task(self._aof_fsync_loop(), name="aof-fsync")

    async def _aof_fsync_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                self.aof.flush_and_sync()
            except (OSError, ValueError):
                logger.exception("AOF fsync failed")

    async def _metrics_tick_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            self.metrics.sample_ops_per_second()

    def save_snapshot_now(self) -> int:
        # Always persist main, never whatever branch happens to be checked out —
        # branches are ephemeral, and silently snapshotting experimental data as
        # if it were main would be a nasty way to lose the real dataset.
        if self.branches.current == MAIN_BRANCH:
            target = self.databases
        else:
            target = self.branches.databases_of(MAIN_BRANCH)
            logger.info("snapshot taken while on branch %r; persisting 'main' instead",
                        self.branches.current)
        n = save_snapshot(self.snapshot_path, target)
        self.last_save_time = time.time()
        return n

    # -- branching ---------------------------------------------------------
    # Switching/merging swaps the dataset wholesale, so secondary indexes
    # (which cache per-key values) must be rebuilt against what's now live.

    def create_branch(self, name: str, from_branch: Optional[str] = None) -> int:
        return self.branches.create(name, from_branch)

    def switch_branch(self, name: str) -> None:
        self.branches.switch(name)
        self.indexes.rebuild_all()

    def merge_branch(self, source: str, into: Optional[str] = None, strategy: str = "fail") -> dict:
        result = self.branches.merge(source, into, strategy)
        changes = result.pop("_changes", [])
        target = result.pop("_target", None)
        if result["merged"]:
            self.indexes.rebuild_all()
            if target == MAIN_BRANCH:
                self._persist_merge_into_main(changes)
        return result

    def _persist_merge_into_main(self, changes: list[tuple[int, str, str]]) -> None:
        """Merges write values directly, bypassing execute(); emit a replayable
        command per changed key so main's AOF, CDC, and replicas see them."""
        import json
        from .persistence import _serialize_value
        main_dbs = self.branches.databases_of(MAIN_BRANCH)
        for db_index, key, action in changes:
            db = main_dbs[db_index]
            if action == "delete" or not db.exists(key):
                argv = ["DEL", key]
            else:
                argv = ["EMBER.RESTORE", key, json.dumps(_serialize_value(db.peek(key)))]
                exp = db.expires.get(key)
                if exp is not None:
                    argv += ["PXAT", str(int(exp * 1000))]
            if self.aof is not None:
                self.aof.log(db_index, argv)
            self.cdc.append(db_index, "write", "BRANCH.MERGE", key, [key])
            self.replication.propagate(argv, db_index)

    def drop_branch(self, name: str) -> None:
        self.branches.drop(name)

    def shutdown(self) -> None:
        self.expirer.stop()
        if self.aof is not None:
            self.aof.close()
        self.cdc.close()

    # -- client registry (server.py calls these on connect/disconnect) ------

    def register_client(self, conn) -> int:
        cid = self._next_client_id
        self._next_client_id += 1
        conn.client_id = cid
        self.clients[cid] = conn
        self.metrics.record_connect()
        return cid

    def unregister_client(self, conn) -> None:
        self.clients.pop(getattr(conn, "client_id", -1), None)
        self.pubsub.unsubscribe_all(conn)
        cb = getattr(conn, "cdc_tail_callback", None)
        if cb is not None:
            self.cdc.tail_unsubscribe(cb)
        self.replication.remove_replica(conn)
        self.metrics.record_disconnect()

    # -- expiry/eviction fan-out: no CommandContext, so wire CDC/hooks/AOF/replication/indexes directly --

    def _on_expired(self, db_index: int, key: str) -> None:
        self.metrics.expired_keys += 1
        self.indexes.on_delete(db_index, key)
        self.cdc.append(db_index, "expired", "EXPIRE", key, [key])
        self.hooks.on_expired(db_index, key)
        if self.aof is not None:
            self.aof.log(db_index, ["DEL", key])
        self.replication.propagate(["DEL", key], db_index)

    def _on_evicted(self, db_index: int, key: str) -> None:
        self.metrics.evicted_keys += 1
        self.indexes.on_delete(db_index, key)
        self.cdc.append(db_index, "evicted", "EVICT", key, [key])
        if self.aof is not None:
            self.aof.log(db_index, ["DEL", key])
        self.replication.propagate(["DEL", key], db_index)

    # -- the central dispatch -------------------------------------------------

    def execute(self, conn_state: Any, db_index: int, argv: list[str], *,
                is_replay: bool = False, defer_side_effects: bool = False) -> ExecResult:
        if not argv:
            raise RespError("empty command")
        name = argv[0].upper()
        spec = COMMAND_TABLE.get(name)
        if spec is None:
            raise RespError(f"unknown command '{argv[0]}'")
        check_arity(spec, argv)

        if not is_replay and not spec.no_auth_required:
            if not getattr(conn_state, "authenticated", True):
                raise RespError("Authentication required.", code="NOAUTH")
            key_hint = [argv[1]] if len(argv) > 1 else []
            self.acl.check(getattr(conn_state, "username", "default"), name, key_hint)

        ctx = CommandContext(
            engine=self, db_index=db_index,
            username=getattr(conn_state, "username", "default"),
            protocol_version=getattr(conn_state, "protocol_version", 2),
            conn_state=conn_state, is_replay=is_replay,
        )

        if spec.write and not is_replay:
            self.eviction.enforce(self.databases, on_evicted=self._on_evicted)

        start = time.perf_counter()
        reply = spec.handler(ctx, argv)
        duration_ms = (time.perf_counter() - start) * 1000
        self.metrics.record_command(argv, duration_ms)

        propagate_argv = None
        if spec.write and not is_replay:
            propagate_argv = ctx.propagate_override or argv
            if not defer_side_effects:
                self.apply_write_side_effects(db_index, propagate_argv, ctx.written_keys, ctx.flushed, name)

        return ExecResult(
            reply=reply, written_keys=ctx.written_keys,
            propagate_argv=propagate_argv if defer_side_effects else None,
            flushed=ctx.flushed, command_name=name,
        )

    def apply_write_side_effects(self, db_index: int, propagate_argv: list[str], written_keys: list[str],
                                  flushed: bool, command_name: str) -> None:
        # Branches are in-memory and ephemeral: their writes must not reach the
        # AOF or replicas, or they'd be replayed into main on restart / applied
        # to a replica that has no notion of branches. Indexes and CDC still
        # track them, since those reflect what's live right now.
        on_main = self.branches.current == MAIN_BRANCH

        if self.aof is not None and on_main:
            self.aof.log(db_index, propagate_argv)

        for key in written_keys:
            if self.databases[db_index].exists(key):
                self.indexes.on_write(db_index, key)
            else:
                self.indexes.on_delete(db_index, key)

        if flushed:
            if command_name == "FLUSHALL":
                self.indexes.clear_all()
                for i in range(len(self.databases)):
                    self.cdc.append(i, "flush", command_name, None, [])
            else:
                self.indexes.clear_db(db_index)
                self.cdc.append(db_index, "flush", command_name, None, [])
        else:
            cdc_key = written_keys[0] if len(written_keys) == 1 else None
            self.cdc.append(db_index, "write", command_name, cdc_key, written_keys)

        if on_main:
            self.replication.propagate(propagate_argv, db_index)

    # -- client-facing entry point: adds MULTI-queueing + subscriber gating -

    def dispatch_from_client(self, conn_state: Any, argv: list[str]) -> Any:
        if not argv:
            raise RespError("empty command")
        name = argv[0].upper()
        spec = COMMAND_TABLE.get(name)

        subscribed = bool(getattr(conn_state, "channels", None) or getattr(conn_state, "patterns", None))
        if subscribed and getattr(conn_state, "protocol_version", 2) < 3:
            if spec is None or not spec.pubsub_context_ok:
                raise RespError(
                    f"Can't execute '{argv[0].lower()}': only (P)SUBSCRIBE / (P)UNSUBSCRIBE / "
                    "PING / QUIT / RESET are allowed in this context"
                )

        if getattr(conn_state, "in_multi", False) and name not in _CONTROL_COMMANDS_ALWAYS_IMMEDIATE:
            if spec is None:
                conn_state.multi_error = True
                raise RespError(f"unknown command '{argv[0]}'")
            try:
                check_arity(spec, argv)
            except RespError:
                conn_state.multi_error = True
                raise
            conn_state.multi_queue.append(argv)
            return SimpleString("QUEUED")

        result = self.execute(conn_state, conn_state.selected_db, argv)
        return result.reply

    # -- transaction support (used by commands/transactions.py::cmd_exec) ---

    def snapshot_candidates(self, db_index: int, argv: list[str]) -> list[tuple]:
        """Pre-image for rollback: snapshot every token in argv[1:] that's an existing key."""
        db = self.databases[db_index]
        seen: list[str] = []
        for token in argv[1:]:
            if token not in seen:
                seen.append(token)
        snapshot = []
        for key in seen:
            if db.exists(key):
                snapshot.append((key, True, copy.deepcopy(db.peek(key)), db.expires.get(key)))
            else:
                snapshot.append((key, False, None, None))
        return snapshot

    def restore_snapshot(self, db_index: int, snapshot: list[tuple]) -> None:
        db = self.databases[db_index]
        for key, existed, value, ttl_at in snapshot:
            if existed:
                db.set_value(key, value)
                if ttl_at is not None:
                    db.set_expire_at(key, ttl_at)
                else:
                    db.persist(key)
                self.indexes.on_write(db_index, key)
            else:
                if db.exists(key):
                    db.delete(key)
                self.indexes.on_delete(db_index, key)

    def begin_transaction_buffering(self) -> None:
        if self.aof is not None:
            self.aof.begin_batch()

    def commit_transaction_buffering(self) -> None:
        if self.aof is not None:
            self.aof.commit_batch()

    def abort_transaction_buffering(self) -> None:
        if self.aof is not None:
            self.aof.discard_batch()
