# Design decisions

Every non-obvious choice in this codebase has a comment at the point it was made. This document pulls them all into one place. If you only read one doc to understand *why* EmberDB is built the way it is rather than some other way, read this one.

## Overall approach

**Pure Python stdlib for the core, zero required dependencies.** The RESP server, the command engine, persistence, replication, and the HTTP/WebSocket dashboard server are all hand-rolled — no `redis-py` underneath (that would defeat the point), no `aiohttp`/`websockets` (see below), no ORM. `pytest` is the one *optional* dev dependency, needed only to run the test suite, not to run the server. This was a deliberate constraint from the start: it keeps the whole system inspectable and means "clone and run" never depends on what happens to `pip install` successfully that day.

**One command engine, every front door.** `EmberEngine.execute()` is the single place any command actually runs — whether it arrived from a live RESP connection, is being replayed from the AOF at startup, arrived over a replication link, or was issued through the HTTP API's command console. See `CODE_FLOW.md` for the full trace. The alternative — a second, parallel "replay interpreter" or a second implementation of command semantics for the HTTP API — would be a second place for bugs to diverge from the first, for no real benefit.

## Persistence & durability

**JSON snapshots, not a compact binary format.** Real Redis's RDB format is compact binary. EmberDB's snapshot (`persistence.py`) is JSON. Trade-off, made deliberately: JSON has no code-execution risk on load (unlike `pickle`, which was the tempting shortcut) and is human-inspectable/diffable, at the cost of file size and load speed on very large datasets. For a reference implementation prioritizing safety and auditability over raw compactness, this is the right trade.

**AOF commands are rewritten to absolute time before they're logged.** `EXPIRE key 60` written to the AOF verbatim would, on replay, grant a *fresh* 60 seconds from whenever replay happens — silently extending every TTL on every restart. Every relative-time command (`EXPIRE`/`PEXPIRE`/`EXPIREAT`/`SET ... EX`/`SETEX`/`PSETEX`/`GETEX`) is rewritten to an absolute-millisecond `PEXPIREAT` form (via `CommandContext.propagate_override`) before it's written to the AOF or sent to a replica. This mirrors what real Redis does internally, for the same reason.

**Transactions are atomic in the AOF too, not just in memory.** `AOFWriter.begin_batch()`/`commit_batch()`/`discard_batch()` buffer a `MULTI`/`EXEC` block's log lines in memory and only write them if the whole transaction commits — see the rollback trace in `CODE_FLOW.md` §4.

**Approximate memory accounting, not `jemalloc`-precise.** `memsize.py`'s `estimate_size()` is a heuristic, cached per key and summed incrementally so `eviction.py` can check "are we over `maxmemory`?" in O(1). It will not match Redis's own `used_memory` figure exactly. Documented as approximate rather than presented as precise.

## Data structures

**A real, from-scratch skip list backs sorted sets** (`skiplist.py`), not a simpler `bisect`-on-a-list approach. This was a deliberate choice to mirror what Redis's own `zset` actually does internally (skiplist + dict) rather than take a shortcut that happens to produce the same external behavior — chosen because the educational/authenticity value justified the extra implementation risk, and it was validated exhaustively (2000 random inserts cross-checked against Python's own `sorted()`, then 1000 random deletes, for every operation the type supports) before anything was built on top of it.

**Lists are backed by `collections.deque`**, giving O(1) push/pop at both ends (the dominant real-world access pattern for lists used as queues/stacks) at the cost of O(n) access into the middle (`LINDEX`/`LRANGE` over an arbitrary slice). Real Redis's own quicklist has the same asymptotic trade-off for middle access on large lists, so this isn't a regression versus upstream — see `commands/lists.py`.

## Eviction & memory pressure

**`maxmemory` is enforced *before* a write, not clamped after.** `EvictionManager.enforce()` checks current usage, evicts if needed, then lets the write proceed — meaning a single very large write can still land the system slightly over budget; the *next* write is what gets blocked (under `noeviction`) or triggers further eviction. This matches real Redis's own enforcement model, and it's why `tests/test_eviction.py`'s OOM test needs a first successful write before the second one can be rejected — a fresh, empty database never exceeds any positive budget, however small.

**LFU is a plain, non-decaying access counter**, not Redis's actual algorithm (a probabilistic, time-decaying Morris counter that ages "hot but no longer accessed" keys back down over time). Documented directly in `eviction.py`'s docstring as a simplification, not presented as the real thing.

## Query & search capabilities

