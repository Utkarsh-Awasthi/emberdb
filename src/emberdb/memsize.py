"""Approximate per-value byte-size estimate, used by eviction.py."""

from __future__ import annotations

from collections import deque
from typing import Any

from .valuetypes import JSONValue, VectorValue, ZSet

# Flat overhead per key: dict-entry pointer, KeyMeta object, key string
# object header, etc. Not meant to be exact — just non-zero and constant,
# so an empty-string key doesn't cost "0 bytes" and skew eviction choices.
PER_KEY_OVERHEAD = 64


def estimate_size(value: Any) -> int:
    if isinstance(value, str):
        return len(value.encode("utf-8", "surrogateescape"))
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    if isinstance(value, deque):
        return sum(estimate_size(v) + 8 for v in value)
    if isinstance(value, dict):
        return sum(estimate_size(k) + estimate_size(v) + 16 for k, v in value.items())
    if isinstance(value, set):
        return sum(estimate_size(v) + 8 for v in value)
    if isinstance(value, ZSet):
        return sum(estimate_size(m) + 16 for m in value.members())
    if isinstance(value, JSONValue):
        return _estimate_json(value.doc)
    if isinstance(value, VectorValue):
        return len(value.values) * 8 + estimate_size(value.namespace)
    if isinstance(value, (int, float, bool)) or value is None:
        return 8
    return 32  # unknown type: small constant rather than crashing


def _estimate_json(doc: Any) -> int:
    if isinstance(doc, str):
        return len(doc.encode("utf-8", "surrogateescape"))
    if isinstance(doc, dict):
        return sum(_estimate_json(k) + _estimate_json(v) + 8 for k, v in doc.items())
    if isinstance(doc, (list, tuple)):
        return sum(_estimate_json(v) + 4 for v in doc)
    if isinstance(doc, (int, float, bool)) or doc is None:
        return 8
    return 16
