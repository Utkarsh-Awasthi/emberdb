"""CREATE INDEX/FIND, VECTOR.*, CDC.*, HOOK.* — the non-Redis commands."""

from __future__ import annotations

from ..protocol import RespError, SimpleString
from . import CommandContext, parse_float, parse_int, register
from .json_docs import _parse_json_arg

_COMPARISON_OPS = ("=", "!=", ">=", "<=", ">", "<")


@register("CREATE", arity=-2, write=True, admin=True)
def cmd_create(ctx: CommandContext, argv: list[str]) -> object:
    if len(argv) < 2 or argv[1].upper() != "INDEX":
        raise RespError("syntax error, expected: CREATE INDEX <name> ON <pattern> FIELD <field>")
    if len(argv) != 7 or argv[3].upper() != "ON" or argv[5].upper() != "FIELD":
        raise RespError("syntax error, expected: CREATE INDEX <name> ON <pattern> FIELD <field>")
    name, pattern, field_name = argv[2], argv[4], argv[6]
    backfilled = ctx.engine.indexes.create_index(name, ctx.db_index, pattern, field_name)
    return backfilled


@register("DROP", arity=-2, write=True, admin=True)
def cmd_drop(ctx: CommandContext, argv: list[str]) -> object:
    if len(argv) != 3 or argv[1].upper() != "INDEX":
        raise RespError("syntax error, expected: DROP INDEX <name>")
    return 1 if ctx.engine.indexes.drop_index(argv[2]) else 0


@register("INDEXES", arity=1)
def cmd_indexes(ctx: CommandContext, argv: list[str]) -> object:
    return ctx.engine.indexes.list_indexes()


@register("FIND", arity=-4)
def cmd_find(ctx: CommandContext, argv: list[str]) -> object:
    index_name = argv[1]
    if argv[2].upper() != "WHERE":
        raise RespError("syntax error, expected: FIND <index> WHERE <field> <op> <value> [AND ...]")
    tokens = argv[3:]
    conditions = []
    i = 0
    while i < len(tokens):
        if i + 3 > len(tokens):
            raise RespError("syntax error in WHERE clause")
        field_name, op, value = tokens[i], tokens[i + 1], tokens[i + 2]
        if op not in _COMPARISON_OPS:
            raise RespError(f"unsupported operator '{op}', expected one of {', '.join(_COMPARISON_OPS)}")
        conditions.append((field_name, op, value))
        i += 3
        if i < len(tokens):
            if tokens[i].upper() != "AND":
                raise RespError("syntax error, expected AND between conditions")
            i += 1
    return ctx.engine.indexes.find(index_name, conditions)


# -- VECTOR.* -----------------------------------------------------------------

def _fmt_score(score: float) -> str:
    if score == int(score) and abs(score) < 1e15:
        return str(int(score))
    return repr(score)


@register("VECTOR.SET", arity=-5, write=True)
def cmd_vector_set(ctx: CommandContext, argv: list[str]) -> object:
    namespace, key = argv[1], argv[2]
    if argv[3].upper() != "VALUES":
        raise RespError("syntax error, expected: VECTOR.SET <namespace> <key> VALUES <v1> <v2> ... [ATTRS <json>]")
    i = 4
    values = []
    while i < len(argv) and argv[i].upper() != "ATTRS":
        values.append(parse_float(argv[i], "vector component"))
        i += 1
    attrs = None
    if i < len(argv) and argv[i].upper() == "ATTRS":
        attrs = _parse_json_arg(argv[i + 1])
        if not isinstance(attrs, dict):
            raise RespError("ATTRS must be a JSON object")
    if not values:
        raise RespError("VECTOR.SET requires at least one value")
    ctx.engine.vectors.upsert(namespace, key, values, attrs)
    ctx.mark_write(key)
    return SimpleString("OK")


@register("VECTOR.DEL", arity=3, write=True)
def cmd_vector_del(ctx: CommandContext, argv: list[str]) -> object:
    namespace, key = argv[1], argv[2]
    removed = ctx.engine.vectors.remove(namespace, key)
    if removed:
        ctx.mark_write(key)
    return 1 if removed else 0


