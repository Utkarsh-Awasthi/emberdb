# Getting started

## Requirements

Python 3.10 or later. No pip packages are required to run the server — the entire core (protocol, engine, persistence, replication, the HTTP/dashboard server) is pure standard library. `pytest` is the one optional dependency, needed only to run the test suite.

## Running the server

From the project root, with `src/` on your `PYTHONPATH` (or after `pip install -e .`):

```bash
python3 -m emberdb
```

This starts the RESP server on port 7379 and the dashboard/HTTP API on port 7380, using the current directory for `emberdb.snapshot.json` / `emberdb.aof.jsonl` / `emberdb.cdc.jsonl`. Open **http://localhost:7380/** for the dashboard.

Useful flags:

```bash
python3 -m emberdb \
  --dir ./data \                 # put all data files under ./data/
  --port 7379 --http-port 7380 \
  --appendonly yes \             # enable the AOF for crash-durability between snapshots
  --requirepass "a-strong-password" \
  --maxmemory 536870912 \        # 512 MB, in bytes; 0 = unlimited (default)
  --maxmemory-policy allkeys-lru
```

Run `python3 -m emberdb --help` for the full flag list. Flags override a `--config` file, which overrides the built-in defaults (`config.py`'s `DEFAULT_CONFIG`) — see that file for every recognized setting.

### Config file

`emberdb.conf`-style, one directive per line:

```
port 7379
http_port 7380
appendonly yes
maxmemory 536870912
maxmemory_policy allkeys-lru
requirepass a-strong-password
```

```bash
python3 -m emberdb --config emberdb.conf
```

## Connecting

Verified against `redis-cli` and three real client libraries — `redis-py` (Python), `node-redis` and `ioredis` (Node.js) — covering their connection handshakes, all core types, transactions, pub/sub, and error handling. See [`CLIENT_COMPATIBILITY.md`](CLIENT_COMPATIBILITY.md) for exactly what was tested and how.

```bash
redis-cli -p 7379 SET foo bar
redis-cli -p 7379 GET foo
```

Or the bundled standalone CLI (works against any RESP server, not just EmberDB):

```bash
python3 cli/ember_cli.py -p 7379
python3 cli/ember_cli.py -p 7379 -a your-password    # if requirepass is set
python3 cli/ember_cli.py -p 7379 GET foo             # one-shot mode
echo "PING" | python3 cli/ember_cli.py -p 7379        # stdin pipe mode
```

## Persistence

Two layers, same as Redis, working together:

- **Snapshot** (`--dir`'s `emberdb.snapshot.json`): a full point-in-time dump, written by `SAVE` (blocking), `BGSAVE` (runs off the event loop), or automatically every `save_interval_seconds` (default 300; set to `0` to disable). Loaded on startup if present.
- **AOF** (`--dir`'s `emberdb.aof.jsonl`, only if `--appendonly yes`): every write command, logged as it happens, replayed on top of the snapshot at startup for anything written since the last snapshot. `aof_fsync` controls durability vs. throughput: `always` (fsync every write), `everysec` (default — bounded ~1s of loss on a hard crash), or `no` (rely on the OS).

Startup always does snapshot-then-AOF, in that order — see `CODE_FLOW.md` §1 for exactly how.

## Authentication

No password is required by default (matching Redis's own default). To require one:

```bash
python3 -m emberdb --requirepass "a-strong-password"
```

or at runtime: `CONFIG SET requirepass a-strong-password` (note: unlike real Redis's `CONFIG SET requirepass`, EmberDB routes this through `ACL SETUSER default > <password>` internally — either `redis-cli -p 7379 ACL SETUSER default '>your-password'` or the `--requirepass` flag work). Once set, every command except `PING`/`AUTH`/`HELLO`/`QUIT`/`RESET` requires `AUTH <password>` first. For per-user restrictions (a service account limited to a few commands and a key pattern):

```bash
redis-cli -p 7379 ACL SETUSER readonly_svc on '>svc-password' +GET +MGET +EXISTS '~cache:*'
```

See `docs/DESIGN_DECISIONS.md` for exactly which subset of real Redis's ACL grammar this covers.

## Replication

Start a leader and a follower (two separate `--dir`s so they don't collide on files):

```bash
python3 -m emberdb --dir ./leader-data --port 7379 --http-port 7380
python3 -m emberdb --dir ./follower-data --port 7380 --http-port 7381
```

Then point the follower at the leader:

```bash
redis-cli -p 7380 REPLICAOF 127.0.0.1 7379
```

The follower performs a full resync (downloads the entire current dataset) and then applies every subsequent write as it streams in. `INFO` on either instance shows `role:master` / `role:replica` and `connected_replicas:N`. See `docs/DESIGN_DECISIONS.md` for what's simplified here (no partial resync, no chaining, `PUBLISH` isn't forwarded).

## The dashboard

**http://localhost:7380/** (or whatever `--http-port` you chose) — no separate install, served by the same process. Live ops/sec, memory usage against your configured `maxmemory`, hit ratio, a real per-key access heat map (genuine LRU/LFU data, not decoration — see `docs/ARCHITECTURE.md`), a live change-stream tail, a keyspace browser with pattern search, and a command console for ad-hoc queries. See `docs/HTTP_API.md` if you want to build your own tooling against the same REST/WebSocket API the dashboard uses.

## Running the tests

```bash
pip install pytest --break-system-packages   # or: use a virtualenv
python3 -m pytest tests/ -q
```

254 tests across protocol framing, every command family, transactions (including rollback), pub/sub, persistence, JSON documents, the query engine, vector search, CDC, ACLs, and real end-to-end wire-protocol tests against a live `asyncio` server and HTTP/WebSocket server.

## Where to go next

- [`COMMANDS.md`](COMMANDS.md) — every command, with syntax
- [`CODE_FLOW.md`](CODE_FLOW.md) — what actually happens, step by step, for a read, a write, a transaction, and startup
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — the module map
- [`COMPARISON.md`](COMPARISON.md) — an honest accounting against real Redis
- [`DESIGN_DECISIONS.md`](DESIGN_DECISIONS.md) — the reasoning behind every simplification
