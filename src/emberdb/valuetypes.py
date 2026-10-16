"""Value types a key can hold, split from database.py to avoid a circular import with memsize.py."""

from __future__ import annotations

from collections import deque
from typing import Any, Iterator, Optional

from .protocol import RespError
from .skiplist import SkipList


class ZSet:
    """Sorted set: skiplist (ordering/rank/range) + dict (O(1) score lookup)."""

    __slots__ = ("_sl", "_scores")

    def __init__(self):
        self._sl = SkipList()
        self._scores: dict[str, float] = {}

    def __len__(self) -> int:
        return len(self._scores)

    def score(self, member: str) -> Optional[float]:
        return self._scores.get(member)

    def add(self, member: str, score: float) -> bool:
        """Insert or update. Returns True if `member` is new."""
        old = self._scores.get(member)
        if old is not None:
            if old == score:
                return False
            self._sl.delete(old, member)  # skiplists have no in-place re-key
        self._sl.insert(score, member)
        self._scores[member] = score
        return old is None

    def remove(self, member: str) -> bool:
        old = self._scores.pop(member, None)
        if old is None:
            return False
        self._sl.delete(old, member)
        return True

    def rank(self, member: str, reverse: bool = False) -> Optional[int]:
        score = self._scores.get(member)
        if score is None:
            return None
        r = self._sl.rank(score, member)
        return (len(self) - 1 - r) if reverse else r

    def range_by_rank(self, start: int, stop: int, reverse: bool = False) -> list[tuple[str, float]]:
        if reverse:
            n = len(self)
            lo, hi = _reverse_range(start, stop, n)
            raw = self._sl.range_by_rank(lo, hi)
            return [(m, s) for s, m in reversed(raw)]
        return [(m, s) for s, m in self._sl.range_by_rank(start, stop)]

    def range_by_score(self, lo, hi, lo_ex=False, hi_ex=False, offset=0, count=None, reverse=False):
        raw = self._sl.range_by_score(lo, hi, lo_ex, hi_ex, offset, count if not reverse else None)
        pairs = [(m, s) for s, m in raw]
        if reverse:
            pairs.reverse()
            if count is not None:
                pairs = pairs[offset:offset + count]
        return pairs

    def count_by_score(self, lo, hi, lo_ex=False, hi_ex=False) -> int:
        return len(self._sl.range_by_score(lo, hi, lo_ex, hi_ex))

    def members(self) -> Iterator[str]:
        return iter(self._scores.keys())

    def items_ascending(self) -> Iterator[tuple[str, float]]:
        for score, member in self._sl:
            yield member, score


def _reverse_range(start: int, stop: int, n: int) -> tuple[int, int]:
    """'From the high end' rank range -> ascending rank range over n elements."""
    if start < 0:
        start = max(n + start, 0)
    if stop < 0:
        stop = n + stop
    hi = n - 1 - start
    lo = n - 1 - stop
    return lo, hi


class JSONValue:
    """A JSON document attached to a key (wraps json.loads output)."""

    __slots__ = ("doc",)

    def __init__(self, doc: Any):
        self.doc = doc


class VectorValue:
    """A float vector attached to a key. `namespace` groups vectors searched together."""

    __slots__ = ("values", "namespace", "attrs")

    def __init__(self, values: list[float], namespace: str, attrs: Optional[dict] = None):
        self.values = values
        self.namespace = namespace
        self.attrs = attrs or {}


def type_name_of(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, JSONValue):
        return "json"
    if isinstance(value, VectorValue):
        return "vector"
    if isinstance(value, ZSet):
        return "zset"
    if isinstance(value, str):
        return "string"
    if isinstance(value, deque):
        return "list"
    if isinstance(value, dict):
        return "hash"
    if isinstance(value, set):
        return "set"
    return "unknown"


def expect_type(value: Any, py_type, redis_command: str = "") -> Any:
    """Raise WRONGTYPE if `value` exists but isn't `py_type`; else return it unchanged."""
    if value is not None and not isinstance(value, py_type):
        raise RespError(
            "Operation against a key holding the wrong kind of value",
            code="WRONGTYPE",
        )
    return value
