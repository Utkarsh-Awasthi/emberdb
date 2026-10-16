"""Skip list backing ZSet: O(log n) insert/delete/rank/range."""

from __future__ import annotations

import random
from typing import Iterator, Optional


class _Node:
    __slots__ = ("member", "score", "forward", "span", "backward")

    def __init__(self, level: int, member: Optional[str] = None, score: float = 0.0):
        self.member = member
        self.score = score
        # forward[i]: next node at level i. span[i]: how many level-0 nodes
        # you cross by taking that forward pointer (used to compute rank).
        self.forward: list[Optional["_Node"]] = [None] * level
        self.span: list[int] = [0] * level
        self.backward: Optional["_Node"] = None


class SkipList:
    """Ordered index of (score, member) pairs; ties broken lexically by member."""

    MAX_LEVEL = 32
    P = 0.25  # probability of promoting a node to the next level up

    def __init__(self):
        self._header = _Node(self.MAX_LEVEL)
        self._level = 1
        self._length = 0
        self._tail: Optional[_Node] = None

    def __len__(self) -> int:
        return self._length

    # -- internal helpers ---------------------------------------------------

    def _random_level(self) -> int:
        lvl = 1
        while random.random() < self.P and lvl < self.MAX_LEVEL:
            lvl += 1
        return lvl

    @staticmethod
    def _less(score_a: float, member_a: str, score_b: float, member_b: str) -> bool:
        """True if (score_a, member_a) sorts strictly before (score_b, member_b)."""
        if score_a != score_b:
            return score_a < score_b
        return member_a < member_b

    # -- mutation -------------------------------------------------------------

    def insert(self, score: float, member: str) -> None:
        """Insert a new (score, member) pair. Caller ensures member isn't already present."""
        update: list[_Node] = [self._header] * self.MAX_LEVEL
        rank = [0] * self.MAX_LEVEL
        x = self._header
        for i in range(self._level - 1, -1, -1):
            rank[i] = rank[i + 1] if i != self._level - 1 else 0
            while x.forward[i] is not None and self._less(
                x.forward[i].score, x.forward[i].member, score, member
            ):
                rank[i] += x.span[i]
                x = x.forward[i]
            update[i] = x

        level = self._random_level()
        if level > self._level:
            for i in range(self._level, level):
                rank[i] = 0
                update[i] = self._header
                update[i].span[i] = self._length
            self._level = level

        node = _Node(level, member, score)
        for i in range(level):
            node.forward[i] = update[i].forward[i]
            update[i].forward[i] = node
            node.span[i] = update[i].span[i] - (rank[0] - rank[i])
            update[i].span[i] = (rank[0] - rank[i]) + 1
        for i in range(level, self._level):
            update[i].span[i] += 1

        node.backward = update[0] if update[0] is not self._header else None
        if node.forward[0] is not None:
            node.forward[0].backward = node
        else:
            self._tail = node
        self._length += 1

    def delete(self, score: float, member: str) -> bool:
        """Remove the (score, member) pair. Returns False if not found."""
        update: list[_Node] = [self._header] * self.MAX_LEVEL
        x = self._header
        for i in range(self._level - 1, -1, -1):
            while x.forward[i] is not None and self._less(
                x.forward[i].score, x.forward[i].member, score, member
            ):
                x = x.forward[i]
            update[i] = x
        x = x.forward[0]
        if x is None or x.score != score or x.member != member:
            return False
        for i in range(self._level):
            if update[i].forward[i] is x:
                update[i].forward[i] = x.forward[i]
                update[i].span[i] += x.span[i] - 1
            else:
                update[i].span[i] -= 1
        if x.forward[0] is not None:
            x.forward[0].backward = x.backward
        else:
            self._tail = x.backward
        while self._level > 1 and self._header.forward[self._level - 1] is None:
            self._level -= 1
        self._length -= 1
        return True

    # -- ordered access ---------------------------------------------------

    def rank(self, score: float, member: str) -> Optional[int]:
        """0-based rank of (score, member) in ascending order, or None."""
        x = self._header
        traversed = 0
        for i in range(self._level - 1, -1, -1):
            while x.forward[i] is not None and self._less(
                x.forward[i].score, x.forward[i].member, score, member
            ):
                traversed += x.span[i]
                x = x.forward[i]
            if x.forward[i] is not None and x.forward[i].score == score and x.forward[i].member == member:
                return traversed + x.span[i] - 1
        return None

    def _seek_rank1(self, rank1: int) -> Optional[_Node]:
        """Find the node at 1-indexed rank `rank1` in O(log n) via spans."""
        x = self._header
        traversed = 0
        for i in range(self._level - 1, -1, -1):
            while x.forward[i] is not None and (traversed + x.span[i]) <= rank1:
                traversed += x.span[i]
                x = x.forward[i]
            if traversed == rank1:
                return x
        return None

    def at_rank(self, rank: int) -> Optional[tuple[float, str]]:
        """0-based ascending-order lookup: the (score, member) at this rank."""
        if rank < 0 or rank >= self._length:
            return None
        node = self._seek_rank1(rank + 1)
        return (node.score, node.member) if node else None

    def range_by_rank(self, start: int, stop: int) -> list[tuple[float, str]]:
        """Inclusive ascending range by 0-based rank, negative indices allowed."""
        n = self._length
        if n == 0:
            return []
        if start < 0:
            start = max(n + start, 0)
        if stop < 0:
            stop = n + stop
        stop = min(stop, n - 1)
        if start > stop or start >= n:
            return []
        x = self._seek_rank1(start + 1)
        result = []
        for _ in range(stop - start + 1):
            if x is None:
                break
            result.append((x.score, x.member))
            x = x.forward[0]
        return result

    def range_by_score(
        self, min_score: float, max_score: float,
        min_exclusive: bool = False, max_exclusive: bool = False,
        offset: int = 0, count: Optional[int] = None,
    ) -> list[tuple[float, str]]:
        """Ascending range of all (score, member) with min_score <= score <= max_score."""
        x = self._header
        for i in range(self._level - 1, -1, -1):
            while x.forward[i] is not None and (
                x.forward[i].score < min_score
                or (min_exclusive and x.forward[i].score == min_score)
            ):
                x = x.forward[i]
        x = x.forward[0]
        result = []
        skipped = 0
        while x is not None:
            if x.score > max_score or (max_exclusive and x.score == max_score):
                break
            if skipped < offset:
                skipped += 1
                x = x.forward[0]
                continue
            result.append((x.score, x.member))
            if count is not None and len(result) >= count:
                break
            x = x.forward[0]
        return result

    def __iter__(self) -> Iterator[tuple[float, str]]:
        x = self._header.forward[0]
        while x is not None:
            yield (x.score, x.member)
            x = x.forward[0]

    def __reversed__(self) -> Iterator[tuple[float, str]]:
        x = self._tail
        while x is not None:
            yield (x.score, x.member)
            x = x.backward
