# Client compatibility

What "speaks the real RESP2/RESP3 wire protocol" actually means, backed by what was tested — not a blanket claim.

## What was tested

Three real client libraries, against a live EmberDB server, not against EmberDB's own test code:

| Client | Language | Version tested |
|---|---|---|
| `redis-py` | Python | 8.1.0 |
| `node-redis` (`redis` on npm) | Node.js | latest |
| `ioredis` | Node.js | latest |

Plus `redis-cli` itself and raw sockets, which were already covered by the project's own test suite.

For each library: connect, `PING`, `SET`/`GET`, `INCR`, hashes, lists, sets, sorted sets (including `WITHSCORES`), `EXPIRE`/`TTL`, pipelining, `MULTI`/`EXEC`, pub/sub (`node-redis`), and confirming a `WRONGTYPE` error surfaces as the client's real typed exception rather than a hang or a garbled reply.

The connection handshake itself was captured at the byte level with a logging TCP proxy sitting between each client and the server, so what's verified is the exact bytes the client sends on connect — not just "the client's high-level call returned successfully," which can hide a client silently swallowing a failed handshake step.

## The one real gap this found

`redis-py` 8.x sends `CLIENT MAINT_NOTIFICATIONS ON moving-endpoint-type internal-ip` automatically on every connection — an opt-in for Redis Enterprise/Cloud maintenance-failover notifications, unrelated to normal command traffic. EmberDB rejected it as an unknown subcommand. `redis-py` tolerated the rejection and continued normally, so this didn't actually break anything observed in testing, but it logged a spurious error on every single connection and there's no guarantee every client (or a future `redis-py` version) degrades that gracefully.

Fixed: `CLIENT MAINT_NOTIFICATIONS` is now accepted as a no-op, consistent with the other optional `CLIENT` subcommands (`NO-EVICT`, `NO-TOUCH`, `PAUSE`, `UNPAUSE`) that a standalone server has nothing real to do with. Covered by a regression test (`tests/test_client_commands.py`) — confirmed to fail without the fix, pass with it.

Neither `node-redis` nor `ioredis` sent anything EmberDB didn't already handle.

## What this does not cover

- **Only these three libraries, at the versions above.** A different client, or a different version of these same clients, may send a different handshake and hit something untested. The general shape of the risk (an optional setup command EmberDB doesn't recognize) is now handled the same defensive way `CLIENT`'s other optional subcommands are, but that's a pattern, not a guarantee against every possibility.
- **No cluster-aware clients.** EmberDB isn't a cluster, so client-side cluster support (e.g. `redis-py`'s `RedisCluster`, ioredis's cluster mode) was not tested and has no reason to work.
- **No TLS.** Clients were tested over plain TCP; EmberDB doesn't support TLS at all (see `docs/DESIGN_DECISIONS.md`).
- **No RESP2-only client explicitly forced.** All three libraries negotiated RESP3 via `HELLO 3`. RESP2 itself is exercised by the project's own test suite and by `redis-cli`, but not by a third-party client explicitly pinned to RESP2.
- **Commands the libraries don't happen to call.** This exercised common usage patterns, not the full command surface — see `docs/COMMANDS.md` for what exists and `docs/COMPARISON.md` for what's deliberately not implemented (Lua scripting, Streams, blocking list ops, and others a client library might otherwise expect).

## Reproducing this

```bash
pip install redis          # redis-py
npm install redis ioredis  # node-redis, ioredis

python3 -m emberdb --port 7379 &

python3 -c "
import redis
r = redis.Redis(port=7379, decode_responses=True)
print(r.ping(), r.set('k','v'), r.get('k'))
"

node -e "
const { createClient } = require('redis');
(async () => {
  const c = createClient({ socket: { port: 7379 } });
  await c.connect();
  console.log(await c.ping(), await c.set('k','v'), await c.get('k'));
  await c.disconnect();
})();
"
```
