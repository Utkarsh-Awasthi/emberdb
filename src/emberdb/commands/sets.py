"""Set commands."""

from __future__ import annotations

import random

from ..protocol import SimpleString
from ..valuetypes import expect_type
from . import CommandContext, parse_int, register


def _get_set(ctx: CommandContext, key: str, create: bool = False) -> set:
    value = expect_type(ctx.db.get(key), set)
    if value is None and create:
        value = set()
        ctx.db.set_value(key, value)
    return value


@register("SADD", arity=-3, write=True)
def cmd_sadd(ctx: CommandContext, argv: list[str]) -> object:
    key = argv[1]
    s = _get_set(ctx, key, create=True)
    before = len(s)
    s.update(argv[2:])
    added = len(s) - before
    if added:
        ctx.db.touch_version(key)
        ctx.mark_write(key)
    return added


@register("SREM", arity=-3, write=True)
def cmd_srem(ctx: CommandContext, argv: list[str]) -> object:
    key = argv[1]
    s = expect_type(ctx.db.get(key), set)
    if not s:
        return 0
    before = len(s)
    s.difference_update(argv[2:])
    removed = before - len(s)
    if removed:
        if s:
            ctx.db.touch_version(key)
        else:
            ctx.db.delete(key)
        ctx.mark_write(key)
    return removed


@register("SMEMBERS", arity=2)
def cmd_smembers(ctx: CommandContext, argv: list[str]) -> object:
    s = expect_type(ctx.db.get(argv[1]), set)
    return set(s) if s else set()


@register("SISMEMBER", arity=3)
def cmd_sismember(ctx: CommandContext, argv: list[str]) -> object:
    s = expect_type(ctx.db.get(argv[1]), set)
    return 1 if s and argv[2] in s else 0


@register("SMISMEMBER", arity=-3)
def cmd_smismember(ctx: CommandContext, argv: list[str]) -> object:
    s = expect_type(ctx.db.get(argv[1]), set) or set()
    return [1 if m in s else 0 for m in argv[2:]]


@register("SCARD", arity=2)
def cmd_scard(ctx: CommandContext, argv: list[str]) -> object:
    s = expect_type(ctx.db.get(argv[1]), set)
    return len(s) if s else 0


def _load_sets(ctx: CommandContext, keys: list[str]) -> list[set]:
    return [expect_type(ctx.db.get(k), set) or set() for k in keys]


@register("SUNION", arity=-2)
def cmd_sunion(ctx: CommandContext, argv: list[str]) -> object:
    return set().union(*_load_sets(ctx, argv[1:]))


@register("SINTER", arity=-2)
def cmd_sinter(ctx: CommandContext, argv: list[str]) -> object:
    sets_ = _load_sets(ctx, argv[1:])
    if not sets_:
        return set()
    result = sets_[0].copy()
    for s in sets_[1:]:
        result &= s
    return result


@register("SDIFF", arity=-2)
def cmd_sdiff(ctx: CommandContext, argv: list[str]) -> object:
    sets_ = _load_sets(ctx, argv[1:])
    if not sets_:
        return set()
    result = sets_[0].copy()
    for s in sets_[1:]:
        result -= s
    return result


def _store_result(ctx: CommandContext, dest: str, result: set) -> int:
    if result:
        ctx.db.set_value(dest, result)
    else:
        ctx.db.delete(dest)
    ctx.mark_write(dest)
    return len(result)


@register("SUNIONSTORE", arity=-3, write=True)
def cmd_sunionstore(ctx: CommandContext, argv: list[str]) -> object:
    return _store_result(ctx, argv[1], set().union(*_load_sets(ctx, argv[2:])))


@register("SINTERSTORE", arity=-3, write=True)
def cmd_sinterstore(ctx: CommandContext, argv: list[str]) -> object:
    sets_ = _load_sets(ctx, argv[2:])
    result = sets_[0].copy() if sets_ else set()
    for s in sets_[1:]:
        result &= s
    return _store_result(ctx, argv[1], result)


@register("SDIFFSTORE", arity=-3, write=True)
def cmd_sdiffstore(ctx: CommandContext, argv: list[str]) -> object:
    sets_ = _load_sets(ctx, argv[2:])
    result = sets_[0].copy() if sets_ else set()
    for s in sets_[1:]:
        result -= s
    return _store_result(ctx, argv[1], result)


@register("SPOP", arity=-2, write=True)
def cmd_spop(ctx: CommandContext, argv: list[str]) -> object:
    key = argv[1]
    s = expect_type(ctx.db.get(key), set)
    count = parse_int(argv[2], "count") if len(argv) > 2 else None
    if not s:
        return set() if count is not None else None
    n = 1 if count is None else min(count, len(s))
    chosen = random.sample(list(s), n)
    for m in chosen:
        s.discard(m)
    if s:
        ctx.db.touch_version(key)
    else:
        ctx.db.delete(key)
    ctx.mark_write(key)
    return set(chosen) if count is not None else chosen[0]


@register("SRANDMEMBER", arity=-2)
def cmd_srandmember(ctx: CommandContext, argv: list[str]) -> object:
    s = expect_type(ctx.db.get(argv[1]), set)
    if not s:
        return [] if len(argv) > 2 else None
    if len(argv) == 2:
        return random.choice(list(s))
    count = parse_int(argv[2], "count")
    members = list(s)
    if count >= 0:
        return random.sample(members, min(count, len(members)))
    return [random.choice(members) for _ in range(-count)]
