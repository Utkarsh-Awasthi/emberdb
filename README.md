# EmberDB

EmberDB is an in-memory database server built from scratch in pure Python — no wrapped Redis, no bindings to a C library, no `pip install redis` underneath the hood. It speaks the real RESP2/RESP3 wire protocol, verified against `redis-cli` and three real client libraries (`redis-py`, `node-redis`, `ioredis`) — see [`docs/CLIENT_COMPATIBILITY.md`](docs/CLIENT_COMPATIBILITY.md) for exactly what was tested. On top of that Redis-equivalent core, EmberDB adds a handful of capabilities Redis needs separate modules or separate tools for: native JSON documents, a secondary-index query engine, vector similarity search, git-style branching of the whole dataset, a durable replayable change stream, TTL webhooks, and a built-in observability dashboard, all in the same process.

```
   ______           __             ____  ____
  / ____/___ ___  / /_  ___  _____/ __ \/ __ )
 / __/ / __ `__ \/ __ \/ _ \/ ___/ / / / __  |
/ /___/ / / / / / /_/ /  __/ /  / /_/ / /_/ /
/_____/_/ /_/ /_/_.___/\___/_/  /_____/_____/
```

## Why this exists

This project was built as an end-to-end exercise: implement something that matches what Redis is as a *product* — not a thin wrapper around it — and then go further, adding capabilities on top. Every module was written and independently validated before the next one was built on top of it; see [`docs/DESIGN_DECISIONS.md`](docs/DESIGN_DECISIONS.md) for the reasoning behind every non-obvious choice, and [`docs/COMPARISON.md`](docs/COMPARISON.md) for an honest accounting of where EmberDB is genuinely different from Redis, where it's a deliberately simplified version, and where it just isn't as fast (it's Python — see below).

**One honesty note up front, because it matters:** EmberDB does not claim to out-perform Redis. Redis is implemented in C with over a decade of micro-optimization; EmberDB is pure Python. The "better" in "matches Redis, but better" refers strictly to *capabilities* — the things listed below that Redis needs a separate module or a separate process for, EmberDB has built in. If you need C-level throughput, use Redis. If you want one process that gives you a cache, a document store, a basic search index, a vector store, and a change-data-capture stream, with the source code short enough to actually read end to end, that's what this is for.

## Quick start

No dependencies are required to run the server — pure Python 3.10+ stdlib.

```bash
cd emberdb
python3 -m src.emberdb --dir ./data --port 7379 --http-port 7380
```

Or, once installed as a package (`pip install -e .` from the project root, or just make sure `src/` is on your `PYTHONPATH`):

```bash
python3 -m emberdb --dir ./data
```

Then, from anywhere:

```bash
redis-cli -p 7379 SET greeting "hello from EmberDB"
redis-cli -p 7379 GET greeting
```

Open **http://localhost:7380/** for the live dashboard — ops/sec, memory, a real per-key access heat map, a live change-stream tail, a keyspace browser, and a command console, all served by the same process, no separate app to install.

Running tests (pytest is the one optional dev dependency, not required to run the server itself):

```bash
pip install pytest --break-system-packages   # or use a virtualenv
python3 -m pytest tests/ -q
```

## A quick tour

Redis-equivalent core — every command below works exactly as it would against real Redis:

```
redis-cli -p 7379 RPUSH mylist a b c
redis-cli -p 7379 HSET user:1 name Alice email alice@example.com
redis-cli -p 7379 ZADD leaderboard 100 alice 200 bob
redis-cli -p 7379 SET session:1 "..." EX 3600
redis-cli -p 7379 MULTI
redis-cli -p 7379 SUBSCRIBE news
```

The differentiators — none of these need a separate module or a separate service:

```bash
# Native JSON documents (no RedisJSON module needed)
redis-cli -p 7379 JSON.SET product:1 '$' '{"name":"Widget","price":9.99,"tags":["new"]}'
redis-cli -p 7379 JSON.GET product:1 '$.price'

# Secondary indexing + a small query engine (no RediSearch module needed)
redis-cli -p 7379 CREATE INDEX idx_email ON "user:*" FIELD email
redis-cli -p 7379 FIND idx_email WHERE email = alice@example.com

# Vector similarity search (no RedisVL / vector module needed)
redis-cli -p 7379 VECTOR.SET images photo:1 VALUES 0.12 0.98 0.44
redis-cli -p 7379 VECTOR.SEARCH images VALUES 0.10 0.95 0.40 K 5

