# EmberDB vs. Redis — an honest comparison

This document exists so nobody has to take "matches Redis, but better" on faith. It says exactly where that's true, exactly where EmberDB is a simplified version of something Redis does, and exactly where Redis is just better.

## Performance: Redis wins, and that's expected

Redis is a mature C codebase, optimized over more than a decade, running on a highly-tuned single-threaded event loop with careful memory layout (its own allocator integration, compact encodings for small collections, and so on). EmberDB is pure Python. For raw throughput and latency, Redis is faster — likely by a large margin under real load. **This project does not claim otherwise.** Nothing in this codebase should be read as a throughput claim. "Better" refers strictly to the capability differences below.

If your requirement is C-level throughput at scale, use Redis. If your requirement is one process that gives you a cache, a document store, basic secondary indexing, vector search, and a change-data-capture stream — without standing up RedisJSON, RediSearch, a vector module, and a separate CDC pipeline — and you'd like to be able to read the entire implementation in an afternoon, that's what EmberDB is for.

## What EmberDB has that Redis needs a separate module (or a separate service) for

| Capability | In Redis, you'd need... | In EmberDB |
|---|---|---|
| Native JSON documents | the RedisJSON module | `JSON.*` commands, built in |
| Secondary indexing + field queries | the RediSearch module | `CREATE INDEX` / `FIND`, built in |
| Vector similarity search | RedisVL or RediSearch's vector support | `VECTOR.*` commands, built in |
| A durable, replayable, offset-addressable change stream | Redis Streams (a different data model) or an external CDC pipeline (e.g. Debezium) | `CDC.SINCE` / `CDC.TAIL`, built in, same process |
| TTL expiry triggering an HTTP call | your own worker subscribed to keyspace notifications | `HOOK.ONEXPIRE`, built in |
| A web dashboard | a separate app (RedisInsight) | served by the same process at `/`, no install |
| Transactions that roll back on a runtime failure | not available — Redis's own docs are explicit that `MULTI`/`EXEC` does not roll back | real all-or-nothing rollback — see `CODE_FLOW.md` §4 |
| Forking the whole dataset to experiment against | stand up a second instance and copy an RDB across | `BRANCH.CREATE`/`SWITCH`/`MERGE`, in-process and instant |

Each of these is real, working code with its own test coverage — not a stub. See `docs/COMMANDS.md` for the full syntax of each.

## What Redis has that EmberDB simplifies or omits

Being equally specific in the other direction:

| Redis capability | EmberDB's status |
|---|---|
| Redis Cluster (sharding, cross-node resharding) | not implemented. `tools/shard_proxy.py` provides consistent-hash routing across independent EmberDB instances as a bonus tool, which is a much thinner idea than real cluster mode (no resharding, no cross-slot transactions, no cluster-bus gossip protocol) |
| Partial resync (`PSYNC`) | not implemented — every `REPLICAOF` performs a full resync. See `replication.py`'s docstring |
| Chained replication (replica-of-replica) | not implemented — a replica applies incoming writes without re-propagating them |
| `PUBLISH` forwarded to replicas | not implemented — a subscriber connected to a follower will not see messages published on the leader |
| Compact binary RDB format | EmberDB's snapshot format is JSON — deliberately, for safety (no code-exec-on-load risk the way `pickle` would have) and human-inspectability, at the cost of file size and load speed on very large datasets. See `DESIGN_DECISIONS.md` |
| Precise `jemalloc`-based memory accounting | EmberDB's `memsize.py` is a heuristic approximation, cached per key |
| True LFU (Redis's decaying Morris counter) | EmberDB's `allkeys-lfu`/`volatile-lfu` use a plain, non-decaying access counter — see `eviction.py`'s docstring |
| Sub-linear (HNSW/ANN) vector search | brute-force O(n·d) — fine for thousands of vectors, not for millions |
| Full JSONPath (wildcards, recursive descent, filters, slices) | a minimal subset: `$`, `.key`, `[idx]`, chained |
| A general query planner (joins, `OR`, multi-field indexes) | one field per index, `AND`-only conjunctive filters |
| `BLPOP`/`BRPOP`/blocking list operations | not implemented |
| Lua scripting (`EVAL`) | not implemented |
| Streams (`XADD`/`XREAD` consumer groups) | not implemented (CDC.* covers a different, narrower slice of the same problem space) |
| Full Redis ACL rule grammar (categories like `+@read`, per-command key patterns, selectors) | a small, practical subset: command-name globs + one key-pattern glob per user — see `acl.py`'s docstring |
| `redis.conf`-identical configuration file format | a compatible-*flavored* parser (`config.py`) covering EmberDB's own option set, not every real Redis directive |

None of these are secret — each one is called out in the relevant module's own docstring at the point where the simplification was made, and consolidated with the reasoning behind each choice in `DESIGN_DECISIONS.md`.

## What's genuinely equivalent

The core data model and its commands — String, List, Hash, Set, Sorted Set, with the same type semantics, the same `WRONGTYPE` error behavior, the same TTL model (active + passive expiration), the same 8 `maxmemory` eviction policies, the same RESP2/RESP3 wire protocol (verified against real `redis-cli`, not just against EmberDB's own client code) — this is a genuine, independent implementation of what Redis's core actually does, not a thin compatibility shim over a handful of commands.