@register("VECTOR.SEARCH", arity=-4)
def cmd_vector_search(ctx: CommandContext, argv: list[str]) -> object:
    namespace = argv[1]
    if argv[2].upper() != "VALUES":
        raise RespError("syntax error, expected: VECTOR.SEARCH <namespace> VALUES <v1> ... [K <k>] [METRIC <name>]")
    i = 3
    values = []
    while i < len(argv) and argv[i].upper() not in ("K", "METRIC"):
        values.append(parse_float(argv[i], "vector component"))
        i += 1
    k, metric = 10, "cosine"
    while i < len(argv):
        opt = argv[i].upper()
        if opt == "K":
            k = parse_int(argv[i + 1], "k")
            i += 2
        elif opt == "METRIC":
            metric = argv[i + 1].lower()
            i += 2
        else:
            raise RespError("syntax error")
    hits = ctx.engine.vectors.search(namespace, values, k=k, metric=metric)
    out = []
    for hit in hits:
        out.append(hit.key)
        out.append(_fmt_score(hit.score))
    return out


@register("VECTOR.DESCRIBE", arity=1)
def cmd_vector_describe(ctx: CommandContext, argv: list[str]) -> object:
    return ctx.engine.vectors.describe()


# -- CDC.* ----------------------------------------------------------------

@register("CDC.SINCE", arity=-2)
def cmd_cdc_since(ctx: CommandContext, argv: list[str]) -> object:
    offset = parse_int(argv[1], "offset")
    limit = parse_int(argv[2], "limit") if len(argv) > 2 else 1000
    events = ctx.engine.cdc.since(offset, limit)
    return [e.to_dict() for e in events]


@register("CDC.STATUS", arity=1)
def cmd_cdc_status(ctx: CommandContext, argv: list[str]) -> object:
    return ctx.engine.cdc.status()


@register("CDC.TAIL", arity=1, pubsub_context_ok=True)
def cmd_cdc_tail(ctx: CommandContext, argv: list[str]) -> object:
    """Push every new CDC event to this connection. Idempotent."""
    conn = ctx.conn_state
    if getattr(conn, "cdc_tail_active", False):
        return SimpleString("OK")

    def _on_event(event) -> None:
        conn.deliver_cdc_event(event.to_dict())

    conn.cdc_tail_callback = _on_event
    conn.cdc_tail_active = True
    ctx.engine.cdc.tail_subscribe(_on_event)
    return SimpleString("OK")


@register("CDC.UNTAIL", arity=1, pubsub_context_ok=True)
def cmd_cdc_untail(ctx: CommandContext, argv: list[str]) -> object:
    conn = ctx.conn_state
    cb = getattr(conn, "cdc_tail_callback", None)
    if cb is not None:
        ctx.engine.cdc.tail_unsubscribe(cb)
        conn.cdc_tail_callback = None
        conn.cdc_tail_active = False
    return SimpleString("OK")


# -- HOOK.* (TTL expiry webhooks) --------------------------------------------

@register("HOOK.ONEXPIRE", arity=6, write=True, admin=True)
def cmd_hook_onexpire(ctx: CommandContext, argv: list[str]) -> object:
    if argv[2].upper() != "ON" or argv[4].upper() != "CALL":
        raise RespError("syntax error, expected: HOOK.ONEXPIRE <name> ON <pattern> CALL <url>")
    name, pattern, url = argv[1], argv[3], argv[5]
    ctx.engine.hooks.register(name, ctx.db_index, pattern, url, event="expired")
    return SimpleString("OK")


@register("HOOK.DEL", arity=2, write=True, admin=True)
def cmd_hook_del(ctx: CommandContext, argv: list[str]) -> object:
    return 1 if ctx.engine.hooks.unregister(argv[1]) else 0


@register("HOOK.LIST", arity=1)
def cmd_hook_list(ctx: CommandContext, argv: list[str]) -> object:
    return ctx.engine.hooks.list_hooks()


