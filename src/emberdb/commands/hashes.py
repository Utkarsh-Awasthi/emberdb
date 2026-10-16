"""Hash commands."""

from __future__ import annotations

import random

from ..protocol import RespError, SimpleString
from ..valuetypes import expect_type
from . import CommandContext, parse_float, parse_int, register


def _get_hash(ctx: CommandContext, key: str, create: bool = False) -> dict:
    value = expect_type(ctx.db.get(key), dict)
    if value is None and create:
        value = {}
        ctx.db.set_value(key, value)
    return value


@register("HSET", arity=-4, write=True)
def cmd_hset(ctx: CommandContext, argv: list[str]) -> object:
    key = argv[1]
    pairs = argv[2:]
    if len(pairs) % 2 != 0:
        raise RespError("wrong number of arguments for 'hset' command")
    h = _get_hash(ctx, key, create=True)
    added = 0
    for i in range(0, len(pairs), 2):
        field, value = pairs[i], pairs[i + 1]
        if field not in h:
            added += 1
        h[field] = value
    ctx.db.touch_version(key)
    ctx.mark_write(key)
    return added


@register("HMSET", arity=-4, write=True)
def cmd_hmset(ctx: CommandContext, argv: list[str]) -> object:
    cmd_hset(ctx, argv)
    return SimpleString("OK")


@register("HSETNX", arity=4, write=True)
def cmd_hsetnx(ctx: CommandContext, argv: list[str]) -> object:
    key, field, value = argv[1], argv[2], argv[3]
    h = _get_hash(ctx, key, create=True)
    if field in h:
        return 0
    h[field] = value
    ctx.db.touch_version(key)
    ctx.mark_write(key)
    return 1


@register("HGET", arity=3)
def cmd_hget(ctx: CommandContext, argv: list[str]) -> object:
    h = expect_type(ctx.db.get(argv[1]), dict)
    return h.get(argv[2]) if h else None


@register("HMGET", arity=-3)
def cmd_hmget(ctx: CommandContext, argv: list[str]) -> object:
    h = expect_type(ctx.db.get(argv[1]), dict) or {}
    return [h.get(f) for f in argv[2:]]


@register("HGETALL", arity=2)
def cmd_hgetall(ctx: CommandContext, argv: list[str]) -> object:
    h = expect_type(ctx.db.get(argv[1]), dict)
    return dict(h) if h else {}


@register("HDEL", arity=-3, write=True)
def cmd_hdel(ctx: CommandContext, argv: list[str]) -> object:
    key = argv[1]
    h = expect_type(ctx.db.get(key), dict)
    if not h:
        return 0
    removed = 0
    for field in argv[2:]:
        if field in h:
            del h[field]
            removed += 1
    if removed:
        if h:
            ctx.db.touch_version(key)
        else:
            ctx.db.delete(key)
        ctx.mark_write(key)
    return removed


@register("HEXISTS", arity=3)
def cmd_hexists(ctx: CommandContext, argv: list[str]) -> object:
    h = expect_type(ctx.db.get(argv[1]), dict)
    return 1 if h and argv[2] in h else 0


@register("HLEN", arity=2)
def cmd_hlen(ctx: CommandContext, argv: list[str]) -> object:
    h = expect_type(ctx.db.get(argv[1]), dict)
    return len(h) if h else 0


@register("HSTRLEN", arity=3)
def cmd_hstrlen(ctx: CommandContext, argv: list[str]) -> object:
    h = expect_type(ctx.db.get(argv[1]), dict)
    value = h.get(argv[2]) if h else None
    return len(value.encode("utf-8", "surrogateescape")) if value else 0


@register("HKEYS", arity=2)
def cmd_hkeys(ctx: CommandContext, argv: list[str]) -> object:
    h = expect_type(ctx.db.get(argv[1]), dict)
    return list(h.keys()) if h else []


@register("HVALS", arity=2)
def cmd_hvals(ctx: CommandContext, argv: list[str]) -> object:
    h = expect_type(ctx.db.get(argv[1]), dict)
    return list(h.values()) if h else []


@register("HINCRBY", arity=4, write=True)
def cmd_hincrby(ctx: CommandContext, argv: list[str]) -> object:
    key, field, delta = argv[1], argv[2], parse_int(argv[3], "increment")
    h = _get_hash(ctx, key, create=True)
    current = parse_int(h.get(field, "0"), "hash value")
    new_val = current + delta
    h[field] = str(new_val)
    ctx.db.touch_version(key)
    ctx.mark_write(key)
    return new_val


@register("HINCRBYFLOAT", arity=4, write=True)
def cmd_hincrbyfloat(ctx: CommandContext, argv: list[str]) -> object:
    key, field, delta = argv[1], argv[2], parse_float(argv[3], "increment")
    h = _get_hash(ctx, key, create=True)
    current = parse_float(h.get(field, "0"), "hash value")
    new_val = current + delta
    text = ("%.17g" % new_val)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    h[field] = text
    ctx.db.touch_version(key)
    ctx.mark_write(key)
    return text


@register("HRANDFIELD", arity=-2)
def cmd_hrandfield(ctx: CommandContext, argv: list[str]) -> object:
    h = expect_type(ctx.db.get(argv[1]), dict)
    if not h:
        return [] if len(argv) > 2 else None
    if len(argv) == 2:
        return random.choice(list(h.keys()))
    count = parse_int(argv[2], "count")
    with_values = len(argv) > 3 and argv[3].upper() == "WITHVALUES"
    keys = list(h.keys())
    if count >= 0:
        chosen = random.sample(keys, min(count, len(keys)))
    else:
        chosen = [random.choice(keys) for _ in range(-count)]  # negative: allow repeats
    if with_values:
        out = []
        for k in chosen:
            out.append(k)
            out.append(h[k])
        return out
    return chosen
