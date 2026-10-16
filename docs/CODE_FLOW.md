# Code flow

This document traces exactly what happens, function by function, for the scenarios that matter most for understanding how EmberDB actually works. Read [`ARCHITECTURE.md`](ARCHITECTURE.md) first if you want the module map before diving into these traces.

## 1. Startup

`python -m emberdb` runs `__main__.py:main()`:

1. `_parse_args()` / `_resolve_config()` — CLI flags layered on top of an optional `--config` file, layered on top of `config.py`'s `DEFAULT_CONFIG`. The result is one complete config dict.
2. `EmberEngine(cfg)` — constructs every subsystem (`acl.py`'s `ACLManager`, `eviction.py`'s `EvictionManager`, and so on), each pointed at its slice of the config. Nothing touches disk yet.
3. `engine.load_from_disk()`:
   - If a snapshot file exists, `persistence.load_snapshot()` deserializes it straight into `engine.databases` — this is a *direct* reconstruction of the value objects, not a replay of commands.
   - If AOF is enabled and an AOF file exists, `persistence.replay_aof()` reads it line by line and calls back into `engine._replay_dispatch(db_index, argv)` for each line — which calls `engine.execute(..., is_replay=True)`. **This is the same `execute()` a live client's commands run through.** There is no separate "replay interpreter" that reimplements what `SADD` does; replay *is* command execution, just with `is_replay=True` telling `execute()` to skip re-logging to the AOF (it's already there) and skip re-emitting CDC events / replicating (they're historical, not new).
4. `serve_resp()` and `serve_http()` each start an `asyncio` TCP listener.
5. `engine.start_background_tasks()` starts the active-expiry loop (`expiry.py`) and a 1-second metrics-sampling tick.
6. A periodic auto-save task and a `SIGINT`/`SIGTERM` handler are registered, then the process blocks until a shutdown signal arrives.

## 2. A read command: `GET foo`

1. `server.py`'s per-connection loop calls `read_command(reader)` (`protocol.py`), getting back `[b"GET", b"foo"]`, and decodes each element to `str`.
2. `engine.dispatch_from_client(conn, ["GET", "foo"])` runs. Since the connection isn't mid-`MULTI` and isn't in subscriber mode, this just calls `engine.execute(conn, conn.selected_db, argv)`.
3. Inside `execute()`:
   - Look up `"GET"` in `COMMAND_TABLE` (built by `commands/__init__.py` importing every `commands/*.py` submodule, each registering its handlers via `@register(...)`).
   - `check_arity()` — `GET` needs exactly 2 argv elements.
   - Since `GET` isn't flagged `no_auth_required` and isn't a replay, `acl.check()` runs (a no-op if no password is configured).
   - Build a `CommandContext` — the one object every handler receives, carrying `engine`, `db_index`, and the connection.
   - `GET` isn't `write=True`, so there's no eviction check.
   - Call the handler: `commands/strings.py:cmd_get(ctx, argv)`, which calls `ctx.db.get("foo")` (this is where passive/lazy TTL expiry happens, inside `Database.get()`) and returns the string.
   - `metrics.record_command()` logs the timing.
   - Since `GET` isn't a write, there's nothing further to do — `execute()` returns an `ExecResult` wrapping the reply.
4. Back in `server.py`: `encode_reply(reply, conn.protocol_version)` turns `"bar"` into `$3\r\nbar\r\n` and the bytes are written to the socket.

## 3. A write command: `SET foo bar EX 60`

Steps 1–2 are identical to the read path. Inside `execute()`, the interesting part starts once the handler returns:

1. `commands/strings.py:cmd_set()` runs. It parses the `EX 60` option, computes an absolute expiry timestamp, calls `ctx.db.set_value("foo", "bar")` and `ctx.db.set_expire_at(...)`, then — critically — calls `ctx.mark_write("foo")`, which appends `"foo"` to `ctx.written_keys`. It also sets `ctx.propagate_override = ["SET", "foo", "bar", "PXAT", "<absolute-ms>"]` instead of leaving the original relative-time `EX 60` form to propagate. **Why:** if the original `SET ... EX 60` were written to the AOF or sent to a replica, replaying it later (even a second later) would grant a *fresh* 60-second TTL from that later moment — silently extending every key's lifetime on every restart or replica resync. Rewriting to an absolute timestamp before propagation fixes the expiry moment permanently, matching what real Redis does internally for the exact same reason.
2. Back in `execute()`, since `SET` is `write=True` and this isn't a replay: `eviction.enforce()` was already called *before* the handler ran (a pre-write check, not a post-write one — see `DESIGN_DECISIONS.md` for why a single large write can still land slightly over `maxmemory`). Now, after the handler returns: `propagate_argv = ctx.propagate_override or argv` (the rewritten `PXAT` form, in this case), and `apply_write_side_effects()` runs:
   - `aof.log(db_index, propagate_argv)` — one line appended to the append-only file.
   - For each key in `ctx.written_keys` (`["foo"]`): `indexes.on_write(db_index, "foo")` — if any `CREATE INDEX` registration's glob pattern matches `"foo"`, the index's inverted-index entries for that key are refreshed.
   - `cdc.append(db_index, "write", "SET", "foo", ["foo"])` — one event appended to the change-data-capture log (ring buffer + durable file), visible immediately to anyone polling `CDC.SINCE` or tailing `CDC.TAIL`.
   - `replication.propagate(propagate_argv, db_index)` — if any followers are attached, the same rewritten command is streamed to each of them (with a `SELECT` prefix first, if their last-known db differs).
3. `server.py` encodes and sends `+OK\r\n`.

**The pattern to notice:** a write command's handler does exactly one thing — call `ctx.mark_write(key)` — and every downstream system (AOF, CDC, indexing, replication) picks that up automatically from the *outside*, in `apply_write_side_effects()`. A handler in `commands/hashes.py` has no idea CDC or indexing exist; it just mutates the database and marks what it touched.

## 4. A transaction: `MULTI` / `SET a 1` / `LPUSH a x` (fails) / `EXEC`

This is where EmberDB's transaction guarantee is stronger than Redis's own documented behavior (Redis explicitly does **not** roll back a transaction when a queued command fails at runtime — only a command that fails to even *queue*, like a syntax error, aborts the whole thing). See `commands/transactions.py`'s module docstring for the full reasoning; here's the mechanical trace:

1. `MULTI` sets `conn.in_multi = True` and clears `conn.multi_queue`.
2. Every subsequent command *except* `MULTI`/`EXEC`/`DISCARD`/`WATCH`/`UNWATCH`/`RESET` is intercepted in `engine.dispatch_from_client()` *before* it ever reaches `execute()`: arity and command-existence are validated immediately (a genuinely malformed command here poisons the transaction — `conn.multi_error = True`), and the raw `argv` is appended to `conn.multi_queue`. The client gets back `+QUEUED\r\n` for each one.
3. `EXEC` (`commands/transactions.py:cmd_exec`) runs:
   - Re-checks every `WATCH`ed key's version (`database.py` bumps a per-key version on every write); if any changed, abort with a nil reply — this is the optimistic-concurrency-control half of the story, independent from the rollback half below.
   - `engine.begin_transaction_buffering()` — tells the `AOFWriter` to buffer subsequent `log()` calls in memory instead of writing them to disk immediately (`persistence.py`'s `begin_batch()`).
   - For each queued command: `engine.snapshot_candidates(db_index, sub_argv)` takes a conservative pre-image — every distinct token in the command's arguments that currently exists as a key gets `deepcopy`'d, along with whether it existed at all. Then `engine.execute(conn, db_index, sub_argv, defer_side_effects=True)` actually runs it. `defer_side_effects=True` means the write still happens to the database immediately, but the AOF/CDC/replication fan-out from step 3 above is *not* applied yet — it's returned to the caller instead.
   - `SET a 1` succeeds; its snapshot and pending side effects are appended to an `applied` list.
   - `LPUSH a x` raises `RespError` (WRONGTYPE — `a` now holds the string `"1"`). The failure handler: restore *this* command's own snapshot (in case it partially wrote before raising), then walk `applied` in reverse and restore every earlier command's snapshot too (`engine.restore_snapshot()`, which also re-triggers `indexes.on_write()`/`on_delete()` for whatever it restores, so indexes stay consistent with the rolled-back state). `engine.abort_transaction_buffering()` discards every buffered AOF line for the whole transaction — not just the failing command's. The error propagates out as `EXECABORT`.
4. The client sees an `EXECABORT` error. `a` is back to whatever it was before the transaction started (or gone, if it didn't exist before). Nothing from this transaction — not in the database, not in the AOF, not in CDC, not sent to any replica — is left applied.

If every queued command had succeeded instead, `EXEC` would call `engine.commit_transaction_buffering()` (flushing the buffered AOF lines all at once) and then iterate the `applied` list calling `engine.apply_write_side_effects()` for each — so CDC events and replication traffic for a committed transaction appear in the same order the commands ran, right after the whole thing is known to have succeeded.

## 5. Active expiration

Independent of any client request, `expiry.py`'s background task wakes up every 100ms (configurable) and calls `ActiveExpirer.run_once()`, which calls `Database.sweep_expired(limit=200)` on each database — a *bounded* sample, not a full scan, so one slow tick never blocks the event loop for long. For each key found expired, `EmberEngine._on_expired(db_index, key)` runs: it bumps `metrics.expired_keys`, tells `indexing.py` to drop the key from any index, appends an `"expired"`-type CDC event, fires any matching `HOOK.ONEXPIRE` webhook (on a background thread — see `hooks.py`), logs a `DEL` to the AOF, and propagates that `DEL` to replicas. This is the same fan-out shape as a normal write's `apply_write_side_effects()`, just triggered by the clock instead of by a command handler.

*(Passive/lazy expiry — a key discovered as expired because a client happened to read it — is simpler: `Database.get()` just purges it inline. It does **not** go through this fan-out, which means a key that's always caught lazily rather than by the active sweep won't fire its TTL webhook. In practice, the default 100ms sweep interval means the active sweep almost always wins the race; this is a documented, deliberate simplification — see `DESIGN_DECISIONS.md`.)*

## 6. The same command, via the HTTP API instead of RESP

`POST /api/v1/command` with `{"db": 0, "argv": ["SET", "foo", "bar"]}` — `http_api.py:_handle_api()` builds a throwaway `InternalConnState` (the same stand-in connection type AOF replay uses) and calls `engine.execute()` directly. It's the identical dispatch path traced in section 3 above, just invoked from a different transport. This is why a write made through `redis-cli` shows up instantly in the dashboard's keyspace browser, and a write made through the dashboard's command console is immediately visible to a RESP client — there's one engine underneath both doors.
