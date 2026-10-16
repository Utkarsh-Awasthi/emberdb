"""Type-agnostic key commands: existence, expiry, renaming, SCAN/KEYS."""

from __future__ import annotations

import fnmatch
import random
import time

from ..protocol import RespError, SimpleString
from ..valuetypes import type_name_of
from . import CommandContext, parse_int, register


@register("DEL", arity=-2, write=True)
def cmd_del(ctx: CommandContext, argv: list[str]) -> object:
    count = 0
    for key in argv[1:]:
        if ctx.db.delete(key):
            count += 1
            ctx.mark_write(key)
    return count


@register("UNLINK", arity=-2, write=True)
def cmd_unlink(ctx: CommandContext, argv: list[str]) -> object:
    # Modeled as a plain synchronous delete: real Redis's UNLINK frees
    # memory on a background thread to avoid blocking, which matters for
    # huge aggregate types. We don't have that background reclaim
    # machinery, so UNLINK and DEL are identical here — documented, not
    # silently pretended away.
    return cmd_del(ctx, argv)


@register("EXISTS", arity=-2)
def cmd_exists(ctx: CommandContext, argv: list[str]) -> object:
    return sum(1 for key in argv[1:] if ctx.db.exists(key))


@register("TYPE", arity=2)
def cmd_type(ctx: CommandContext, argv: list[str]) -> object:
    return SimpleString(type_name_of(ctx.db.peek(argv[1])))


@register("TOUCH", arity=-2)
def cmd_touch(ctx: CommandContext, argv: list[str]) -> object:
    count = 0
    for key in argv[1:]:
        if ctx.db.exists(key):
            ctx.db.touch_access(key)
            count += 1
    return count


@register("RANDOMKEY", arity=1)
def cmd_randomkey(ctx: CommandContext, argv: list[str]) -> object:
    keys = ctx.db.all_keys()
    return random.choice(keys) if keys else None


@register("RENAME", arity=3, write=True)
def cmd_rename(ctx: CommandContext, argv: list[str]) -> object:
    src, dst = argv[1], argv[2]
    if not ctx.db.exists(src):
        raise RespError("no such key")
    ctx.db.rename(src, dst)
    ctx.mark_write(src)
    ctx.mark_write(dst)
    return SimpleString("OK")


@register("RENAMENX", arity=3, write=True)
def cmd_renamenx(ctx: CommandContext, argv: list[str]) -> object:
    src, dst = argv[1], argv[2]
    if not ctx.db.exists(src):
        raise RespError("no such key")
    if ctx.db.exists(dst):
        return 0
    ctx.db.rename(src, dst)
    ctx.mark_write(src)
    ctx.mark_write(dst)
    return 1


@register("COPY", arity=-3, write=True)
def cmd_copy(ctx: CommandContext, argv: list[str]) -> object:
    import copy as copy_module
    src, dst = argv[1], argv[2]
    replace = "REPLACE" in [a.upper() for a in argv[3:]]
    value = ctx.db.peek(src)
    if value is None:
        return 0
    if ctx.db.exists(dst) and not replace:
        return 0
    ctx.db.set_value(dst, copy_module.deepcopy(value))
    ttl = ctx.db.ttl_seconds(src)
    if ttl is not None:
        ctx.db.set_expire_at(dst, time.time() + ttl)
    ctx.mark_write(dst)
    return 1


# -- TTL family --------------------------------------------------------------

@register("EXPIRE", arity=-3, write=True)
def cmd_expire(ctx: CommandContext, argv: list[str]) -> object:
    key, seconds = argv[1], parse_int(argv[2], "seconds")
    return _expire_at(ctx, key, time.time() + seconds, argv[3:])


@register("PEXPIRE", arity=-3, write=True)
def cmd_pexpire(ctx: CommandContext, argv: list[str]) -> object:
    key, ms = argv[1], parse_int(argv[2], "milliseconds")
    return _expire_at(ctx, key, time.time() + ms / 1000.0, argv[3:])


@register("EXPIREAT", arity=-3, write=True)
def cmd_expireat(ctx: CommandContext, argv: list[str]) -> object:
    key, ts = argv[1], parse_int(argv[2], "timestamp")
    return _expire_at(ctx, key, float(ts), argv[3:])


@register("PEXPIREAT", arity=-3, write=True)
def cmd_pexpireat(ctx: CommandContext, argv: list[str]) -> object:
    key, ts_ms = argv[1], parse_int(argv[2], "timestamp")
    return _expire_at(ctx, key, ts_ms / 1000.0, argv[3:])


