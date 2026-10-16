# Command reference

157 commands. `[optional]`, `<required>`, `A|B` means "A or B". Commands are case-insensitive on the wire; shown here upper-case by convention. See [`COMPARISON.md`](COMPARISON.md) for what's *not* here that real Redis has, and the notes at the end of each section for documented syntax gaps.

## Strings

| Command | Notes |
|---|---|
| `SET <key> <value> [NX\|XX] [GET] [EX sec\|PX ms\|EXAT ts\|PXAT ts-ms] [KEEPTTL]` | |
| `GET <key>` | |
| `GETSET <key> <value>` | |
| `GETDEL <key>` | |
| `GETEX <key> [EX sec\|PX ms\|EXAT ts\|PXAT ts-ms\|PERSIST]` | |
| `APPEND <key> <value>` | |
| `STRLEN <key>` | |
| `INCR <key>` / `DECR <key>` | |
| `INCRBY <key> <n>` / `DECRBY <key> <n>` | |
| `INCRBYFLOAT <key> <n>` | |
| `GETRANGE <key> <start> <end>` | negative indices count from the end |
| `SETRANGE <key> <offset> <value>` | pads with `\x00` if `offset` is past the current length |
| `MSET <k1> <v1> [k2 v2 ...]` / `MSETNX ...` | `MSETNX` is all-or-nothing: any existing key aborts the whole call |
| `MGET <k1> [k2 ...]` | a wrong-type key yields `nil` for that slot, not a whole-command error |
| `SETNX <key> <value>` / `SETEX <key> <sec> <value>` / `PSETEX <key> <ms> <value>` | |

## Lists (backed by `collections.deque` — O(1) at both ends, O(n) for middle access; see `ARCHITECTURE.md`)

| Command | Notes |
|---|---|
| `LPUSH` / `RPUSH <key> <v1> [v2 ...]` | |
| `LPUSHX` / `RPUSHX <key> <value>` | no-op if the key doesn't already exist |
| `LPOP` / `RPOP <key> [count]` | |
| `LLEN <key>` | |
| `LINDEX <key> <index>` / `LSET <key> <index> <value>` | |
| `LRANGE <key> <start> <stop>` | |
| `LTRIM <key> <start> <stop>` | |
| `LREM <key> <count> <value>` | `count > 0`: head→tail; `< 0`: tail→head; `0`: all |
| `LINSERT <key> BEFORE\|AFTER <pivot> <value>` | |
| `LMOVE <src> <dst> LEFT\|RIGHT LEFT\|RIGHT` | |

*Gap: `BLPOP`/`BRPOP`/`BLMOVE` (blocking variants) aren't implemented.*

## Hashes

| Command | Notes |
|---|---|
| `HSET <key> <f1> <v1> [f2 v2 ...]` / `HMSET ...` | `HMSET` is a deprecated alias, kept for compatibility |
| `HSETNX <key> <field> <value>` | |
| `HGET <key> <field>` / `HMGET <key> <f1> [f2 ...]` | |
| `HGETALL <key>` | |
| `HDEL <key> <f1> [f2 ...]` | |
| `HEXISTS <key> <field>` / `HLEN <key>` / `HSTRLEN <key> <field>` | |
| `HKEYS <key>` / `HVALS <key>` | |
| `HINCRBY <key> <field> <n>` / `HINCRBYFLOAT <key> <field> <n>` | |
| `HRANDFIELD <key> [count [WITHVALUES]]` | |

## Sets

| Command | Notes |
|---|---|
| `SADD <key> <m1> [m2 ...]` / `SREM <key> <m1> [m2 ...]` | |
| `SMEMBERS <key>` / `SCARD <key>` | |
| `SISMEMBER <key> <member>` / `SMISMEMBER <key> <m1> [m2 ...]` | |
| `SUNION` / `SINTER` / `SDIFF <k1> [k2 ...]` | |
| `SUNIONSTORE` / `SINTERSTORE` / `SDIFFSTORE <dest> <k1> [k2 ...]` | |
| `SPOP <key> [count]` / `SRANDMEMBER <key> [count]` | `SRANDMEMBER` never mutates; negative `count` allows repeats |

## Sorted sets (backed by a from-scratch skip list — see `skiplist.py`)

| Command | Notes |
|---|---|
| `ZADD <key> [NX\|XX] [GT\|LT] [CH] [INCR] <score1> <member1> [...]` | |
| `ZSCORE <key> <member>` / `ZMSCORE <key> <m1> [m2 ...]` | |
| `ZCARD <key>` / `ZINCRBY <key> <n> <member>` | |
| `ZRANGE` / `ZREVRANGE <key> <start> <stop> [WITHSCORES]` | rank-based (classic syntax — see gap below) |
| `ZRANGEBYSCORE` / `ZREVRANGEBYSCORE <key> <min> <max> [WITHSCORES] [LIMIT off cnt]` | `(` prefix = exclusive bound; `-inf`/`+inf` supported |
| `ZCOUNT <key> <min> <max>` | |
| `ZRANK` / `ZREVRANK <key> <member> [WITHSCORE]` | |
| `ZREM <key> <m1> [m2 ...]` | |
| `ZPOPMIN` / `ZPOPMAX <key> [count]` | |

