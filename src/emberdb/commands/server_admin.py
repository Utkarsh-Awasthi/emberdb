"""Connection lifecycle and server introspection: PING/HELLO/AUTH, INFO/CONFIG/CLIENT/ACL."""

from __future__ import annotations

import asyncio
import time

from ..protocol import RespError, SimpleString, Verbatim
from . import CommandContext, parse_int, register


@register("PING", arity=-1, no_auth_required=True, pubsub_context_ok=True)
def cmd_ping(ctx: CommandContext, argv: list[str]) -> object:
    if len(argv) > 2:
        raise RespError("wrong number of arguments for 'ping' command")
    if len(argv) == 2:
        return argv[1]
    return SimpleString("PONG")


@register("ECHO", arity=2)
def cmd_echo(ctx: CommandContext, argv: list[str]) -> object:
    return argv[1]


@register("SELECT", arity=2)
def cmd_select(ctx: CommandContext, argv: list[str]) -> object:
    idx = parse_int(argv[1], "index")
    if idx < 0 or idx >= len(ctx.engine.databases):
        raise RespError("DB index is out of range")
    ctx.conn_state.selected_db = idx
    return SimpleString("OK")


@register("SWAPDB", arity=3, write=True, admin=True)
def cmd_swapdb(ctx: CommandContext, argv: list[str]) -> object:
    i1, i2 = parse_int(argv[1], "index"), parse_int(argv[2], "index")
    dbs = ctx.engine.databases
    if not (0 <= i1 < len(dbs)) or not (0 <= i2 < len(dbs)):
        raise RespError("DB index is out of range")
    dbs[i1], dbs[i2] = dbs[i2], dbs[i1]
    return SimpleString("OK")


@register("AUTH", arity=-2, no_auth_required=True)
def cmd_auth(ctx: CommandContext, argv: list[str]) -> object:
    if len(argv) == 2:
        username, password = "default", argv[1]
    elif len(argv) == 3:
        username, password = argv[1], argv[2]
    else:
        raise RespError("wrong number of arguments for 'auth' command")
    if not ctx.engine.acl.authenticate(username, password):
        raise RespError("invalid username-password pair or user is disabled", code="WRONGPASS")
    ctx.conn_state.username = username
    ctx.conn_state.authenticated = True
    return SimpleString("OK")


@register("HELLO", arity=-1, no_auth_required=True)
def cmd_hello(ctx: CommandContext, argv: list[str]) -> object:
    conn = ctx.conn_state
    proto = conn.protocol_version
    i = 1
    if len(argv) > 1 and argv[1] not in ("AUTH", "SETNAME"):
        proto = parse_int(argv[1], "protocol version")
        if proto not in (2, 3):
            raise RespError("NOPROTO unsupported protocol version", code="NOPROTO")
        i = 2
    while i < len(argv):
        opt = argv[i].upper()
        if opt == "AUTH":
            username, password = argv[i + 1], argv[i + 2]
            if not ctx.engine.acl.authenticate(username, password):
                raise RespError("invalid username-password pair or user is disabled", code="WRONGPASS")
            conn.username = username
            conn.authenticated = True
            i += 3
        elif opt == "SETNAME":
            conn.name = argv[i + 1]
            i += 2
        else:
            raise RespError("syntax error in HELLO")
    if ctx.engine.acl.needs_auth_by_default() and not getattr(conn, "authenticated", False):
        raise RespError(
            "HELLO must be called with the client already authenticated, otherwise the "
            "HELLO <proto> AUTH <user> <pass> option can be used to authenticate the client "
            "and select the RESP protocol version at the same time",
            code="NOAUTH",
        )
    conn.protocol_version = proto
    return {
        "server": "emberdb",
        "version": ctx.engine.version,
        "proto": proto,
        "id": getattr(conn, "client_id", 0),
        "mode": "standalone",
        "role": ctx.engine.replication.role,
        "modules": [],
    }