def _expire_at(ctx: CommandContext, key: str, when_epoch: float, opts: list[str]) -> int:
    """Shared implementation for EXPIRE/PEXPIRE/EXPIREAT/PEXPIREAT. All four
    always propagate as PEXPIREAT with an absolute millisecond timestamp —
    see persistence.py's docstring on why relative-time writes must be
    rewritten before hitting the AOF/replicas."""
    flag = opts[0].upper() if opts else None
    if flag in ("NX", "XX", "GT", "LT"):
        current_ttl = ctx.db.ttl_seconds(key)
        has_ttl = current_ttl is not None
        if flag == "NX" and has_ttl:
            return 0
        if flag == "XX" and not has_ttl:
            return 0
        if flag in ("GT", "LT") and has_ttl:
            current_at = time.time() + current_ttl
            if flag == "GT" and when_epoch <= current_at:
                return 0
            if flag == "LT" and when_epoch >= current_at:
                return 0
    if not ctx.db.exists(key):
        return 0
    ctx.db.set_expire_at(key, when_epoch)
    ctx.mark_write(key)
    ctx.propagate_override = ["PEXPIREAT", key, str(int(when_epoch * 1000))]
    return 1


@register("TTL", arity=2)
def cmd_ttl(ctx: CommandContext, argv: list[str]) -> object:
    if not ctx.db.exists(argv[1]):
        return -2
    ttl = ctx.db.ttl_seconds(argv[1])
    return -1 if ttl is None else int(round(ttl))


@register("PTTL", arity=2)
def cmd_pttl(ctx: CommandContext, argv: list[str]) -> object:
    if not ctx.db.exists(argv[1]):
        return -2
    ttl = ctx.db.ttl_seconds(argv[1])
    return -1 if ttl is None else int(round(ttl * 1000))


@register("PERSIST", arity=2, write=True)
def cmd_persist(ctx: CommandContext, argv: list[str]) -> object:
    key = argv[1]
    if not ctx.db.exists(key):
        return 0
    changed = ctx.db.persist(key)
    if changed:
        ctx.mark_write(key)
    return 1 if changed else 0


@register("EXPIRETIME", arity=2)
def cmd_expiretime(ctx: CommandContext, argv: list[str]) -> object:
    if not ctx.db.exists(argv[1]):
        return -2
    ttl = ctx.db.ttl_seconds(argv[1])
    if ttl is None:
        return -1
    return int(round(time.time() + ttl))


# -- keyspace iteration -------------------------------------------------------

@register("KEYS", arity=2)
def cmd_keys(ctx: CommandContext, argv: list[str]) -> object:
    pattern = argv[1]
    return [k for k in ctx.db.all_keys() if fnmatch.fnmatchcase(k, pattern)]


@register("DBSIZE", arity=1)
def cmd_dbsize(ctx: CommandContext, argv: list[str]) -> object:
    return ctx.db.size()


@register("SCAN", arity=-2)
def cmd_scan(ctx: CommandContext, argv: list[str]) -> object:
    """Cursor = how many sorted keys returned so far (not a real hash-bucket cursor)."""
    cursor = parse_int(argv[1], "cursor")
    count = 10
    pattern = "*"
    type_filter = None
    i = 2
    while i < len(argv):
        opt = argv[i].upper()
        if opt == "COUNT":
            i += 1
            count = parse_int(argv[i], "count")
        elif opt == "MATCH":
            i += 1
            pattern = argv[i]
        elif opt == "TYPE":
            i += 1
            type_filter = argv[i]
        else:
            raise RespError("syntax error")
        i += 1

    all_keys = sorted(ctx.db.all_keys())
    window = all_keys[cursor:cursor + count]
    next_cursor = cursor + len(window)
    if next_cursor >= len(all_keys):
        next_cursor = 0  # 0 signals "iteration complete", same as real Redis
    out = []
    for k in window:
        if not fnmatch.fnmatchcase(k, pattern):
            continue
        if type_filter and type_name_of(ctx.db.peek(k)) != type_filter:
            continue
        out.append(k)
    return [str(next_cursor), out]


@register("FLUSHDB", arity=-1, write=True)
def cmd_flushdb(ctx: CommandContext, argv: list[str]) -> object:
    ctx.db.flush()
    ctx.flushed = True
    return SimpleString("OK")


@register("FLUSHALL", arity=-1, write=True, admin=True)
def cmd_flushall(ctx: CommandContext, argv: list[str]) -> object:
    for db in ctx.engine.databases:
        db.flush()
    ctx.flushed = True
    return SimpleString("OK")