**The secondary-index query engine is intentionally narrow**: one field per index, equality and numeric-range operators, conjunctive (`AND`-only) filters, no `OR`, no joins, no query planner. It solves the concrete problem ("find every key where some field equals/exceeds some value" without a full keyspace scan) without pretending to be a general query engine. See `indexing.py`'s docstring.

**Vector search is brute-force**, O(n·d) per query — fine at the scale a reference implementation is actually used at (thousands of vectors), explicitly not sub-linear like a production ANN/HNSW index would be. Documented in `vectorsearch.py` as a roadmap item, not silently passed off as production-scale.

**JSONPath support is a minimal subset**: `$`, `.key`, `[idx]` (including negative indices), chained. No wildcards, no recursive descent, no filter expressions, no slices. `jsonpath.py`'s own docstring states this explicitly.

## Security

**Passwords are salted-PBKDF2 hashes, not plaintext** — but also not bcrypt/scrypt/argon2. `acl.py` uses `hashlib.pbkdf2_hmac` + `hmac.compare_digest` (constant-time comparison) to stay dependency-free; a real production system would reach for a purpose-built slow KDF instead of a fast general-purpose hash. Storing the password at all, even hashed with a faster algorithm, was rejected outright — this project didn't want to model that habit even for a reference implementation.

**ACLs cover a small, practical subset of Redis's real rule grammar**: per-user command-name globs and one key-pattern glob per user. No selectors, no `+@category` groupings, no per-command key patterns. The key-pattern check treats `argv[1]` as a candidate key uniformly across every command — which is right for the vast majority of commands but means a multi-word admin command like `CONFIG GET` has its *subcommand* name (`GET`) checked against the key pattern too. In practice this only matters for a very restrictively-scoped user also being granted access to admin commands, which is an unusual combination; documented rather than engineered around, given how narrow the edge case is.

**A small, deliberate set of commands bypass authentication**: `PING`, `AUTH`, `HELLO`, `QUIT`, `RESET`. The first three are required for the auth bootstrapping flow itself or are a harmless liveness check; the last two are harmless connection-lifecycle operations. Every other command requires authentication once a password is configured — verified directly in `tests/test_acl.py`.

## Networking & protocol

**No TLS anywhere — RESP or HTTP.** Both `server.py` and `http_api.py` listen on plain TCP only. Real Redis's own TLS support is itself a compiled-in build option wrapping OpenSSL; doing the same properly in pure Python would mean adopting `ssl`-module machinery throughout both servers for a reference implementation that's meant to run locally or behind a trusted network boundary. Documented as absent, not silently assumed away — run EmberDB behind a TLS-terminating proxy if you need encryption in transit.

**The HTTP/WebSocket server is hand-rolled**, not built on `aiohttp` or the `websockets` package. If EmberDB's "built-in dashboard, one process, nothing extra to install" claim required a separate web framework dependency, the claim would be hollow. `http_api.py` implements HTTP/1.1 parsing (GET/POST, `Content-Length` bodies, static file serving) and RFC 6455 WebSocket framing (handshake, masked/unmasked frames, ping/pong/close) directly. Deliberately out of scope: TLS, chunked transfer encoding, HTTP/2, cookie/session auth (a bearer-token check against the same `requirepass` is used instead), and persistent keep-alive connections (every HTTP request closes its connection after responding — simpler and correct, at some efficiency cost at very high request rates, which is not the regime a dashboard operates in).

**`SCAN`'s cursor is a plain position, not Redis's hash-table-bucket-reverse-binary-iteration cursor.** Real Redis's cursor encodes a position in its internal hash table's bucket layout specifically so iteration stays stable under concurrent table resizing. EmberDB doesn't have that structure — its cursor is simply "how many of the (sorted, stably-ordered) keys have been returned so far." This gives the same client-facing contract `SCAN` promises (every key present for the whole scan is returned at least once) without needing to model Redis's internal hash table.

**`UNLINK` behaves identically to `DEL`.** Real Redis's `UNLINK` frees memory on a background thread specifically to avoid blocking on huge aggregate types; EmberDB has no such background-reclaim machinery, so the two commands are the same here. Documented in `commands/generic.py` rather than silently pretended away.

## Expiration & webhooks