@register("RESET", arity=1, no_auth_required=True, pubsub_context_ok=True)
def cmd_reset(ctx: CommandContext, argv: list[str]) -> object:
    conn = ctx.conn_state
    for channel in list(getattr(conn, "channels", ())):
        ctx.engine.pubsub.unsubscribe(conn, channel)
    for pattern in list(getattr(conn, "patterns", ())):
        ctx.engine.pubsub.punsubscribe(conn, pattern)
    conn.channels = set()
    conn.patterns = set()
    conn.in_multi = False
    conn.multi_queue = []
    conn.multi_error = False
    conn.watched = {}
    conn.selected_db = 0
    conn.protocol_version = 2
    conn.username = "default"
    conn.authenticated = not ctx.engine.acl.needs_auth_by_default()
    return SimpleString("RESET")


@register("QUIT", arity=-1, no_auth_required=True, pubsub_context_ok=True)
def cmd_quit(ctx: CommandContext, argv: list[str]) -> object:
    ctx.conn_state.should_close = True
    return SimpleString("OK")


@register("TIME", arity=1)
def cmd_time(ctx: CommandContext, argv: list[str]) -> object:
    now = time.time()
    return [str(int(now)), str(int((now % 1) * 1_000_000))]


# -- INFO / COMMAND --

@register("INFO", arity=-1)
def cmd_info(ctx: CommandContext, argv: list[str]) -> object:
    import os
    m = ctx.engine.metrics.snapshot()
    lines = [
        "# Server",
        "emberdb_version:" + ctx.engine.version,
        "redis_version:7.4.0-emberdb-compat",  # lets client libs that gate features on version string proceed
        "process_id:" + str(os.getpid()),
        "tcp_port:" + str(ctx.engine.config.get("port", 7379)),
        "uptime_in_seconds:" + str(int(m["uptime_seconds"])),
        "",
        "# Clients",
        "connected_clients:" + str(m["connected_clients"]),
        "",
        "# Memory",
        "used_memory:" + str(sum(db.approx_bytes for db in ctx.engine.databases)),
        "maxmemory:" + str(ctx.engine.config.get("maxmemory", 0)),
        "maxmemory_policy:" + str(ctx.engine.config.get("maxmemory_policy", "noeviction")),
        "",
        "# Persistence",
        "aof_enabled:" + ("1" if ctx.engine.aof else "0"),
        "last_save_time:" + str(int(ctx.engine.last_save_time)),
        "",
        "# Stats",
        "total_connections_received:" + str(m["total_connections_received"]),
        "total_commands_processed:" + str(m["commands_processed"]),
        "instantaneous_ops_per_sec:" + str(m["ops_per_sec"]),
        "keyspace_hits:" + str(m["keyspace_hits"]),
        "keyspace_misses:" + str(m["keyspace_misses"]),
        "expired_keys:" + str(m["expired_keys"]),
        "evicted_keys:" + str(m["evicted_keys"]),
        "",
        "# Replication",
        "role:" + ctx.engine.replication.role,
        "connected_replicas:" + str(len(ctx.engine.replication.replicas)),
        "",
        "# Keyspace",
    ]
    for i, db in enumerate(ctx.engine.databases):
        if db.size() > 0:
            lines.append(f"db{i}:keys={db.size()},expires={len(db.expires)}")
    return Verbatim("\n".join(lines) + "\n")


@register("COMMAND", arity=-1)
def cmd_command(ctx: CommandContext, argv: list[str]) -> object:
    from . import COMMAND_TABLE
    sub = argv[1].upper() if len(argv) > 1 else "LIST"
    if sub == "COUNT":
        return len(COMMAND_TABLE)
    if sub == "LIST":
        return sorted(name.lower() for name in COMMAND_TABLE)
    if sub == "DOCS":
        return {}
    return []


# -- CONFIG --

