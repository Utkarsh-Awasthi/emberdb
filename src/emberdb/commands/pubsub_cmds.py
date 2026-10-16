"""SUBSCRIBE/PUBLISH and friends."""

from __future__ import annotations

from ..protocol import MultiReply, RespError
from . import CommandContext, register


def _ensure_pubsub_sets(conn) -> tuple[set, set]:
    if not hasattr(conn, "channels") or conn.channels is None:
        conn.channels = set()
    if not hasattr(conn, "patterns") or conn.patterns is None:
        conn.patterns = set()
    return conn.channels, conn.patterns


@register("SUBSCRIBE", arity=-2, pubsub_context_ok=True)
def cmd_subscribe(ctx: CommandContext, argv: list[str]) -> object:
    conn = ctx.conn_state
    channels, _ = _ensure_pubsub_sets(conn)
    frames = []
    for channel in argv[1:]:
        count = ctx.engine.pubsub.subscribe(conn, channel)
        channels.add(channel)
        frames.append(["subscribe", channel, count])
    return MultiReply(frames)


@register("UNSUBSCRIBE", arity=-1, pubsub_context_ok=True)
def cmd_unsubscribe(ctx: CommandContext, argv: list[str]) -> object:
    conn = ctx.conn_state
    channels, _ = _ensure_pubsub_sets(conn)
    targets = argv[1:] if len(argv) > 1 else list(channels)
    if not targets:
        return MultiReply([["unsubscribe", None, 0]])
    frames = []
    for channel in targets:
        count = ctx.engine.pubsub.unsubscribe(conn, channel)
        channels.discard(channel)
        frames.append(["unsubscribe", channel, count])
    return MultiReply(frames)


@register("PSUBSCRIBE", arity=-2, pubsub_context_ok=True)
def cmd_psubscribe(ctx: CommandContext, argv: list[str]) -> object:
    conn = ctx.conn_state
    _, patterns = _ensure_pubsub_sets(conn)
    frames = []
    for pattern in argv[1:]:
        count = ctx.engine.pubsub.psubscribe(conn, pattern)
        patterns.add(pattern)
        frames.append(["psubscribe", pattern, count])
    return MultiReply(frames)


@register("PUNSUBSCRIBE", arity=-1, pubsub_context_ok=True)
def cmd_punsubscribe(ctx: CommandContext, argv: list[str]) -> object:
    conn = ctx.conn_state
    _, patterns = _ensure_pubsub_sets(conn)
    targets = argv[1:] if len(argv) > 1 else list(patterns)
    if not targets:
        return MultiReply([["punsubscribe", None, 0]])
    frames = []
    for pattern in targets:
        count = ctx.engine.pubsub.punsubscribe(conn, pattern)
        patterns.discard(pattern)
        frames.append(["punsubscribe", pattern, count])
    return MultiReply(frames)


@register("PUBLISH", arity=3)
def cmd_publish(ctx: CommandContext, argv: list[str]) -> object:
    # Not flagged write=True: a publish doesn't touch the keyspace, so it
    # shouldn't hit the AOF, count against maxmemory, or emit a CDC event.
    # KNOWN GAP: this also means PUBLISH is currently NOT forwarded to
    # replicas (replication.py only streams write-flagged keyspace
    # commands) — a subscriber connected to a follower will not see
    # messages published on the leader. Documented in docs/COMPARISON.md.
    channel, message = argv[1], argv[2]
    return ctx.engine.pubsub.publish(channel, message)


@register("PUBSUB", arity=-2)
def cmd_pubsub(ctx: CommandContext, argv: list[str]) -> object:
    sub = argv[1].upper()
    if sub == "CHANNELS":
        pattern = argv[2] if len(argv) > 2 else "*"
        return ctx.engine.pubsub.channels(pattern)
    if sub == "NUMSUB":
        out = []
        for ch in argv[2:]:
            out.append(ch)
            out.append(ctx.engine.pubsub.numsub(ch))
        return out
    if sub == "NUMPAT":
        return ctx.engine.pubsub.numpat()
    raise RespError(f"Unknown PUBSUB subcommand or wrong number of arguments for '{argv[1]}'")
