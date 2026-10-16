"""Brute-force vector similarity search."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .protocol import RespError


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _norm(a: list[float]) -> float:
    return math.sqrt(sum(x * x for x in a))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    na, nb = _norm(a), _norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return _dot(a, b) / (na * nb)


def euclidean_distance(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


METRICS = {
    # (fn, higher_is_better) — search sorts accordingly for each metric
    "cosine": (cosine_similarity, True),
    "dot": (_dot, True),
    "euclidean": (euclidean_distance, False),
}


@dataclass
class SearchHit:
    key: str
    score: float


@dataclass
class _Namespace:
    dim: Optional[int] = None
    vectors: dict[str, list[float]] = field(default_factory=dict)
    attrs: dict[str, dict] = field(default_factory=dict)


class VectorSearchEngine:
    def __init__(self):
        self._namespaces: dict[str, _Namespace] = {}

    def _ns(self, namespace: str, create: bool = False) -> Optional[_Namespace]:
        ns = self._namespaces.get(namespace)
        if ns is None and create:
            ns = _Namespace()
            self._namespaces[namespace] = ns
        return ns

    def upsert(self, namespace: str, key: str, values: list[float], attrs: Optional[dict] = None) -> None:
        ns = self._ns(namespace, create=True)
        if ns.dim is None:
            ns.dim = len(values)
        elif len(values) != ns.dim:
            raise RespError(
                f"vector dimension mismatch: namespace '{namespace}' expects {ns.dim}, got {len(values)}"
            )
        ns.vectors[key] = values
        if attrs is not None:
            ns.attrs[key] = attrs

    def remove(self, namespace: str, key: str) -> bool:
        ns = self._namespaces.get(namespace)
        if ns is None:
            return False
        existed = ns.vectors.pop(key, None) is not None
        ns.attrs.pop(key, None)
        return existed

    def search(self, namespace: str, query: list[float], k: int = 10, metric: str = "cosine") -> list[SearchHit]:
        ns = self._namespaces.get(namespace)
        if ns is None or not ns.vectors:
            return []
        if ns.dim is not None and len(query) != ns.dim:
            raise RespError(
                f"query vector dimension {len(query)} does not match namespace dimension {ns.dim}"
            )
        fn, higher_is_better = METRICS.get(metric, (None, None))
        if fn is None:
            raise RespError(f"unknown metric '{metric}', expected one of: {', '.join(METRICS)}")
        scored = [SearchHit(key=key, score=fn(query, vec)) for key, vec in ns.vectors.items()]
        scored.sort(key=lambda h: h.score, reverse=higher_is_better)
        return scored[:k]

    def describe(self) -> list[dict]:
        return [
            {"namespace": name, "dimensions": ns.dim, "count": len(ns.vectors)}
            for name, ns in self._namespaces.items()
        ]

    def namespace_size(self, namespace: str) -> int:
        ns = self._namespaces.get(namespace)
        return len(ns.vectors) if ns else 0
