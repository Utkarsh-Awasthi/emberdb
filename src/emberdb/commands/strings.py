"""String commands."""

from __future__ import annotations

import time

from ..protocol import RespError, SimpleString
from ..valuetypes import expect_type
from . import CommandContext, parse_float, parse_int, register


def _get_string(ctx: CommandContext, key: str) -> str | None:
    return expect_type(ctx.db.get(key), str)


@register("SET", arity=-3, write=True)
def cmd_set(ctx: CommandContext, argv: list[str]) -> object:
    key, value = argv[1], argv[2]
    nx = xx = keepttl = want_get = False
    expire_at: float | None = None
    propagate_expire_ms: int | None = None

    i = 3
    while i < len(argv):
        opt = argv[i].upper()
        if opt == "NX":
            nx = True
        elif opt == "XX":
            xx = True
        elif opt == "GET":
            want_get = True
        elif opt == "KEEPTTL":
            keepttl = True
        elif opt in ("EX", "PX", "EXAT", "PXAT"):
            i += 1
            if i >= len(argv):
                raise RespError("syntax error")
            n = parse_int(argv[i], "expire time")
            now_ms = int(time.time() * 1000)
            if opt == "EX":
                abs_ms = now_ms + n * 1000
            elif opt == "PX":
                abs_ms = now_ms + n
            elif opt == "EXAT":
                abs_ms = n * 1000
            else:  # PXAT
                abs_ms = n
            expire_at = abs_ms / 1000.0
            propagate_expire_ms = abs_ms
        else:
            raise RespError("syntax error")
        i += 1

    if nx and xx:
        raise RespError("syntax error")

    existing = expect_type(ctx.db.get(key), str)
    old_value = existing
    if nx and existing is not None:
        return old_value if want_get else None
    if xx and existing is None:
        return old_value if want_get else None

    ctx.db.set_value(key, value, keep_ttl=keepttl)
    if expire_at is not None:
        ctx.db.set_expire_at(key, expire_at)
    ctx.mark_write(key)

    # Propagate a fully-resolved, absolute-time form so AOF replay / a
    # replica applying this later doesn't grant a *fresh* relative TTL —
    # see persistence.py's module docstring ("AOF command rewriting").
    if propagate_expire_ms is not None:
        ctx.propagate_override = ["SET", key, value, "PXAT", str(propagate_expire_ms)]
    elif keepttl:
        ctx.propagate_override = ["SET", key, value, "KEEPTTL"]

    if want_get:
        return old_value
    return SimpleString("OK")


@register("GET", arity=2)
def cmd_get(ctx: CommandContext, argv: list[str]) -> object:
    return _get_string(ctx, argv[1])


@register("GETSET", arity=3, write=True)
def cmd_getset(ctx: CommandContext, argv: list[str]) -> object:
    key, value = argv[1], argv[2]
    old = expect_type(ctx.db.get(key), str)
    ctx.db.set_value(key, value)
    ctx.mark_write(key)
    return old


@register("GETDEL", arity=2, write=True)
def cmd_getdel(ctx: CommandContext, argv: list[str]) -> object:
    key = argv[1]
    old = expect_type(ctx.db.get(key), str)
    if old is not None:
        ctx.db.delete(key)
        ctx.mark_write(key)
    return old


@register("GETEX", arity=-2, write=True)
def cmd_getex(ctx: CommandContext, argv: list[str]) -> object:
    key = argv[1]
    value = expect_type(ctx.db.get(key), str)
    if value is None:
        return None
    i = 2
    changed_ttl = False
    while i < len(argv):
        opt = argv[i].upper()
        if opt == "PERSIST":
            ctx.db.persist(key)
            changed_ttl = True
        elif opt in ("EX", "PX", "EXAT", "PXAT"):
            i += 1
            n = parse_int(argv[i], "expire time")
            now_ms = int(time.time() * 1000)
            abs_ms = {"EX": now_ms + n * 1000, "PX": now_ms + n,
                      "EXAT": n * 1000, "PXAT": n}[opt]
            ctx.db.set_expire_at(key, abs_ms / 1000.0)
            changed_ttl = True
        else:
            raise RespError("syntax error")
        i += 1
    if changed_ttl:
        ctx.mark_write(key)  # TTL-only change; still worth propagating
    return value


@register("APPEND", arity=3, write=True)
def cmd_append(ctx: CommandContext, argv: list[str]) -> object:
    key, suffix = argv[1], argv[2]
    current = expect_type(ctx.db.get(key), str) or ""
    new_value = current + suffix
    ctx.db.set_value(key, new_value, keep_ttl=True)
    ctx.mark_write(key)
    return len(new_value.encode("utf-8", "surrogateescape"))


@register("STRLEN", arity=2)
def cmd_strlen(ctx: CommandContext, argv: list[str]) -> object:
    value = _get_string(ctx, argv[1])
    return len(value.encode("utf-8", "surrogateescape")) if value else 0


def _incr_by(ctx: CommandContext, key: str, delta: int) -> int:
    current = expect_type(ctx.db.get(key), str)
    n = parse_int(current, "current value") if current is not None else 0
    new_val = n + delta
    ctx.db.set_value(key, str(new_val), keep_ttl=True)
    ctx.mark_write(key)
    return new_val


