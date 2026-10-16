"""Minimal JSONPath subset for JSON.* commands: $, .key, [idx], chained."""

from __future__ import annotations

import re
from typing import Any

_TOKEN_RE = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(-?\d+)\]")


class JSONPathError(Exception):
    pass


def _tokenize(path: str) -> list:
    path = path.strip()
    if path in ("$", ""):
        return []
    if path.startswith("$"):
        path = path[1:]
    # Normalize so the path always reads as a run of ".key" / "[idx]"
    # tokens starting at position 0 — a bare leading key ("user.name",
    # with no "$" and no leading dot) needs a "." prepended so the token
    # regex below (which only matches starting with '.' or '[') finds it.
    if path and not path.startswith(".") and not path.startswith("["):
        path = "." + path

    tokens: list = []
    pos = 0
    while pos < len(path):
        m = _TOKEN_RE.match(path, pos)
        if not m:
            raise JSONPathError(f"invalid path syntax at {path[pos:]!r}")
        if m.group(1) is not None:
            tokens.append(("key", m.group(1)))
        else:
            tokens.append(("index", int(m.group(2))))
        pos = m.end()
    return tokens


def get_path(doc: Any, path: str) -> tuple[bool, Any]:
    """Returns (found, value). `found=False` distinguishes "the path led to
    a real None/null value" from "the path doesn't exist" — both matter
    differently to JSON.GET (a nil reply either way) and to the index
    extractor (a genuinely-null field vs. a missing one)."""
    node = doc
    for kind, tok in _tokenize(path):
        if kind == "key":
            if not isinstance(node, dict) or tok not in node:
                return False, None
            node = node[tok]
        else:
            if not isinstance(node, list) or tok >= len(node) or tok < -len(node):
                return False, None
            node = node[tok]
    return True, node


def set_path(doc: Any, path: str, value: Any) -> Any:
    """Set a value at `path`, auto-vivifying intermediate dicts (not lists).
    Returns the (possibly new) root — call as `doc = set_path(doc, path, value)`."""
    tokens = _tokenize(path)
    if not tokens:
        return value  # setting "$" replaces the whole document

    if doc is None:
        doc = {} if tokens[0][0] == "key" else []
    node = doc
    for i, (kind, tok) in enumerate(tokens):
        last = i == len(tokens) - 1
        if kind == "key":
            if not isinstance(node, dict):
                raise JSONPathError(f"path component {tok!r} expects an object")
            if last:
                node[tok] = value
            else:
                nxt_kind = tokens[i + 1][0]
                if tok not in node or not isinstance(node[tok], (dict, list)):
                    node[tok] = {} if nxt_kind == "key" else []
                node = node[tok]
        else:
            if not isinstance(node, list):
                raise JSONPathError(f"path index [{tok}] expects an array")
            if tok < 0:
                tok += len(node)
            if tok < 0 or tok > len(node):
                raise JSONPathError(f"array index [{tok}] out of range")
            if last:
                if tok == len(node):
                    node.append(value)
                else:
                    node[tok] = value
            else:
                if tok == len(node):
                    nxt_kind = tokens[i + 1][0]
                    node.append({} if nxt_kind == "key" else [])
                node = node[tok]
    return doc


def delete_path(doc: Any, path: str) -> bool:
    tokens = _tokenize(path)
    if not tokens:
        return False  # deleting the whole document isn't a "path delete"; use DEL
    node = doc
    for kind, tok in tokens[:-1]:
        if kind == "key":
            if not isinstance(node, dict) or tok not in node:
                return False
            node = node[tok]
        else:
            if not isinstance(node, list) or tok >= len(node) or tok < -len(node):
                return False
            node = node[tok]
    last_kind, last_tok = tokens[-1]
    if last_kind == "key":
        if isinstance(node, dict) and last_tok in node:
            del node[last_tok]
            return True
        return False
    else:
        if isinstance(node, list) and -len(node) <= last_tok < len(node):
            del node[last_tok]
            return True
        return False
