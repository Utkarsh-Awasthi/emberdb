"""Configuration defaults and redis.conf-style file parsing."""

from __future__ import annotations

import shlex
from typing import Optional

DEFAULT_CONFIG: dict = {
    "port": 7379,
    "http_port": 7380,
    "bind": "0.0.0.0",
    "databases": 16,
    "maxmemory": 0,                   # 0 = unlimited; named to match Redis's own CONFIG GET/SET key
    "maxmemory_policy": "noeviction",
    "appendonly": False,
    "aof_path": "emberdb.aof.jsonl",
    "aof_fsync": "everysec",          # "always" | "everysec" | "no"
    "snapshot_path": "emberdb.snapshot.json",
    "save_interval_seconds": 300,     # background auto-SAVE cadence; see __main__.py
    "cdc_path": "emberdb.cdc.jsonl",
    "cdc_ring_capacity": 5000,
    "active_expire_interval": 0.1,
    "slowlog_threshold_ms": 10.0,
    "requirepass": "",                # empty = no password required, matches real Redis's default
    "logfile": "",                    # empty = log to stderr
}

_BOOL_KEYS = {"appendonly"}
_INT_KEYS = {"port", "http_port", "databases", "maxmemory", "cdc_ring_capacity", "save_interval_seconds"}
_FLOAT_KEYS = {"active_expire_interval", "slowlog_threshold_ms"}


def _coerce(key: str, value: str):
    if key in _BOOL_KEYS:
        return value.lower() in ("yes", "true", "1")
    if key in _INT_KEYS:
        return int(value)
    if key in _FLOAT_KEYS:
        return float(value)
    return value


def parse_config_file(path: str) -> dict:
    """redis.conf-style: `key value` per line, `#` comments, shlex-quoted values."""
    overrides: dict = {}
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = shlex.split(line)
            if not parts:
                continue
            key, *rest = parts
            key = key.lower()
            overrides[key] = _coerce(key, " ".join(rest))
    return overrides


def load_config(path: Optional[str] = None, cli_overrides: Optional[dict] = None) -> dict:
    """Resolve defaults -> config file -> explicit overrides, in that
    order (each layer wins over the one before it)."""
    cfg = dict(DEFAULT_CONFIG)
    if path:
        cfg.update(parse_config_file(path))
    if cli_overrides:
        cfg.update({k: v for k, v in cli_overrides.items() if v is not None})
    return cfg