@register("CONFIG", arity=-2, admin=True)
def cmd_config(ctx: CommandContext, argv: list[str]) -> object:
    import fnmatch
    sub = argv[1].upper()
    cfg = ctx.engine.config
    if sub == "GET":
        pattern = argv[2] if len(argv) > 2 else "*"
        return {k: str(v) for k, v in cfg.items() if fnmatch.fnmatchcase(k, pattern)}
    if sub == "SET":
        if len(argv) != 4:
            raise RespError("wrong number of arguments for 'config|set' command")
        key, value = argv[2], argv[3]
        if key not in cfg:
            raise RespError(f"Unknown option or number of arguments for CONFIG SET - '{key}'")
        current = cfg[key]
        if isinstance(current, bool):
            cfg[key] = value.lower() in ("yes", "true", "1")
        elif isinstance(current, int):
            cfg[key] = parse_int(value, key)
        else:
            cfg[key] = value
        if key == "requirepass":
            # also update the actual ACL password, not just the reported value
            if value:
                ctx.engine.acl.set_password("default", value)
            else:
                ctx.engine.acl.clear_password("default")
        return SimpleString("OK")
    if sub == "REWRITE":
        return SimpleString("OK")
    raise RespError(f"Unknown CONFIG subcommand '{argv[1]}'")


# -- CLIENT --

@register("CLIENT", arity=-2)
def cmd_client(ctx: CommandContext, argv: list[str]) -> object:
    sub = argv[1].upper()
    conn = ctx.conn_state
    if sub == "GETNAME":
        return getattr(conn, "name", "") or ""
    if sub == "SETNAME":
        conn.name = argv[2]
        return SimpleString("OK")
    if sub == "ID":
        return getattr(conn, "client_id", 0)
    if sub == "LIST":
        lines = []
        for c in ctx.engine.clients.values():
            lines.append(
                f"id={getattr(c, 'client_id', 0)} addr={getattr(c, 'addr', '?')} "
                f"name={getattr(c, 'name', '') or ''} db={getattr(c, 'selected_db', 0)} "
                f"resp={getattr(c, 'protocol_version', 2)} "
                f"lib-name={getattr(c, 'lib_info', {}).get('LIB-NAME', '')} "
                f"lib-ver={getattr(c, 'lib_info', {}).get('LIB-VER', '')}"
            )
        return Verbatim("\n".join(lines) + ("\n" if lines else ""))
    if sub == "INFO":
        return (
            f"id={getattr(conn, 'client_id', 0)} addr={getattr(conn, 'addr', '?')} "
            f"name={getattr(conn, 'name', '') or ''} db={conn.selected_db} resp={conn.protocol_version} "
            f"lib-name={getattr(conn, 'lib_info', {}).get('LIB-NAME', '')} "
            f"lib-ver={getattr(conn, 'lib_info', {}).get('LIB-VER', '')}"
        )
    if sub == "SETINFO":
        if len(argv) != 4:
            raise RespError("wrong number of arguments for 'client|setinfo' command")
        attr, value = argv[2].upper(), argv[3]
        if attr not in ("LIB-NAME", "LIB-VER"):
            raise RespError(f"Unrecognized option '{argv[2]}'")
        if not hasattr(conn, "lib_info"):
            conn.lib_info = {}
        conn.lib_info[attr] = value
        return SimpleString("OK")
    if sub in ("NO-EVICT", "NO-TOUCH", "UNPAUSE", "PAUSE"):
        return SimpleString("OK")
    if sub == "MAINT_NOTIFICATIONS":
        # redis-py 8.x sends this automatically on every connect, opting into
        # Enterprise/Cloud maintenance-failover notifications. Not something
        # a standalone server has any notifications to send, but the client
        # only degrades gracefully if we answer OK rather than reject it.
        return SimpleString("OK")
    raise RespError(f"Unknown CLIENT subcommand '{argv[1]}'")


# -- SLOWLOG --

@register("SLOWLOG", arity=-2)
def cmd_slowlog(ctx: CommandContext, argv: list[str]) -> object:
    sub = argv[1].upper()
    if sub == "GET":
        count = parse_int(argv[2]) if len(argv) > 2 else 10
        entries = ctx.engine.metrics.slowlog_entries(count)
        return [[e["ts"], e["duration_ms"], e["argv"]] for e in entries]
    if sub == "RESET":
        ctx.engine.metrics.slowlog_reset()
        return SimpleString("OK")
    if sub == "LEN":
        return len(ctx.engine.metrics.slowlog)
    raise RespError(f"Unknown SLOWLOG subcommand '{argv[1]}'")


