"""List commands, backed by collections.deque."""

from __future__ import annotations

from collections import deque

from ..protocol import RespError
from ..valuetypes import expect_type
from . import CommandContext, parse_int, register


def _get_list(ctx: CommandContext, key: str, create: bool = False) -> deque:
    value = expect_type(ctx.db.get(key), deque)
    if value is None and create:
        value = deque()
        ctx.db.set_value(key, value)
    return value


@register("LPUSH", arity=-3, write=True)
def cmd_lpush(ctx: CommandContext, argv: list[str]) -> object:
    key = argv[1]
    lst = _get_list(ctx, key, create=True)
    for item in argv[2:]:
        lst.appendleft(item)
    ctx.db.touch_version(key)
    ctx.mark_write(key)
    return len(lst)


@register("RPUSH", arity=-3, write=True)
def cmd_rpush(ctx: CommandContext, argv: list[str]) -> object:
    key = argv[1]
    lst = _get_list(ctx, key, create=True)
    for item in argv[2:]:
        lst.append(item)
    ctx.db.touch_version(key)
    ctx.mark_write(key)
    return len(lst)


@register("LPUSHX", arity=-3, write=True)
def cmd_lpushx(ctx: CommandContext, argv: list[str]) -> object:
    key = argv[1]
    lst = expect_type(ctx.db.get(key), deque)
    if lst is None:
        return 0
    for item in argv[2:]:
        lst.appendleft(item)
    ctx.db.touch_version(key)
    ctx.mark_write(key)
    return len(lst)


@register("RPUSHX", arity=-3, write=True)
def cmd_rpushx(ctx: CommandContext, argv: list[str]) -> object:
    key = argv[1]
    lst = expect_type(ctx.db.get(key), deque)
    if lst is None:
        return 0
    for item in argv[2:]:
        lst.append(item)
    ctx.db.touch_version(key)
    ctx.mark_write(key)
    return len(lst)


def _pop(ctx: CommandContext, key: str, left: bool, count: int | None) -> object:
    lst = expect_type(ctx.db.get(key), deque)
    if lst is None or len(lst) == 0:
        return [] if count is not None else None
    n = 1 if count is None else min(count, len(lst))
    popped = []
    for _ in range(n):
        popped.append(lst.popleft() if left else lst.pop())
    if len(lst) == 0:
        ctx.db.delete(key)
    else:
        ctx.db.touch_version(key)
    ctx.mark_write(key)
    return popped if count is not None else popped[0]


@register("LPOP", arity=-2, write=True)
def cmd_lpop(ctx: CommandContext, argv: list[str]) -> object:
    count = parse_int(argv[2], "count") if len(argv) > 2 else None
    return _pop(ctx, argv[1], left=True, count=count)


@register("RPOP", arity=-2, write=True)
def cmd_rpop(ctx: CommandContext, argv: list[str]) -> object:
    count = parse_int(argv[2], "count") if len(argv) > 2 else None
    return _pop(ctx, argv[1], left=False, count=count)


@register("LLEN", arity=2)
def cmd_llen(ctx: CommandContext, argv: list[str]) -> object:
    lst = expect_type(ctx.db.get(argv[1]), deque)
    return len(lst) if lst else 0


def _norm_index(i: int, n: int) -> int:
    return n + i if i < 0 else i


@register("LINDEX", arity=3)
def cmd_lindex(ctx: CommandContext, argv: list[str]) -> object:
    lst = expect_type(ctx.db.get(argv[1]), deque)
    if not lst:
        return None
    idx = _norm_index(parse_int(argv[2]), len(lst))
    if idx < 0 or idx >= len(lst):
        return None
    return lst[idx]


@register("LSET", arity=4, write=True)
def cmd_lset(ctx: CommandContext, argv: list[str]) -> object:
    key = argv[1]
    lst = expect_type(ctx.db.get(key), deque)
    if not lst:
        raise RespError("no such key")
    idx = _norm_index(parse_int(argv[2]), len(lst))
    if idx < 0 or idx >= len(lst):
        raise RespError("index out of range")
    lst[idx] = argv[3]
    ctx.db.touch_version(key)
    ctx.mark_write(key)
    from ..protocol import SimpleString
    return SimpleString("OK")


