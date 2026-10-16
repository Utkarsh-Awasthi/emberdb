"""Secondary indexes and the CREATE INDEX / FIND query engine."""

from __future__ import annotations

import bisect
import fnmatch
from dataclasses import dataclass, field
from typing import Any, Optional

from .database import Database
from .jsonpath import get_path
from .valuetypes import JSONValue
from .protocol import RespError

_OPS = {
    "=": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a is not None and b is not None and a > b,
    ">=": lambda a, b: a is not None and b is not None and a >= b,
    "<": lambda a, b: a is not None and b is not None and a < b,
    "<=": lambda a, b: a is not None and b is not None and a <= b,
}


def _extract_field(value: Any, field_name: str) -> Any:
    """Pull `field_name` out of a Hash (dict) or a JSON document
    (JSONValue, via a dotted JSONPath). Any other type has no fields to
    index and yields None."""
    if isinstance(value, JSONValue):
        found, v = get_path(value.doc, field_name)
        return v if found else None
    if isinstance(value, dict):
        return value.get(field_name)
    return None


def _coerce_number(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


@dataclass
class IndexDef:
    name: str
    db_index: int
    pattern: str
    field: str
    # value (as string, for equality) -> matching keys
    equality: dict[str, set[str]] = field(default_factory=dict)
    # sorted (numeric_value, key) pairs, for range queries
    numeric: list[tuple[float, str]] = field(default_factory=list)
    # last-seen raw field value per key, so we know what to remove from
    # `equality`/`numeric` when the key is overwritten or deleted
    current: dict[str, Any] = field(default_factory=dict)

    def matches_key(self, key: str) -> bool:
        return fnmatch.fnmatchcase(key, self.pattern)


class IndexManager:
    def __init__(self, databases: list[Database]):
        self._databases = databases
        self.indexes: dict[str, IndexDef] = {}

    def _db(self, index: int) -> Database:
        return self._databases[index]

    # -- lifecycle ------------------------------------------------------

    def create_index(self, name: str, db_index: int, pattern: str, field_name: str) -> int:
        if name in self.indexes:
            raise RespError(f"index '{name}' already exists")
        idx = IndexDef(name=name, db_index=db_index, pattern=pattern, field=field_name)
        self.indexes[name] = idx
        db = self._db(db_index)
        backfilled = 0
        for key in db.all_keys():
            if idx.matches_key(key):
                self._reindex_key(idx, db, key)
                backfilled += 1
        return backfilled

    def drop_index(self, name: str) -> bool:
        return self.indexes.pop(name, None) is not None

    def list_indexes(self) -> list[dict]:
        return [
            {"name": i.name, "db": i.db_index, "pattern": i.pattern,
             "field": i.field, "keys_indexed": len(i.current)}
            for i in self.indexes.values()
        ]

    def clear_db(self, db_index: int) -> None:
        """FLUSHDB wipes every key in one database — every index watching
        that db_index loses its backing keys too. The index *definition*
        survives (matching how a real RediSearch index definition outlives
        FLUSHDB), only its contents are emptied."""
        for idx in self.indexes.values():
            if idx.db_index == db_index:
                idx.equality.clear()
                idx.numeric.clear()
                idx.current.clear()

    def clear_all(self) -> None:
        for idx in self.indexes.values():
            idx.equality.clear()
            idx.numeric.clear()
            idx.current.clear()

    def rebuild_all(self) -> None:
        """Re-backfill every index from the current dataset. Needed after the
        underlying databases are swapped wholesale (branch switch/merge)."""
        self.clear_all()
        for idx in self.indexes.values():
            db = self._db(idx.db_index)
            for key in db.all_keys():
                if idx.matches_key(key):
                    self._reindex_key(idx, db, key)

    # -- keeping indexes in sync with writes --------------------------------

    def on_write(self, db_index: int, key: str) -> None:
        db = self._db(db_index)
        for idx in self.indexes.values():
            if idx.db_index == db_index and idx.matches_key(key):
                self._reindex_key(idx, db, key)

    def on_delete(self, db_index: int, key: str) -> None:
        for idx in self.indexes.values():
            if idx.db_index == db_index and key in idx.current:
                self._remove_from_index(idx, key)

    def _remove_from_index(self, idx: IndexDef, key: str) -> None:
        old = idx.current.pop(key, None)
        if old is None:
            return
        old_str = str(old)
        bucket = idx.equality.get(old_str)
        if bucket is not None:
            bucket.discard(key)
            if not bucket:
                del idx.equality[old_str]
        num = _coerce_number(old)
        if num is not None:
            pos = bisect.bisect_left(idx.numeric, (num, key))
            if pos < len(idx.numeric) and idx.numeric[pos] == (num, key):
                idx.numeric.pop(pos)

    def _reindex_key(self, idx: IndexDef, db: Database, key: str) -> None:
        self._remove_from_index(idx, key)
        value = db.peek(key)
        if value is None:
            return
        field_value = _extract_field(value, idx.field)
        if field_value is None:
            return
        idx.current[key] = field_value
        idx.equality.setdefault(str(field_value), set()).add(key)
        num = _coerce_number(field_value)
        if num is not None:
            bisect.insort(idx.numeric, (num, key))

    # -- querying -----------------------------------------------------------

    def find(self, index_name: str, conditions: list[tuple[str, str, str]]) -> list[str]:
        """`conditions`: (field, op, value) tuples. First condition on the indexed
        field drives lookup; the rest are a residual filter."""
        idx = self.indexes.get(index_name)
        if idx is None:
            raise RespError(f"no such index '{index_name}'")

        candidates: Optional[set[str]] = None
        residual: list[tuple[str, str, str]] = []
        for i, (fname, op, raw) in enumerate(conditions):
            if i == 0 and fname == idx.field:
                candidates = self._candidates_for(idx, op, raw)
            else:
                residual.append((fname, op, raw))

        if candidates is None:
            candidates = set(idx.current.keys())

        if not residual:
            return sorted(candidates)

        db = self._db(idx.db_index)
        out = []
        for key in candidates:
            value = db.peek(key)
            if value is None:
                continue
            ok = True
            for fname, op, raw in residual:
                actual = _extract_field(value, fname)
                if not _compare(actual, op, raw):
                    ok = False
                    break
            if ok:
                out.append(key)
        return sorted(out)

    def _candidates_for(self, idx: IndexDef, op: str, raw: str) -> set[str]:
        if op == "=":
            return set(idx.equality.get(raw, ()))
        if op == "!=":
            all_keys = set(idx.current.keys())
            return all_keys - set(idx.equality.get(raw, ()))
        # range operators require the query value to parse as a number
        num = _coerce_number(raw)
        if num is None:
            raise RespError(f"'{raw}' is not numeric, required for operator '{op}'")
        keys = set()
        if op == ">":
            i = bisect.bisect_right(idx.numeric, (num, chr(0x10FFFF)))
            keys = {k for _, k in idx.numeric[i:]}
        elif op == ">=":
            i = bisect.bisect_left(idx.numeric, (num, ""))
            keys = {k for _, k in idx.numeric[i:]}
        elif op == "<":
            i = bisect.bisect_left(idx.numeric, (num, ""))
            keys = {k for _, k in idx.numeric[:i]}
        elif op == "<=":
            i = bisect.bisect_right(idx.numeric, (num, chr(0x10FFFF)))
            keys = {k for _, k in idx.numeric[:i]}
        else:
            raise RespError(f"unsupported operator '{op}'")
        return keys


def _compare(actual: Any, op: str, raw: str) -> bool:
    fn = _OPS.get(op)
    if fn is None:
        raise RespError(f"unsupported operator '{op}'")
    if op in ("=", "!="):
        return fn(str(actual) if actual is not None else None, raw)
    num_actual = _coerce_number(actual)
    num_raw = _coerce_number(raw)
    return fn(num_actual, num_raw)
