"""Sorted-set commands."""

from __future__ import annotations

from ..protocol import RespError
from ..valuetypes import ZSet, expect_type
from . import CommandContext, parse_float, parse_int, register


def _get_zset(ctx: CommandContext, key: str, create: bool = False) -> ZSet | None:
    value = expect_type(ctx.db.get(key), ZSet)
    if value is None and create:
        value = ZSet()
        ctx.db.set_value(key, value)
    return value


@register("ZADD", arity=-4, write=True)
def cmd_zadd(ctx: CommandContext, argv: list[str]) -> object:
    key = argv[1]
    i = 2
    nx = xx = gt = lt = ch = incr = False
    while i < len(argv) and argv[i].upper() in ("NX", "XX", "GT", "LT", "CH", "INCR"):
        opt = argv[i].upper()
        if opt == "NX":
            nx = True
        elif opt == "XX":
            xx = True
        elif opt == "GT":
            gt = True
        elif opt == "LT":
            lt = True
        elif opt == "CH":
            ch = True
        elif opt == "INCR":
            incr = True
        i += 1
    if nx and (gt or lt):
        raise RespError("GT, LT, and/or NX options at the same time are not compatible")
    if gt and lt:
        raise RespError("GT and LT options at the same time are not compatible")

    pairs = argv[i:]
    if not pairs or len(pairs) % 2 != 0:
        raise RespError("wrong number of arguments for 'zadd' command")
    if incr and len(pairs) != 2:
        raise RespError("INCR option supports a single increment-element pair")

    zs = _get_zset(ctx, key, create=not incr or True)
    added = 0
    changed = 0
    last_incr_result = None
    for j in range(0, len(pairs), 2):
        score = parse_float(pairs[j], "score")
        member = pairs[j + 1]
        old = zs.score(member)
        if incr:
            if (nx and old is not None) or (xx and old is None):
                last_incr_result = None
                continue
            new_score = (old or 0.0) + score
            if (gt and old is not None and new_score <= old) or (lt and old is not None and new_score >= old):
                last_incr_result = None
                continue
            zs.add(member, new_score)
            last_incr_result = new_score
            continue
        if old is None:
            if xx:
                continue
            zs.add(member, score)
            added += 1
            changed += 1
        else:
            if nx:
                continue
            if gt and score <= old:
                continue
            if lt and score >= old:
                continue
            if score != old:
                zs.add(member, score)
                changed += 1

    if len(zs) == 0:
        ctx.db.delete(key)
    else:
        ctx.db.touch_version(key)
    ctx.mark_write(key)

    if incr:
        return _fmt_score(last_incr_result) if last_incr_result is not None else None
    return changed if ch else added


@register("ZREM", arity=-3, write=True)
def cmd_zrem(ctx: CommandContext, argv: list[str]) -> object:
    key = argv[1]
    zs = _get_zset(ctx, key)
    if not zs:
        return 0
    removed = 0
    for member in argv[2:]:
        if zs.remove(member):
            removed += 1
    if removed:
        if len(zs) == 0:
            ctx.db.delete(key)
        else:
            ctx.db.touch_version(key)
        ctx.mark_write(key)
    return removed


@register("ZSCORE", arity=3)
def cmd_zscore(ctx: CommandContext, argv: list[str]) -> object:
    zs = _get_zset(ctx, argv[1])
    if not zs:
        return None
    score = zs.score(argv[2])
    return _fmt_score(score) if score is not None else None


@register("ZMSCORE", arity=-3)
def cmd_zmscore(ctx: CommandContext, argv: list[str]) -> object:
    zs = _get_zset(ctx, argv[1])
    return [(_fmt_score(zs.score(m)) if zs and zs.score(m) is not None else None) for m in argv[2:]]


@register("ZCARD", arity=2)
def cmd_zcard(ctx: CommandContext, argv: list[str]) -> object:
    zs = _get_zset(ctx, argv[1])
    return len(zs) if zs else 0


@register("ZINCRBY", arity=4, write=True)
def cmd_zincrby(ctx: CommandContext, argv: list[str]) -> object:
    key, delta, member = argv[1], parse_float(argv[2], "increment"), argv[3]
    zs = _get_zset(ctx, key, create=True)
    new_score = (zs.score(member) or 0.0) + delta
    zs.add(member, new_score)
    ctx.db.touch_version(key)
    ctx.mark_write(key)
    return _fmt_score(new_score)


def _fmt_score(score: float) -> str:
    if score == int(score) and abs(score) < 1e15:
        return str(int(score))
    return repr(score)


def _emit(ctx: CommandContext, pairs: list[tuple[str, float]], with_scores: bool) -> list:
    if not with_scores:
        return [m for m, _ in pairs]
    if ctx.protocol_version >= 3:
        return [[m, s] for m, s in pairs]
    out = []
    for m, s in pairs:
        out.append(m)
        out.append(_fmt_score(s))
    return out