# -- BRANCH.* (dataset branching) --
# Not flagged write: branch ops must never hit the AOF or replicas themselves.
# Only a merge's effects on main are persisted, via EMBER.RESTORE.

@register("BRANCH.CREATE", arity=-2, admin=True)
def cmd_branch_create(ctx: CommandContext, argv: list[str]) -> object:
    """BRANCH.CREATE <name> [FROM <branch>]"""
    name = argv[1]
    from_branch = None
    if len(argv) > 2:
        if len(argv) != 4 or argv[2].upper() != "FROM":
            raise RespError("syntax error, expected: BRANCH.CREATE <name> [FROM <branch>]")
        from_branch = argv[3]
    return ctx.engine.create_branch(name, from_branch)


@register("BRANCH.SWITCH", arity=2, admin=True)
def cmd_branch_switch(ctx: CommandContext, argv: list[str]) -> object:
    ctx.engine.switch_branch(argv[1])
    return SimpleString("OK")


@register("BRANCH.LIST", arity=1)
def cmd_branch_list(ctx: CommandContext, argv: list[str]) -> object:
    return ctx.engine.branches.list_branches()


@register("BRANCH.CURRENT", arity=1)
def cmd_branch_current(ctx: CommandContext, argv: list[str]) -> object:
    return ctx.engine.branches.current


@register("BRANCH.DROP", arity=2, admin=True)
def cmd_branch_drop(ctx: CommandContext, argv: list[str]) -> object:
    ctx.engine.drop_branch(argv[1])
    return SimpleString("OK")


@register("BRANCH.DIFF", arity=-1)
def cmd_branch_diff(ctx: CommandContext, argv: list[str]) -> object:
    """BRANCH.DIFF [branch] — what changed since the branch's fork point."""
    name = argv[1] if len(argv) > 1 else None
    return ctx.engine.branches.diff(name)


@register("BRANCH.MERGE", arity=-2, admin=True)
def cmd_branch_merge(ctx: CommandContext, argv: list[str]) -> object:
    """BRANCH.MERGE <source> [INTO <branch>] [STRATEGY fail|theirs|ours]"""
    source = argv[1]
    into = None
    strategy = "fail"
    i = 2
    while i < len(argv):
        opt = argv[i].upper()
        if opt == "INTO":
            if i + 1 >= len(argv):
                raise RespError("syntax error")
            into = argv[i + 1]
            i += 2
        elif opt == "STRATEGY":
            if i + 1 >= len(argv):
                raise RespError("syntax error")
            strategy = argv[i + 1].lower()
            i += 2
        else:
            raise RespError("syntax error, expected: BRANCH.MERGE <source> [INTO <branch>] [STRATEGY fail|theirs|ours]")
    return ctx.engine.merge_branch(source, into, strategy)


# -- EMBER.RESTORE: internal, replayable "set this key to this exact value" --
# Emitted by branch merges into main so they reach the AOF and replicas. Works
# for every value type, unlike reconstructing via SET/HSET/ZADD sequences.

@register("EMBER.RESTORE", arity=-3, write=True, admin=True)
def cmd_ember_restore(ctx: CommandContext, argv: list[str]) -> object:
    """EMBER.RESTORE <key> <serialized-json> [PXAT <ms>]"""
    import json
    from ..persistence import _deserialize_value
    key = argv[1]
    try:
        value = _deserialize_value(json.loads(argv[2]))
    except (ValueError, KeyError, TypeError) as e:
        raise RespError(f"invalid EMBER.RESTORE payload: {e}")
    ctx.db.set_value(key, value)
    if len(argv) == 5 and argv[3].upper() == "PXAT":
        ctx.db.set_expire_at(key, int(argv[4]) / 1000.0)
    elif len(argv) != 3:
        raise RespError("syntax error, expected: EMBER.RESTORE <key> <payload> [PXAT <ms>]")
    ctx.mark_write(key)
    return SimpleString("OK")