# -- ACL --

@register("ACL", arity=-2, admin=True)
def cmd_acl(ctx: CommandContext, argv: list[str]) -> object:
    sub = argv[1].upper()
    acl = ctx.engine.acl
    if sub == "WHOAMI":
        return ctx.conn_state.username
    if sub == "LIST":
        out = []
        for u in acl.list_users():
            cmds = "+@all" if u["commands"] == ["*"] else "+" + " +".join(u["commands"])
            out.append(f"user {u['username']} {'on' if u['enabled'] else 'off'} {cmds} ~{u['keys']}")
        return out
    if sub == "CAT":
        from . import COMMAND_TABLE
        return sorted({name.split(".")[0] for name in COMMAND_TABLE})
    if sub == "GETUSER":
        if argv[2] not in acl.users:
            return None
        return acl.describe_user(argv[2])
    if sub == "SETUSER":
        username = argv[2]
        kwargs: dict = {}
        rest = argv[3:]
        cmds = None
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok == ">":
                kwargs["password"] = rest[i + 1]
                i += 2
            elif tok == "on":
                kwargs["enabled"] = True
                i += 1
            elif tok == "off":
                kwargs["enabled"] = False
                i += 1
            elif tok.startswith("~"):
                kwargs["key_pattern"] = tok[1:]
                i += 1
            elif tok.startswith("+") or tok == "allcommands":
                cmds = cmds or set()
                cmds.add("*" if tok == "allcommands" else tok[1:].upper())
                i += 1
            else:
                i += 1
        if cmds:
            kwargs["command_patterns"] = cmds
        acl.create_or_update_user(username, **kwargs)
        return SimpleString("OK")
    if sub == "DELUSER":
        removed = 0
        for username in argv[2:]:
            if acl.delete_user(username):
                removed += 1
        return removed
    raise RespError(f"Unknown ACL subcommand '{argv[1]}'")


# -- persistence control --

@register("SAVE", arity=1, admin=True)
def cmd_save(ctx: CommandContext, argv: list[str]) -> object:
    ctx.engine.save_snapshot_now()
    return SimpleString("OK")


@register("BGSAVE", arity=-1, admin=True)
def cmd_bgsave(ctx: CommandContext, argv: list[str]) -> object:
    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, ctx.engine.save_snapshot_now)
    except RuntimeError:
        ctx.engine.save_snapshot_now()  # no running loop (e.g. during AOF replay) — just do it inline
    return SimpleString("Background saving started")


@register("LASTSAVE", arity=1)
def cmd_lastsave(ctx: CommandContext, argv: list[str]) -> object:
    return int(ctx.engine.last_save_time)


# -- replication --

@register("REPLICAOF", arity=3, admin=True)
def cmd_replicaof(ctx: CommandContext, argv: list[str]) -> object:
    host, port = argv[1], argv[2]
    if host.upper() == "NO" and port.upper() == "ONE":
        ctx.engine.replication.become_leader()
    else:
        ctx.engine.replication.replicate_from(host, parse_int(port, "port"))
    return SimpleString("OK")


@register("SLAVEOF", arity=3, admin=True)
def cmd_slaveof(ctx: CommandContext, argv: list[str]) -> object:
    return cmd_replicaof(ctx, argv)


@register("SYNC", arity=1, admin=True)
def cmd_sync(ctx: CommandContext, argv: list[str]) -> object:
    """Issued by a follower's replication loop (replication.py), never by
    a normal client. The reply is a sentinel, not real RESP — see
    ReplicaSyncStart's docstring (protocol.py) and server.py's connection
    loop, which is the piece that actually streams the dataset."""
    from ..protocol import ReplicaSyncStart
    ctx.engine.replication.add_replica(ctx.conn_state)
    return ReplicaSyncStart()