**TTL webhooks and "expired"-type CDC events are driven by active expiration, not passive.** A key discovered as expired because a client happened to read it first (`Database.get()`'s inline lazy purge) is correctly treated as gone for that read, but does not independently trigger a webhook or CDC event — only the background active-expiry sweep (`expiry.py`, default 100ms interval) does that fan-out. In practice the sweep interval means it almost always wins the race against passive discovery, but this is a real, documented gap rather than a guarantee.

**Webhooks are fire-and-forget, best-effort** — no retry queue, no at-least-once guarantee. If a target endpoint is down, the call is logged as failed and dropped. For a workflow that needs a durable, retryable guarantee, `CDC.SINCE` (poll-based, backed by a durable file) is the right building block instead; `HOOK.*` is for side effects like cache invalidation where an occasional miss is acceptable. See `hooks.py`'s docstring.

## Replication

**Full resync only — no partial resync (`PSYNC`).** `REPLICAOF` always transfers the entire current dataset, then streams live commands. A follower that briefly disconnects re-downloads everything rather than requesting only what it missed. Simpler protocol, simpler code, at the cost of reconnect efficiency — the right trade for a reference implementation. See `replication.py`.

**No chained replication.** A replica applies incoming commands with `is_replay=True`, which does not re-propagate them to any sub-replicas of its own.

**`PUBLISH` is not forwarded to replicas.** It's deliberately not flagged as a write (it doesn't touch the keyspace, so it shouldn't hit the AOF or count against `maxmemory`), and replication only streams write-flagged commands. A subscriber connected to a follower will not see messages published on the leader. See `commands/pubsub_cmds.py`.

## Transactions

**Real rollback, going beyond what Redis itself documents.** Redis's own docs are explicit that `MULTI`/`EXEC` does not roll back when a queued command fails at runtime (only a command that fails to even *queue* — unknown command, wrong arity — aborts the whole transaction, before anything runs). EmberDB snapshots every key a queued command might touch immediately before running it and restores every snapshot, in reverse order, if any command in the batch raises at runtime — covering the database, the AOF, the CDC stream, and replication traffic uniformly. See `commands/transactions.py`'s module docstring and the full trace in `CODE_FLOW.md` §4.

## Branching

**Branches are ephemeral by design.** Writes made on a non-`main` branch never reach the AOF and are never replicated, and snapshots always capture `main` even when another branch is checked out. The alternative — persisting whatever branch happened to be live — would replay experimental writes into `main` on the next restart and push them to replicas that have no concept of a branch. Losing scratch data on restart is the right trade against silently corrupting the real dataset.

**Merge is three-way, not last-writer-wins.** Each branch keeps a fork-point image of the dataset it was forked from, so `BRANCH.MERGE` can distinguish "changed on the source" from "unchanged" and detect when both sides touched the same key. Naive overwrite would have been far less code, but it would silently clobber concurrent target-side work, which is exactly the failure mode anyone reaching for branching is trying to avoid. The default `fail` strategy aborts the whole merge on conflict rather than applying a partial one.

**Forking deep-copies.** No copy-on-write or structural sharing: `BRANCH.CREATE` is a full `deepcopy`, and the fork-point image is a second copy. That makes branch cost proportional to dataset size — fine for the intended use (scratch branches in development, agent experimentation, CI isolation), wrong for branching a large production dataset. Copy-on-write would fix this and is the obvious next step; it is not implemented.

**Switching swaps the dataset in place.** `BranchManager` mutates the contents of the single list object the engine holds (`self._live[:] = target`) rather than rebinding it, because `IndexManager`, `EvictionManager`, and `ActiveExpirer` all captured a reference to that same list at construction. Secondary indexes cache per-key values, so they're rebuilt (`IndexManager.rebuild_all`) after any switch or merge.

**Merges into `main` are persisted as value restores, not replayed branch commands.** A merge writes values directly rather than going through `execute()`, so it emits one `EMBER.RESTORE` (or `DEL`) per changed key to the AOF and replicas. The `BRANCH.*` commands themselves are deliberately not flagged as writes: replaying `BRANCH.SWITCH` from the AOF could leave startup replay parked on a branch and misroute every later `main` write.

## AOF durability

**Every AOF write is handed to the OS immediately; `aof_fsync` only controls fsync timing.** `always` fsyncs per write, `everysec` fsyncs from a 1-second background task, `no` leaves it to the OS. A process crash (`kill -9`) therefore loses nothing under any policy; only an OS crash or power loss can cost up to ~1s under `everysec`. This matches Redis's own semantics. An earlier version buffered writes in-process and never ran the 1-second task, so a crash could lose an unbounded amount of recent data — caught by a live `kill -9` test, now covered by tests that read the AOF without flushing first.