*Gap: the unified Redis 6.2+ syntax (`ZRANGE key min max BYSCORE|BYLEX [REV] [LIMIT ..]`) isn't implemented — use the classic per-purpose commands above, which cover the same functionality. No `BYLEX` (lexicographic ranges) at all.*

## Generic (type-agnostic) key commands

| Command | Notes |
|---|---|
| `DEL <k1> [k2 ...]` / `UNLINK ...` | `UNLINK` is a plain synchronous delete here — see `COMPARISON.md` |
| `EXISTS <k1> [k2 ...]` | counts duplicates |
| `TYPE <key>` | returns `string\|list\|hash\|set\|zset\|json\|vector\|none` |
| `TOUCH <k1> [k2 ...]` / `RANDOMKEY` | |
| `RENAME <src> <dst>` / `RENAMENX <src> <dst>` | |
| `COPY <src> <dst> [REPLACE]` | preserves TTL |
| `EXPIRE`/`PEXPIRE`/`EXPIREAT`/`PEXPIREAT <key> <n> [NX\|XX\|GT\|LT]` | all four always propagate as absolute-time `PEXPIREAT` — see `CODE_FLOW.md` |
| `TTL <key>` / `PTTL <key>` / `EXPIRETIME <key>` | `-2` = no such key, `-1` = no TTL |
| `PERSIST <key>` | |
| `KEYS <pattern>` | glob (`fnmatch`) |
| `SCAN <cursor> [MATCH pattern] [COUNT n] [TYPE t]` | cursor is a plain "how many sorted keys returned so far" position — see `DESIGN_DECISIONS.md` for why that's a safe substitute for Redis's hash-table-bucket cursor |
| `DBSIZE` | |
| `FLUSHDB` / `FLUSHALL` | index *definitions* survive a flush; their contents are cleared |

## Transactions

| Command | Notes |
|---|---|
| `MULTI` | begin queueing |
| `EXEC` | run the queue — **real all-or-nothing rollback on any runtime failure**, unlike Redis; see `CODE_FLOW.md` §4 |
| `DISCARD` | abandon the queue |
| `WATCH <k1> [k2 ...]` / `UNWATCH` | optimistic concurrency: `EXEC` aborts (nil) if a watched key's version changed |

## Pub/Sub

| Command | Notes |
|---|---|
| `SUBSCRIBE <ch1> [ch2 ...]` / `UNSUBSCRIBE [ch1 ...]` | |
| `PSUBSCRIBE <pat1> [...]` / `PUNSUBSCRIBE [pat1 ...]` | glob patterns |
| `PUBLISH <channel> <message>` | **not forwarded to replicas** — see `COMPARISON.md` |
| `PUBSUB CHANNELS [pattern]` / `PUBSUB NUMSUB [ch ...]` / `PUBSUB NUMPAT` | |

*Gap: pattern-matched deliveries use the same `"message"` push type as exact-channel ones, rather than Redis's distinct `"pmessage"` + pattern-name variant — see `commands/pubsub_cmds.py`'s docstring. No sharded pub/sub (`SSUBSCRIBE`/`SPUBLISH`, cluster-mode only in real Redis).*

## Server / connection administration

| Command | Notes |
|---|---|
| `PING [message]` / `ECHO <message>` | |
| `HELLO [2\|3] [AUTH user pass] [SETNAME name]` | protocol negotiation |
| `AUTH [username] <password>` | |
| `SELECT <index>` / `SWAPDB <i1> <i2>` | |
| `RESET` / `QUIT` | |
| `TIME` | |
| `INFO [section]` | Redis-style `# Section` text blocks |
| `CONFIG GET <pattern>` / `CONFIG SET <key> <value>` / `CONFIG REWRITE` | only keys in `config.py`'s `DEFAULT_CONFIG` are settable |
| `CLIENT GETNAME` / `SETNAME` / `ID` / `LIST` / `INFO` / `NO-EVICT` / `NO-TOUCH` / `PAUSE` / `UNPAUSE` | |
| `SLOWLOG GET [count]` / `RESET` / `LEN` | |
| `ACL WHOAMI` / `LIST` / `CAT` / `GETUSER <user>` / `SETUSER <user> [...]` / `DELUSER <user> [...]` | see `DESIGN_DECISIONS.md` for the deliberately small subset of real Redis ACL rules this covers |
| `SAVE` / `BGSAVE` / `LASTSAVE` | `BGSAVE` runs on a thread-pool executor so it doesn't block the event loop |
| `REPLICAOF <host> <port>` / `REPLICAOF NO ONE` / `SLAVEOF ...` | |
| `COMMAND [COUNT\|LIST\|DOCS]` | |

## JSON documents (`JSON.*`) — no separate RedisJSON module needed