@register("INCR", arity=2, write=True)
def cmd_incr(ctx: CommandContext, argv: list[str]) -> object:
    return _incr_by(ctx, argv[1], 1)


@register("DECR", arity=2, write=True)
def cmd_decr(ctx: CommandContext, argv: list[str]) -> object:
    return _incr_by(ctx, argv[1], -1)


@register("INCRBY", arity=3, write=True)
def cmd_incrby(ctx: CommandContext, argv: list[str]) -> object:
    return _incr_by(ctx, argv[1], parse_int(argv[2], "increment"))


@register("DECRBY", arity=3, write=True)
def cmd_decrby(ctx: CommandContext, argv: list[str]) -> object:
    return _incr_by(ctx, argv[1], -parse_int(argv[2], "decrement"))


@register("INCRBYFLOAT", arity=3, write=True)
def cmd_incrbyfloat(ctx: CommandContext, argv: list[str]) -> object:
    key = argv[1]
    delta = parse_float(argv[2], "increment")
    current = expect_type(ctx.db.get(key), str)
    n = parse_float(current, "current value") if current is not None else 0.0
    new_val = n + delta
    text = ("%.17g" % new_val).rstrip("0").rstrip(".") if "." in ("%.17g" % new_val) else "%.17g" % new_val
    ctx.db.set_value(key, text, keep_ttl=True)
    ctx.mark_write(key)
    return text


@register("GETRANGE", arity=4)
def cmd_getrange(ctx: CommandContext, argv: list[str]) -> object:
    value = _get_string(ctx, argv[1]) or ""
    start, end = parse_int(argv[2]), parse_int(argv[3])
    n = len(value)
    if n == 0:
        return ""
    if start < 0:
        start = max(n + start, 0)
    if end < 0:
        end = n + end
    end = min(end, n - 1)
    if start > end or start >= n:
        return ""
    return value[start:end + 1]


@register("SETRANGE", arity=4, write=True)
def cmd_setrange(ctx: CommandContext, argv: list[str]) -> object:
    key = argv[1]
    offset = parse_int(argv[2], "offset")
    if offset < 0:
        raise RespError("offset is out of range")
    fragment = argv[3]
    current = expect_type(ctx.db.get(key), str) or ""
    if offset > len(current):
        current = current + ("\x00" * (offset - len(current)))
    new_value = current[:offset] + fragment + current[offset + len(fragment):]
    ctx.db.set_value(key, new_value, keep_ttl=True)
    ctx.mark_write(key)
    return len(new_value)


@register("MSET", arity=-3, write=True)
def cmd_mset(ctx: CommandContext, argv: list[str]) -> object:
    pairs = argv[1:]
    if len(pairs) % 2 != 0:
        raise RespError("wrong number of arguments for 'mset' command")
    for i in range(0, len(pairs), 2):
        ctx.db.set_value(pairs[i], pairs[i + 1])
        ctx.mark_write(pairs[i])
    return SimpleString("OK")


@register("MSETNX", arity=-3, write=True)
def cmd_msetnx(ctx: CommandContext, argv: list[str]) -> object:
    pairs = argv[1:]
    if len(pairs) % 2 != 0:
        raise RespError("wrong number of arguments for 'msetnx' command")
    keys = pairs[0::2]
    if any(ctx.db.exists(k) for k in keys):
        return 0
    for i in range(0, len(pairs), 2):
        ctx.db.set_value(pairs[i], pairs[i + 1])
        ctx.mark_write(pairs[i])
    return 1


@register("MGET", arity=-2)
def cmd_mget(ctx: CommandContext, argv: list[str]) -> object:
    out = []
    for key in argv[1:]:
        try:
            out.append(_get_string(ctx, key))
        except RespError:
            out.append(None)  # WRONGTYPE inside MGET yields nil for that slot, not a whole-command error
    return out


@register("SETNX", arity=3, write=True)
def cmd_setnx(ctx: CommandContext, argv: list[str]) -> object:
    key, value = argv[1], argv[2]
    if ctx.db.exists(key):
        return 0
    ctx.db.set_value(key, value)
    ctx.mark_write(key)
    return 1


@register("SETEX", arity=4, write=True)
def cmd_setex(ctx: CommandContext, argv: list[str]) -> object:
    key, seconds, value = argv[1], parse_int(argv[2], "seconds"), argv[3]
    if seconds <= 0:
        raise RespError("invalid expire time in 'setex' command")
    ctx.db.set_value(key, value)
    abs_ms = int(time.time() * 1000) + seconds * 1000
    ctx.db.set_expire_at(key, abs_ms / 1000.0)
    ctx.mark_write(key)
    ctx.propagate_override = ["SET", key, value, "PXAT", str(abs_ms)]
    return SimpleString("OK")


@register("PSETEX", arity=4, write=True)
def cmd_psetex(ctx: CommandContext, argv: list[str]) -> object:
    key, ms, value = argv[1], parse_int(argv[2], "milliseconds"), argv[3]
    if ms <= 0:
        raise RespError("invalid expire time in 'psetex' command")
    ctx.db.set_value(key, value)
    abs_ms = int(time.time() * 1000) + ms
    ctx.db.set_expire_at(key, abs_ms / 1000.0)
    ctx.mark_write(key)
    ctx.propagate_override = ["SET", key, value, "PXAT", str(abs_ms)]
    return SimpleString("OK")