# Git-style branching of the entire dataset — fork, experiment, merge or discard
redis-cli -p 7379 BRANCH.CREATE experiment
redis-cli -p 7379 BRANCH.SWITCH experiment
redis-cli -p 7379 SET config:rate 999        # only exists on this branch
redis-cli -p 7379 BRANCH.SWITCH main
redis-cli -p 7379 BRANCH.MERGE experiment    # three-way merge, conflicts reported

# A durable, offset-addressable change stream (beyond fire-and-forget keyspace notifications)
redis-cli -p 7379 CDC.SINCE 0

# TTL expiry webhooks — call a URL when a key expires, no polling worker needed
redis-cli -p 7379 HOOK.ONEXPIRE cleanup ON "session:*" CALL https://api.internal/cleanup

# Transactions that actually roll back on a runtime failure (Redis's MULTI/EXEC explicitly does not)
redis-cli -p 7379 MULTI
redis-cli -p 7379 SET a 1
redis-cli -p 7379 EXEC
```

See [`docs/COMMANDS.md`](docs/COMMANDS.md) for the full command reference and [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) for a longer walkthrough including persistence, replication, and the HTTP API.

## Project layout

```
emberdb/
├── src/emberdb/
│   ├── protocol.py         RESP2/RESP3 codec — the only module that touches wire bytes
│   ├── database.py         the in-memory keyspace (one Database per logical db)
│   ├── valuetypes.py       ZSet / JSONValue / VectorValue value types
│   ├── skiplist.py         from-scratch skip list backing ZSet (mirrors real Redis)
│   ├── memsize.py          approximate per-key memory accounting
│   ├── expiry.py           active (background sweep) TTL expiration
│   ├── eviction.py         maxmemory policies (LRU/LFU/random/TTL, all 8 Redis policies)
│   ├── persistence.py      JSON snapshot (RDB-equivalent) + AOF, with transaction batching
│   ├── pubsub.py           channel + pattern pub/sub, transport-agnostic
│   ├── cdc.py              change data capture: durable, offset-addressable event log
│   ├── jsonpath.py         minimal JSONPath subset for JSON.* commands
│   ├── indexing.py         secondary indexes + the CREATE INDEX / FIND query engine
│   ├── vectorsearch.py     brute-force vector similarity search
│   ├── acl.py               AUTH / per-user command + key-pattern permissions
│   ├── metrics.py          INFO stats, ops/sec history, slowlog
│   ├── hooks.py            TTL expiry webhooks
│   ├── branching.py        git-style dataset branching (fork/switch/merge/diff)
│   ├── replication.py      simplified leader-follower replication
│   ├── config.py           configuration defaults + redis.conf-style parsing
│   ├── commands/           one file per Redis type, mirroring Redis's own t_string.c-style layout
│   ├── engine.py           EmberEngine — ties every subsystem into one command dispatch path
│   ├── server.py           the asyncio RESP TCP server
│   ├── http_api.py         hand-rolled HTTP/1.1 + WebSocket server (REST API + dashboard)
│   ├── dashboard/          the dashboard's HTML/CSS/JS
│   └── __main__.py         `python -m emberdb` entry point
├── cli/ember_cli.py         standalone interactive RESP client (like redis-cli)
├── tools/                   benchmark.py, shard_proxy.py (bonus tools)
├── tests/                   pytest suite (254 tests at last count)
└── docs/                    architecture, code flow, commands, comparison, design decisions
```

## Documentation

- [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) — installation, configuration, a longer walkthrough
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — how the modules fit together
- [`docs/CODE_FLOW.md`](docs/CODE_FLOW.md) — tracing a request end to end: what actually happens, step by step, when a command runs
- [`docs/COMMANDS.md`](docs/COMMANDS.md) — full command reference
- [`docs/CLIENT_COMPATIBILITY.md`](docs/CLIENT_COMPATIBILITY.md) — exactly which client libraries were tested, how, and what was found
- [`docs/HTTP_API.md`](docs/HTTP_API.md) — REST API and WebSocket protocol for the dashboard
- [`docs/COMPARISON.md`](docs/COMPARISON.md) — an honest, specific comparison against real Redis
- [`docs/DESIGN_DECISIONS.md`](docs/DESIGN_DECISIONS.md) — the reasoning behind every non-obvious choice and every deliberate simplification

## License

This is a reference/educational implementation produced in a single engineering session. No warranty, no production-hardening claims beyond what's documented. Not affiliated with, endorsed by, or a product of Redis Ltd.