| Command | Notes |
|---|---|
| `JSON.SET <key> <path> <json> [NX\|XX]` | the value argument is always JSON, including scalars (`JSON.SET k $ 42`, `JSON.SET k $ '"a string"'`) |
| `JSON.GET <key> [path]` | defaults to `$` (whole document) |
| `JSON.DEL <key> [path]` | |
| `JSON.TYPE <key> [path]` | `null\|boolean\|number\|string\|array\|object` |
| `JSON.NUMINCRBY <key> <path> <n>` | preserves int-vs-float, same as JSON itself does |
| `JSON.STRLEN` / `JSON.ARRLEN` / `JSON.OBJLEN <key> [path]` | |
| `JSON.ARRAPPEND <key> <path> <v1> [v2 ...]` | |
| `JSON.CLEAR <key> [path]` | empties an array/object or zeroes a number |

*Gap: paths use the subset `jsonpath.py` implements (`$`, `.key`, `[idx]`, chained) — no wildcards, no recursive descent (`..`), no filter expressions, no slices.*

## Secondary indexing / query engine — no separate RediSearch module needed

| Command | Notes |
|---|---|
| `CREATE INDEX <name> ON <pattern> FIELD <field>` | backfills every currently-matching key; returns the count |
| `DROP INDEX <name>` | |
| `FIND <index> WHERE <field> <op> <value> [AND <field> <op> <value> ...]` | `op` ∈ `= != > >= < <=`; the *first* condition, if it names the indexed field, drives lookup — everything else is a residual filter |
| `INDEXES` | list every registered index and how many keys it currently covers |

Works over Hash fields and JSON fields (dotted-path). One field per index, conjunctive (`AND`-only) filters, no joins — see `indexing.py`'s docstring for the reasoning.

## Vector similarity search — no separate module needed

| Command | Notes |
|---|---|
| `VECTOR.SET <namespace> <key> VALUES <v1> <v2> ... [ATTRS <json>]` | first insert into a namespace fixes its dimensionality |
| `VECTOR.DEL <namespace> <key>` | |
| `VECTOR.SEARCH <namespace> VALUES <v1> ... [K <k>] [METRIC cosine\|dot\|euclidean]` | flat `[key, score, key, score, ...]` reply; default `K` is 10, default metric `cosine` |
| `VECTOR.DESCRIBE` | dimensions + count per namespace |

Brute-force O(n·d) — fine for thousands of vectors, not sub-linear like a production ANN index. See `COMPARISON.md`.

## Change data capture (`CDC.*`)

| Command | Notes |
|---|---|
| `CDC.SINCE <offset> [limit]` | pull/poll model: every event with `offset > <offset>` |
| `CDC.TAIL` / `CDC.UNTAIL` | push model: this connection receives every new event as it happens |
| `CDC.STATUS` | current offset, in-memory ring size, live subscriber count |

## TTL webhooks (`HOOK.*`) — no separate polling worker needed

| Command | Notes |
|---|---|
| `HOOK.ONEXPIRE <name> ON <pattern> CALL <url>` | fire an HTTP POST when a matching key expires |
| `HOOK.DEL <name>` / `HOOK.LIST` | |

Fire-and-forget, best-effort delivery (no retry queue) — see `hooks.py`'s docstring for when to reach for this vs. `CDC.SINCE` instead.

## Branching (`BRANCH.*`) — no Redis equivalent

Git-style forking of the entire dataset, in-process and instant. Fork a branch, experiment destructively, then merge or discard.

| Command | Notes |
|---|---|
| `BRANCH.CREATE <name> [FROM <branch>]` | fork the current branch (or `FROM` another); returns keys copied. Does not switch |
| `BRANCH.SWITCH <name>` | check out a branch; all subsequent commands operate on its dataset |
| `BRANCH.CURRENT` | name of the checked-out branch |
| `BRANCH.LIST` | every branch with its parent, creation time, key count, and whether it's current |
| `BRANCH.DIFF [branch]` | keys added/modified/deleted on that branch since its fork point |
| `BRANCH.MERGE <source> [INTO <branch>] [STRATEGY fail\|theirs\|ours]` | three-way merge; returns `merged`, `applied`, and any `conflicts` |
| `BRANCH.DROP <name>` | delete a branch (not `main`, not the checked-out one) |

**Merge is three-way, not an overwrite.** A key is only carried over if it actually changed on the source since the fork point. If the target changed the same key since that fork, it's a conflict:

- `fail` (default) — abort the entire merge, change nothing, return the conflict list
- `theirs` — source wins
- `ours` — target keeps its value; non-conflicting changes still apply

**Branches are in-memory and ephemeral.** They do not survive a restart, writes made on a branch are deliberately *not* written to the AOF and *not* replicated (a replica has no notion of branches, and replaying branch writes into `main` on restart would corrupt it), and `SAVE`/`BGSAVE` always persist `main` even while another branch is checked out. Merging into `main` persists: each changed key is written to the AOF and sent to replicas as an `EMBER.RESTORE <key> <serialized-value> [PXAT ms]` (or `DEL`), an internal command that recreates any value type exactly. The `BRANCH.*` commands themselves are never logged or replicated. Forking deep-copies the dataset, so branches cost memory proportional to the data they hold — this is a dev/experimentation tool, not a storage-efficient VCS.
