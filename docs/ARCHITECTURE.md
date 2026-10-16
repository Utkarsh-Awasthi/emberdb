# Architecture

This document describes how EmberDB's pieces fit together at the module level. For a step-by-step trace of what actually happens when a specific request runs, see [`CODE_FLOW.md`](CODE_FLOW.md). For *why* things are built this way rather than some other way, see [`DESIGN_DECISIONS.md`](DESIGN_DECISIONS.md).

## The layering, top to bottom

```
                    ┌─────────────┐       ┌──────────────────┐
   redis-cli /      │  server.py  │       │   http_api.py     │   a browser tab
   any RESP client ─┤  (RESP TCP) │       │ (HTTP + WebSocket) ├─  (dashboard)
                    └──────┬──────┘       └─────────┬─────────┘
                           │                          │
                           │   both are thin transports:
                           │   parse bytes -> call engine -> encode reply
                           │                          │
                           └────────────┬─────────────┘
                                        │
                                ┌───────▼────────┐
                                │   engine.py     │   EmberEngine — the ONE
                                │  (dispatch)     │   place every command
                                └───────┬────────┘   actually runs
                                        │
                     ┌──────────┬───────┼───────┬──────────┬──────────┐
                     │          │        │       │          │          │
                ┌────▼───┐ ┌───▼────┐ ┌─▼────┐ ┌▼───────┐ ┌▼───────┐ ┌▼─────────┐
                │commands│ │database│ │ acl  │ │eviction│ │ expiry │ │replication│
                │  /*.py │ │  .py   │ │ .py  │ │  .py   │ │  .py   │ │   .py     │
                └────────┘ └────────┘ └──────┘ └────────┘ └────────┘ └──────────┘
                     │          │
                     │     ┌────┴─────┬───────────┬────────────┬─────────────┐
                     │     │          │            │            │             │
              (reads/writes│    ┌─────▼────┐ ┌─────▼─────┐┌─────▼─────┐┌──────▼──────┐
               via db.get/  │   │persistence│ │  pubsub   ││    cdc    ││  indexing / │
               set_value/…) │   │   .py     │ │   .py     ││   .py     ││ vectorsearch│
                             │   └──────────┘ └───────────┘└───────────┘└─────────────┘
                             │
                       (also used directly
                        by commands for
                        type-specific logic)
```

Every arrow in that diagram is a plain Python import and a plain Python function/method call — there's no message bus, no RPC, no serialization boundary between these pieces. The only place bytes actually go over a socket is at the very top (`server.py`, `http_api.py`) and, for replication, in `replication.py`'s follower loop.

## The core objects

### `protocol.py` — the wire format
The only module that knows what `*3\r\n$3\r\nSET\r\n...` means. `read_command()` turns bytes into a plain `list[bytes]`; `encode_reply()` turns a plain Python value (str, int, list, dict, a handful of small wrapper types for RESP's finer distinctions) back into bytes. Nothing above this layer touches framing.

### `database.py` — the keyspace
One `Database` instance per logical numbered database (`SELECT 0..15` by default). Holds `data` (key → value), `expires` (key → absolute expiry time), and `meta` (key → `KeyMeta`: access recency/frequency for eviction, a version counter for `WATCH`, a cached size estimate). Every command handler reads and writes through this object's methods — `get`, `set_value`, `delete`, and so on — never by touching `data` directly, so expiry checks, version bumps, and memory accounting happen in exactly one place.

### `valuetypes.py` — what a key can hold
The seven value types: Python's own `str`/`deque`/`dict`/`set` for String/List/Hash/Set, plus three types this project defines — `ZSet` (skip list + dict, mirroring Redis's own zset internals; see `skiplist.py`), `JSONValue`, and `VectorValue`.

### `commands/` — one file per type
`strings.py`, `lists.py`, `hashes.py`, `sets.py`, `zsets.py`, `generic.py` (type-agnostic commands like `DEL`/`EXPIRE`/`SCAN`), `transactions.py`, `pubsub_cmds.py`, `server_admin.py`, `json_docs.py`, `query_ext.py` (the EmberDB-specific commands: indexing, vectors, CDC, hooks). This mirrors how real Redis organizes its own source (`t_string.c`, `t_list.c`, ...). Every handler is a plain function `(ctx: CommandContext, argv: list[str]) -> reply`, registered into a global table via a `@register(...)` decorator — see `commands/__init__.py`.

### `engine.py` — where it all comes together
`EmberEngine` owns the list of `Database`s and an instance of every subsystem below, and `execute()` is the single dispatch point every command — from a live client, from AOF replay at startup, or from a replication stream — runs through. See [`CODE_FLOW.md`](CODE_FLOW.md) for exactly what that method does.

### The subsystems `EmberEngine` wires together
| Module | Responsibility |
|---|---|
| `acl.py` | AUTH, per-user command and key-pattern permissions |
| `eviction.py` | `maxmemory` enforcement, all 8 Redis eviction policies |
| `expiry.py` | background active-expiration sweep (passive/lazy expiry lives in `database.py` itself) |
| `persistence.py` | JSON snapshot (save/load) and the append-only file (write/replay), including transaction batching |
| `pubsub.py` | channel + pattern subscriptions, transport-agnostic (a RESP connection and a dashboard WebSocket are both just `Subscriber`s) |
| `cdc.py` | the change-data-capture log: a bounded in-memory ring plus a durable file, offset-addressable |
| `indexing.py` | secondary indexes and the `CREATE INDEX` / `FIND` query engine |
| `vectorsearch.py` | brute-force vector similarity search |
| `hooks.py` | TTL expiry webhooks (fire-and-forget HTTP POST on a background thread) |
| `metrics.py` | command counters, hit/miss ratio, ops/sec history, slowlog |
| `replication.py` | leader-follower replication (full resync + live command streaming) |

### `server.py` and `http_api.py` — the two front doors
Both are thin: parse a request, call `engine.dispatch_from_client()` / `engine.execute()`, encode whatever comes back, write it to the socket. `server.py` speaks RESP over a raw TCP socket (real Redis clients connect here). `http_api.py` is a hand-rolled HTTP/1.1 server (no framework — see `DESIGN_DECISIONS.md` for why) serving a small JSON REST API, the dashboard's static files, and a WebSocket endpoint for live updates. Both run as independent `asyncio` servers inside the *same* process, sharing the *same* `EmberEngine` instance — a write made over RESP is immediately visible to a GET against the REST API, because there's only ever one copy of the data.

### `__main__.py` — the process
Resolves configuration (`config.py`: defaults → config file → CLI flags), constructs the engine, replays whatever's on disk, starts both servers and the background tasks (active expiry, metrics sampling, periodic auto-snapshot), and shuts everything down cleanly on `SIGINT`/`SIGTERM`.

## Why one process, one engine, two front doors

A deliberate design choice worth calling out: the dashboard is not a second implementation that happens to look at the same data — `http_api.py`'s command-execution endpoint and its keyspace/heatmap endpoints call the *exact same* `engine.execute()` a RESP client's `SET`/`SCAN` calls go through. There is exactly one command engine in this project. This matters for correctness (no risk of the dashboard and the RESP server disagreeing about what a command does) and it's also just less code to maintain — see `CODE_FLOW.md`'s section on AOF replay for the same principle applied to startup.