@register("LRANGE", arity=4)
def cmd_lrange(ctx: CommandContext, argv: list[str]) -> object:
    lst = expect_type(ctx.db.get(argv[1]), deque)
    if not lst:
        return []
    n = len(lst)
    start, stop = _norm_index(parse_int(argv[2]), n), _norm_index(parse_int(argv[3]), n)
    start = max(start, 0)
    stop = min(stop, n - 1)
    if start > stop:
        return []
    return list(lst)[start:stop + 1]


@register("LTRIM", arity=4, write=True)
def cmd_ltrim(ctx: CommandContext, argv: list[str]) -> object:
    key = argv[1]
    lst = expect_type(ctx.db.get(key), deque)
    from ..protocol import SimpleString
    if not lst:
        return SimpleString("OK")
    n = len(lst)
    start, stop = _norm_index(parse_int(argv[2]), n), _norm_index(parse_int(argv[3]), n)
    start = max(start, 0)
    stop = min(stop, n - 1)
    kept = list(lst)[start:stop + 1] if start <= stop else []
    if kept:
        ctx.db.set_value(key, deque(kept), keep_ttl=True)
    else:
        ctx.db.delete(key)
    ctx.mark_write(key)
    return SimpleString("OK")


@register("LREM", arity=4, write=True)
def cmd_lrem(ctx: CommandContext, argv: list[str]) -> object:
    key = argv[1]
    count = parse_int(argv[2], "count")
    value = argv[3]
    lst = expect_type(ctx.db.get(key), deque)
    if not lst:
        return 0
    items = list(lst)
    removed = 0
    if count == 0:
        # count == 0 means "remove every occurrence", scanning head to tail.
        new_items = []
        for x in items:
            if x == value:
                removed += 1
            else:
                new_items.append(x)
    elif count > 0:
        new_items = []
        for x in items:
            if x == value and removed < count:
                removed += 1
                continue
            new_items.append(x)
    else:
        new_items = []
        for x in reversed(items):
            if x == value and removed < -count:
                removed += 1
                continue
            new_items.append(x)
        new_items.reverse()
    if new_items:
        ctx.db.set_value(key, deque(new_items), keep_ttl=True)
    else:
        ctx.db.delete(key)
    if removed:
        ctx.mark_write(key)
    return removed


@register("LINSERT", arity=5, write=True)
def cmd_linsert(ctx: CommandContext, argv: list[str]) -> object:
    key, where, pivot, value = argv[1], argv[2].upper(), argv[3], argv[4]
    if where not in ("BEFORE", "AFTER"):
        raise RespError("syntax error")
    lst = expect_type(ctx.db.get(key), deque)
    if lst is None:
        return 0
    items = list(lst)
    try:
        idx = items.index(pivot)
    except ValueError:
        return -1
    insert_at = idx if where == "BEFORE" else idx + 1
    items.insert(insert_at, value)
    ctx.db.set_value(key, deque(items), keep_ttl=True)
    ctx.mark_write(key)
    return len(items)


@register("LMOVE", arity=5, write=True)
def cmd_lmove(ctx: CommandContext, argv: list[str]) -> object:
    src, dst, src_side, dst_side = argv[1], argv[2], argv[3].upper(), argv[4].upper()
    if src_side not in ("LEFT", "RIGHT") or dst_side not in ("LEFT", "RIGHT"):
        raise RespError("syntax error")
    source = expect_type(ctx.db.get(src), deque)
    if not source:
        return None
    value = source.popleft() if src_side == "LEFT" else source.pop()
    if len(source) == 0:
        ctx.db.delete(src)
    else:
        ctx.db.touch_version(src)
    dest = _get_list(ctx, dst, create=True)
    if dst_side == "LEFT":
        dest.appendleft(value)
    else:
        dest.append(value)
    ctx.db.touch_version(dst)
    ctx.mark_write(src)
    ctx.mark_write(dst)
    return value
