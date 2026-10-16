"""MULTI/EXEC/WATCH/DISCARD, with real rollback on a runtime failure."""

from __future__ import annotations

from ..protocol import RespError, SimpleString
from . import CommandContext, register


def _ensure_watch_dict(conn) -> dict:
    if not hasattr(conn, "watched") or conn.watched is None:
        conn.watched = {}
    return conn.watched


def _clear_transaction_state(conn) -> None:
    conn.in_multi = False
    conn.multi_queue = []
    conn.multi_error = False
    conn.watched = {}


@register("MULTI", arity=1, no_auth_required=False)
def cmd_multi(ctx: CommandContext, argv: list[str]) -> object:
    conn = ctx.conn_state
    if getattr(conn, "in_multi", False):
        raise RespError("MULTI calls can not be nested")
    conn.in_multi = True
    conn.multi_queue = []
    conn.multi_error = False
    return SimpleString("OK")


@register("DISCARD", arity=1)
def cmd_discard(ctx: CommandContext, argv: list[str]) -> object:
    conn = ctx.conn_state
    if not getattr(conn, "in_multi", False):
        raise RespError("DISCARD without MULTI")
    _clear_transaction_state(conn)
    return SimpleString("OK")


@register("WATCH", arity=-2)
def cmd_watch(ctx: CommandContext, argv: list[str]) -> object:
    conn = ctx.conn_state
    if getattr(conn, "in_multi", False):
        raise RespError("WATCH inside MULTI is not allowed")
    watched = _ensure_watch_dict(conn)
    for key in argv[1:]:
        watched[(ctx.db_index, key)] = ctx.db.version_of(key)
    return SimpleString("OK")


@register("UNWATCH", arity=1)
def cmd_unwatch(ctx: CommandContext, argv: list[str]) -> object:
    ctx.conn_state.watched = {}
    return SimpleString("OK")


@register("EXEC", arity=1)
def cmd_exec(ctx: CommandContext, argv: list[str]) -> object:
    conn = ctx.conn_state
    engine = ctx.engine
    if not getattr(conn, "in_multi", False):
        raise RespError("EXEC without MULTI")

    queue = list(getattr(conn, "multi_queue", []))
    had_error = getattr(conn, "multi_error", False)
    watched = dict(getattr(conn, "watched", {}))
    _clear_transaction_state(conn)

    for (db_idx, key), version_at_watch in watched.items():
        if engine.databases[db_idx].version_of(key) != version_at_watch:
            return None  # nil: EXEC aborted because a watched key changed

    if had_error:
        raise RespError("Transaction discarded because of previous errors.", code="EXECABORT")

    if not queue:
        return []

    engine.begin_transaction_buffering()
    results = []
    applied = []  # (db_index, snapshot, propagate_argv, written_keys, flushed, command_name)
    try:
        for sub_argv in queue:
            snapshot = engine.snapshot_candidates(ctx.db_index, sub_argv)
            try:
                result = engine.execute(conn, ctx.db_index, sub_argv, defer_side_effects=True)
            except RespError as e:
                # Roll back this command's own (possibly partial) writes,
                # then every earlier command in this same transaction, in
                # reverse order, before propagating the failure.
                engine.restore_snapshot(ctx.db_index, snapshot)
                for prev_db_idx, prev_snapshot, *_ in reversed(applied):
                    engine.restore_snapshot(prev_db_idx, prev_snapshot)
                engine.abort_transaction_buffering()
                raise RespError(
                    f"transaction rolled back — {e.reply_text()}", code="EXECABORT"
                )
            results.append(result.reply)
            if result.propagate_argv is not None:
                applied.append((
                    ctx.db_index, snapshot, result.propagate_argv,
                    result.written_keys, result.flushed, result.command_name,
                ))
        engine.commit_transaction_buffering()
        for db_idx, _snap, propagate_argv, written_keys, flushed, command_name in applied:
            engine.apply_write_side_effects(db_idx, propagate_argv, written_keys, flushed, command_name)
    except RespError:
        raise
    return results
