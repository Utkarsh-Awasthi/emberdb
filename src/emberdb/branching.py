"""Git-style branching of the whole dataset: fork, switch, merge, drop.

Each branch owns an independent copy of every Database. A fork-point snapshot is
kept per branch so MERGE can do a real three-way comparison (changed-on-source vs
changed-on-target) rather than blindly overwriting.

Cost: forking deep-copies the dataset, so a branch roughly doubles memory for the
data it holds, and the fork-point snapshot doubles it again for that branch. This
is an in-memory dev/experimentation tool, not a storage-efficient VCS.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .database import Database
from .protocol import RespError

MAIN_BRANCH = "main"

MERGE_STRATEGIES = ("fail", "theirs", "ours")


@dataclass
class BranchMeta:
    name: str
    parent: Optional[str]
    created_at: float = field(default_factory=time.time)


# A fork-point image: {db_index: {key: (deep-copied value, expire_at or None)}}
ForkPoint = dict[int, dict[str, tuple[Any, Optional[float]]]]


def _image_of(databases: list[Database]) -> ForkPoint:
    image: ForkPoint = {}
    for db in databases:
        entries: dict[str, tuple[Any, Optional[float]]] = {}
        for key in list(db.data.keys()):
            if db.is_expired(key):
                continue
            entries[key] = (copy.deepcopy(db.data[key]), db.expires.get(key))
        image[db.index] = entries
    return image


def _clone_databases(databases: list[Database]) -> list[Database]:
    clones = []
    for db in databases:
        clone = Database(db.index)
        for key in list(db.data.keys()):
            if db.is_expired(key):
                continue
            clone.set_value(key, copy.deepcopy(db.data[key]))
            exp = db.expires.get(key)
            if exp is not None:
                clone.set_expire_at(key, exp)
        clones.append(clone)
    return clones


@dataclass
class MergeConflict:
    db_index: int
    key: str
    reason: str


class BranchManager:
    """Owns every branch's dataset. The engine holds one stable list object whose
    contents are swapped in place on switch, so expiry/eviction/index managers
    holding a reference to that list keep working."""

    def __init__(self, live_databases: list[Database]):
        self._live = live_databases
        self.current = MAIN_BRANCH
        self.meta: dict[str, BranchMeta] = {
            MAIN_BRANCH: BranchMeta(name=MAIN_BRANCH, parent=None)
        }
        # Datasets for branches that are not currently checked out.
        self._stored: dict[str, list[Database]] = {}
        self._fork_points: dict[str, ForkPoint] = {}

    # -- introspection -----------------------------------------------------

    def exists(self, name: str) -> bool:
        return name in self.meta

    def list_branches(self) -> list[dict]:
        out = []
        for name, m in sorted(self.meta.items()):
            dbs = self._live if name == self.current else self._stored.get(name, [])
            out.append({
                "name": name,
                "current": name == self.current,
                "parent": m.parent,
                "created_at": m.created_at,
                "keys": sum(db.size() for db in dbs),
            })
        return out

    def _require(self, name: str) -> None:
        if name not in self.meta:
            raise RespError(f"no such branch '{name}'")

    def _databases_of(self, name: str) -> list[Database]:
        return self._live if name == self.current else self._stored[name]

    def databases_of(self, name: str) -> list[Database]:
        """Public accessor — the engine snapshots 'main' even while another branch is live."""
        self._require(name)
        return self._databases_of(name)

    # -- lifecycle ---------------------------------------------------------

    def create(self, name: str, from_branch: Optional[str] = None) -> int:
        if name in self.meta:
            raise RespError(f"branch '{name}' already exists")
        if not name or any(c.isspace() for c in name):
            raise RespError("branch name must be non-empty and contain no whitespace")
        source = from_branch or self.current
        self._require(source)
        source_dbs = self._databases_of(source)

        self._stored[name] = _clone_databases(source_dbs)
        self._fork_points[name] = _image_of(source_dbs)
        self.meta[name] = BranchMeta(name=name, parent=source)
        return sum(db.size() for db in self._stored[name])

    def switch(self, name: str) -> None:
        self._require(name)
        if name == self.current:
            return
        # Park the current dataset, then swap the new one into the same list
        # object the rest of the engine already holds a reference to.
        self._stored[self.current] = list(self._live)
        target = self._stored.pop(name)
        self._live[:] = target
        self.current = name

    def drop(self, name: str) -> None:
        self._require(name)
        if name == MAIN_BRANCH:
            raise RespError("cannot drop the 'main' branch")
        if name == self.current:
            raise RespError(f"cannot drop the checked-out branch '{name}'; switch away first")
        self._stored.pop(name, None)
        self._fork_points.pop(name, None)
        self.meta.pop(name, None)

    # -- merge -------------------------------------------------------------

    def merge(self, source: str, into: Optional[str] = None,
              strategy: str = "fail") -> dict:
        """Apply changes made on `source` since its fork point onto `into`
        (default: the current branch).

        Three-way: a key is only carried over if it actually changed on the
        source since the fork. If the target also changed that key since the
        same fork point, it's a conflict, resolved per `strategy`:
          fail   - abort the whole merge, change nothing (default)
          theirs - source wins
          ours   - target wins (conflicting key left as-is)
        """
        if strategy not in MERGE_STRATEGIES:
            raise RespError(f"unknown merge strategy '{strategy}', expected one of {', '.join(MERGE_STRATEGIES)}")
        self._require(source)
        target = into or self.current
        self._require(target)
        if source == target:
            raise RespError("cannot merge a branch into itself")
        if source == MAIN_BRANCH and source not in self._fork_points:
            raise RespError("cannot merge 'main': it has no fork point")

        fork = self._fork_points.get(source, {})
        source_dbs = self._databases_of(source)
        target_dbs = self._databases_of(target)

        planned: list[tuple[int, str, str, Any, Optional[float]]] = []  # (db, key, action, value, expire_at)
        conflicts: list[MergeConflict] = []

        for sdb in source_dbs:
            i = sdb.index
            fork_entries = fork.get(i, {})
            tdb = target_dbs[i]

            source_keys = {k for k in sdb.data.keys() if not sdb.is_expired(k)}
            target_keys = {k for k in tdb.data.keys() if not tdb.is_expired(k)}

            # keys added or modified on the source since the fork
            for key in source_keys:
                s_val = sdb.data[key]
                forked = fork_entries.get(key)
                changed_on_source = forked is None or forked[0] != s_val
                if not changed_on_source:
                    continue
                t_present = key in target_keys
                t_val = tdb.data.get(key)
                changed_on_target = (
                    (forked is None and t_present)
                    or (forked is not None and (not t_present or forked[0] != t_val))
                )
                if changed_on_target and t_val != s_val:
                    if strategy == "fail":
                        conflicts.append(MergeConflict(i, key, "modified on both branches"))
                        continue
                    if strategy == "ours":
                        continue
                planned.append((i, key, "set", copy.deepcopy(s_val), sdb.expires.get(key)))

            # keys deleted on the source since the fork
            for key, (f_val, _f_exp) in fork_entries.items():
                if key in source_keys:
                    continue
                if key not in target_keys:
                    continue
                changed_on_target = tdb.data.get(key) != f_val
                if changed_on_target:
                    if strategy == "fail":
                        conflicts.append(MergeConflict(i, key, "deleted on source, modified on target"))
                        continue
                    if strategy == "ours":
                        continue
                planned.append((i, key, "delete", None, None))

        if conflicts and strategy == "fail":
            return {
                "merged": False,
                "applied": 0,
                "conflicts": [{"db": c.db_index, "key": c.key, "reason": c.reason} for c in conflicts],
            }

        for db_index, key, action, value, expire_at in planned:
            tdb = target_dbs[db_index]
            if action == "set":
                tdb.set_value(key, value)
                if expire_at is not None:
                    tdb.set_expire_at(key, expire_at)
            else:
                tdb.delete(key)

        # The source's history is now folded in; re-baseline it so a second
        # merge doesn't replay the same changes.
        self._fork_points[source] = _image_of(source_dbs)

        return {
            "merged": True,
            "applied": len(planned),
            "conflicts": [{"db": c.db_index, "key": c.key, "reason": c.reason} for c in conflicts],
            # internal: consumed (and removed) by the engine to persist/replicate
            "_changes": [(db_index, key, action) for db_index, key, action, _v, _e in planned],
            "_target": target,
        }

    # -- diff --------------------------------------------------------------

    def diff(self, name: Optional[str] = None) -> list[dict]:
        """What changed on `name` (default: current) since its fork point."""
        branch = name or self.current
        self._require(branch)
        fork = self._fork_points.get(branch)
        if fork is None:
            return []
        dbs = self._databases_of(branch)
        out = []
        for db in dbs:
            fork_entries = fork.get(db.index, {})
            live_keys = {k for k in db.data.keys() if not db.is_expired(k)}
            for key in sorted(live_keys):
                forked = fork_entries.get(key)
                if forked is None:
                    out.append({"db": db.index, "key": key, "change": "added"})
                elif forked[0] != db.data[key]:
                    out.append({"db": db.index, "key": key, "change": "modified"})
            for key in sorted(fork_entries):
                if key not in live_keys:
                    out.append({"db": db.index, "key": key, "change": "deleted"})
        return out