@register("ZRANGE", arity=-4)
def cmd_zrange(ctx: CommandContext, argv: list[str]) -> object:
    zs = _get_zset(ctx, argv[1])
    with_scores = len(argv) > 4 and argv[4].upper() == "WITHSCORES"
    if not zs:
        return []
    start, stop = parse_int(argv[2]), parse_int(argv[3])
    return _emit(ctx, zs.range_by_rank(start, stop), with_scores)


@register("ZREVRANGE", arity=-4)
def cmd_zrevrange(ctx: CommandContext, argv: list[str]) -> object:
    zs = _get_zset(ctx, argv[1])
    with_scores = len(argv) > 4 and argv[4].upper() == "WITHSCORES"
    if not zs:
        return []
    start, stop = parse_int(argv[2]), parse_int(argv[3])
    return _emit(ctx, zs.range_by_rank(start, stop, reverse=True), with_scores)


def _parse_score_bound(raw: str) -> tuple[float, bool]:
    if raw.startswith("("):
        return parse_float(raw[1:], "score bound"), True
    if raw == "-inf":
        return float("-inf"), False
    if raw == "+inf":
        return float("inf"), False
    return parse_float(raw, "score bound"), False


def _parse_limit(argv: list[str], start_idx: int) -> tuple[bool, int, int | None]:
    with_scores = False
    offset, count = 0, None
    i = start_idx
    while i < len(argv):
        opt = argv[i].upper()
        if opt == "WITHSCORES":
            with_scores = True
            i += 1
        elif opt == "LIMIT":
            offset = parse_int(argv[i + 1], "offset")
            count = parse_int(argv[i + 2], "count")
            i += 3
        else:
            raise RespError("syntax error")
    return with_scores, offset, count


@register("ZRANGEBYSCORE", arity=-4)
def cmd_zrangebyscore(ctx: CommandContext, argv: list[str]) -> object:
    zs = _get_zset(ctx, argv[1])
    lo, lo_ex = _parse_score_bound(argv[2])
    hi, hi_ex = _parse_score_bound(argv[3])
    with_scores, offset, count = _parse_limit(argv, 4)
    if not zs:
        return []
    pairs = zs.range_by_score(lo, hi, lo_ex, hi_ex, offset, count)
    return _emit(ctx, pairs, with_scores)


@register("ZREVRANGEBYSCORE", arity=-4)
def cmd_zrevrangebyscore(ctx: CommandContext, argv: list[str]) -> object:
    zs = _get_zset(ctx, argv[1])
    hi, hi_ex = _parse_score_bound(argv[2])
    lo, lo_ex = _parse_score_bound(argv[3])
    with_scores, offset, count = _parse_limit(argv, 4)
    if not zs:
        return []
    pairs = zs.range_by_score(lo, hi, lo_ex, hi_ex, offset, count, reverse=True)
    return _emit(ctx, pairs, with_scores)


@register("ZCOUNT", arity=4)
def cmd_zcount(ctx: CommandContext, argv: list[str]) -> object:
    zs = _get_zset(ctx, argv[1])
    if not zs:
        return 0
    lo, lo_ex = _parse_score_bound(argv[2])
    hi, hi_ex = _parse_score_bound(argv[3])
    return zs.count_by_score(lo, hi, lo_ex, hi_ex)


@register("ZRANK", arity=-3)
def cmd_zrank(ctx: CommandContext, argv: list[str]) -> object:
    zs = _get_zset(ctx, argv[1])
    if not zs:
        return None
    r = zs.rank(argv[2])
    if r is None:
        return None
    if len(argv) > 3 and argv[3].upper() == "WITHSCORE":
        return [r, _fmt_score(zs.score(argv[2]))]
    return r


@register("ZREVRANK", arity=-3)
def cmd_zrevrank(ctx: CommandContext, argv: list[str]) -> object:
    zs = _get_zset(ctx, argv[1])
    if not zs:
        return None
    r = zs.rank(argv[2], reverse=True)
    if r is None:
        return None
    if len(argv) > 3 and argv[3].upper() == "WITHSCORE":
        return [r, _fmt_score(zs.score(argv[2]))]
    return r


def _pop_extreme(ctx: CommandContext, key: str, lowest: bool, count: int) -> list:
    zs = _get_zset(ctx, key)
    if not zs:
        return []
    n = min(count, len(zs))
    pairs = zs.range_by_rank(0, n - 1) if lowest else zs.range_by_rank(0, n - 1, reverse=True)
    for m, _ in pairs:
        zs.remove(m)
    if len(zs) == 0:
        ctx.db.delete(key)
    else:
        ctx.db.touch_version(key)
    ctx.mark_write(key)
    return _emit(ctx, pairs, with_scores=True)


@register("ZPOPMIN", arity=-2, write=True)
def cmd_zpopmin(ctx: CommandContext, argv: list[str]) -> object:
    count = parse_int(argv[2], "count") if len(argv) > 2 else 1
    return _pop_extreme(ctx, argv[1], lowest=True, count=count)


@register("ZPOPMAX", arity=-2, write=True)
def cmd_zpopmax(ctx: CommandContext, argv: list[str]) -> object:
    count = parse_int(argv[2], "count") if len(argv) > 2 else 1
    return _pop_extreme(ctx, argv[1], lowest=False, count=count)
