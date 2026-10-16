# HTTP API

The dashboard at `http://localhost:7380/` (default `--http-port`) is a client of this same REST API and WebSocket endpoint — nothing here is dashboard-only. Build your own tooling against it if you like.

Implementation notes (see `docs/DESIGN_DECISIONS.md` for the reasoning): hand-rolled HTTP/1.1, no TLS, no chunked encoding, no persistent keep-alive (every request closes its connection after responding), CORS wide open (`Access-Control-Allow-Origin: *`).

## Authentication

If `requirepass` is set (see `GETTING_STARTED.md`), every `/api/*` and `/ws` request needs a bearer token — the same password used for RESP `AUTH`:

```
Authorization: Bearer your-password
```

or, for the WebSocket endpoint where custom headers are less convenient from a browser: `?token=your-password` as a query parameter. No `requirepass` configured means no auth is required (matching the RESP server's own default).

## `GET /api/v1/stats`

Everything the dashboard's KPI strip and status bar need in one call.

```json
{
  "metrics": { "uptime_seconds": 120.4, "commands_processed": 5031, "connected_clients": 3,
               "keyspace_hits": 4200, "keyspace_misses": 130, "hit_rate": 0.97,
               "ops_per_sec": 42, "ops_history": [40, 41, 38, ...], "top_commands": [["GET", 3000], ...] },
  "eviction": { "maxmemory": 0, "maxmemory_policy": "noeviction", "evicted_keys": 0, "oom_denials": 0 },
  "replication": { "role": "master", "connected_replicas": 0, "link_status": "down" },
  "keyspace": [ { "db": 0, "keys": 42, "expires": 3, "bytes": 8192 } ],
  "cdc": { "current_offset": 517, "events_in_memory": 517, "live_subscribers": 1, "file": "emberdb.cdc.jsonl" },
  "vectors": [ { "namespace": "images", "dimensions": 128, "count": 900 } ],
  "hooks": [ { "name": "cleanup", "db": 0, "pattern": "session:*", "url": "https://...", "event": "expired" } ],
  "uptime_seconds": 120.4,
  "version": "0.1.0"
}
```

## `GET /api/v1/keyspace?db=0&pattern=*&cursor=0&count=50`

Wraps `SCAN` (see `docs/COMMANDS.md`) and enriches each key with real access metadata:

```json
{ "cursor": "50", "keys": [
  { "key": "user:1", "type": "hash", "ttl": null, "last_access_seconds_ago": 4.2, "access_count": 17 }
] }
```

## `GET /api/v1/heatmap?db=0&limit=200`

Walks `SCAN` to completion (or `limit`) rather than one page, returning the raw access data the dashboard's heat-strip renders:

```json
{ "cells": [ { "key": "user:1", "last_access_seconds_ago": 4.2, "access_count": 17 } ] }
```

## `GET /api/v1/keys/<key>?db=0`

Type-appropriate value dump plus TTL:

```json
{ "key": "user:1", "found": true, "type": "hash", "ttl": null,
  "value": { "name": "Alice", "email": "alice@example.com" } }
```

`{ "found": false }` if the key doesn't exist. `value` uses whichever read command fits the type (`GET`/`LRANGE 0 -1`/`HGETALL`/`SMEMBERS`/`ZRANGE 0 -1 WITHSCORES`/`JSON.GET`) — sets are returned as a sorted JSON array (JSON has no native set type).

## `POST /api/v1/command`

Execute *any* command — this is what the dashboard's command console runs against, and it's the same `engine.execute()` a RESP client's commands run through (see `docs/CODE_FLOW.md` §6).

Request:
```json
{ "db": 0, "argv": ["SET", "foo", "bar"] }
```

Response (200):
```json
{ "reply": "OK" }
```

Response (400, on a command-level error — e.g. `WRONGTYPE`):
```json
{ "error": "WRONGTYPE Operation against a key holding the wrong kind of value" }
```

## `GET /api/v1/cdc?offset=0&limit=100`

Wraps `CDC.SINCE`:

```json
{ "events": [ { "offset": 12, "ts": 1732900000.1, "db": 0, "type": "write", "cmd": "SET", "key": "foo", "args": ["foo"] } ] }
```

## `GET /api/v1/indexes`

Wraps `INDEXES`:

```json
{ "indexes": [ { "name": "idx_email", "db": 0, "pattern": "user:*", "field": "email", "keys_indexed": 42 } ] }
```

## `GET /ws` — live updates

A WebSocket connection receives a JSON text frame roughly once per second:

```json
{ "type": "tick", "stats": { /* same shape as GET /api/v1/stats */ },
  "cdc_events": [ /* any new CDC events since the last tick, up to 50 */ ] }
```

`cdc_events` is present only when there's something new — the dashboard's live change-stream tail and its heat-strip refresh both key off this same message. The server doesn't expect or act on anything the client sends over the socket beyond the initial handshake; a ping/pong or stray message is drained and ignored.

## Error format

Every error response is `{"error": "<message>"}` with an appropriate status code (`400` for a command/validation error, `401` for missing/invalid auth, `404` for an unknown route, `500` for an unexpected server error).
