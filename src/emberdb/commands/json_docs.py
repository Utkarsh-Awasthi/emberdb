"""JSON.* document commands."""

from __future__ import annotations

import json as json_module

from ..jsonpath import JSONPathError, delete_path, get_path, set_path
from ..protocol import RespError, SimpleString
from ..valuetypes import JSONValue, expect_type
from . import CommandContext, register


def _get_doc(ctx: CommandContext, key: str) -> JSONValue | None:
    return expect_type(ctx.db.get(key), JSONValue)


def _parse_json_arg(raw: str) -> object:
    try:
        return json_module.loads(raw)
    except json_module.JSONDecodeError as e:
        raise RespError(f"invalid JSON value: {e}")


@register("JSON.SET", arity=-4, write=True)
def cmd_json_set(ctx: CommandContext, argv: list[str]) -> object:
    key, path, raw_value = argv[1], argv[2], argv[3]
    nx = "NX" in [a.upper() for a in argv[4:]]
    xx = "XX" in [a.upper() for a in argv[4:]]
    if nx and xx:
        raise RespError("syntax error")

    new_value = _parse_json_arg(raw_value)
    existing = _get_doc(ctx, key)

    if path == "$":
        if existing is not None and nx:
            return None
        if existing is None and xx:
            return None
        ctx.db.set_value(key, JSONValue(new_value), keep_ttl=True)
        ctx.mark_write(key)
        return SimpleString("OK")

    if existing is None:
        raise RespError("new objects must be created at the root ($ path)")
    found, _ = get_path(existing.doc, path)
    if found and nx:
        return None
    if not found and xx:
        return None
    try:
        new_doc = set_path(existing.doc, path, new_value)
    except JSONPathError as e:
        raise RespError(str(e))
    ctx.db.set_value(key, JSONValue(new_doc), keep_ttl=True)
    ctx.mark_write(key)
    return SimpleString("OK")


@register("JSON.GET", arity=-2)
def cmd_json_get(ctx: CommandContext, argv: list[str]) -> object:
    doc = _get_doc(ctx, argv[1])
    if doc is None:
        return None
    path = argv[2] if len(argv) > 2 else "$"
    found, value = get_path(doc.doc, path)
    if not found:
        return None
    return json_module.dumps(value)


@register("JSON.DEL", arity=-2, write=True)
def cmd_json_del(ctx: CommandContext, argv: list[str]) -> object:
    key = argv[1]
    path = argv[2] if len(argv) > 2 else "$"
    doc = _get_doc(ctx, key)
    if doc is None:
        return 0
    if path == "$":
        ctx.db.delete(key)
        ctx.mark_write(key)
        return 1
    removed = delete_path(doc.doc, path)
    if removed:
        ctx.db.touch_version(key)
        ctx.mark_write(key)
        return 1
    return 0


@register("JSON.TYPE", arity=-2)
def cmd_json_type(ctx: CommandContext, argv: list[str]) -> object:
    doc = _get_doc(ctx, argv[1])
    if doc is None:
        return None
    path = argv[2] if len(argv) > 2 else "$"
    found, value = get_path(doc.doc, path)
    if not found:
        return None
    return _json_type_name(value)


def _json_type_name(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


@register("JSON.NUMINCRBY", arity=4, write=True)
def cmd_json_numincrby(ctx: CommandContext, argv: list[str]) -> object:
    key, path = argv[1], argv[2]
    delta = _parse_json_arg(argv[3])
    if isinstance(delta, bool) or not isinstance(delta, (int, float)):
        raise RespError("increment must be a JSON number")
    doc = _get_doc(ctx, key)
    if doc is None:
        raise RespError("no such key")
    found, current = get_path(doc.doc, path)
    if not found or not isinstance(current, (int, float)) or isinstance(current, bool):
        raise RespError("path does not point to a number")
    # int + int stays int (so incrementing 10 by 5 gives 15, not 15.0) —
    # only promotes to float when either operand actually is one, mirroring
    # how JSON itself distinguishes `5` from `5.0`.
    new_value = current + delta
    new_doc = set_path(doc.doc, path, new_value)
    ctx.db.set_value(key, JSONValue(new_doc), keep_ttl=True)
    ctx.mark_write(key)
    return json_module.dumps(new_value)


def _length_of(ctx: CommandContext, argv: list[str], expect_py_type: type, what: str) -> object:
    doc = _get_doc(ctx, argv[1])
    if doc is None:
        return None
    path = argv[2] if len(argv) > 2 else "$"
    found, value = get_path(doc.doc, path)
    if not found:
        return None
    if not isinstance(value, expect_py_type):
        raise RespError(f"path does not point to {what}")
    return len(value)


@register("JSON.STRLEN", arity=-2)
def cmd_json_strlen(ctx: CommandContext, argv: list[str]) -> object:
    return _length_of(ctx, argv, str, "a string")


@register("JSON.ARRLEN", arity=-2)
def cmd_json_arrlen(ctx: CommandContext, argv: list[str]) -> object:
    return _length_of(ctx, argv, list, "an array")


@register("JSON.OBJLEN", arity=-2)
def cmd_json_objlen(ctx: CommandContext, argv: list[str]) -> object:
    return _length_of(ctx, argv, dict, "an object")


@register("JSON.ARRAPPEND", arity=-4, write=True)
def cmd_json_arrappend(ctx: CommandContext, argv: list[str]) -> object:
    key, path = argv[1], argv[2]
    doc = _get_doc(ctx, key)
    if doc is None:
        raise RespError("no such key")
    found, arr = get_path(doc.doc, path)
    if not found or not isinstance(arr, list):
        raise RespError("path does not point to an array")
    for raw in argv[3:]:
        arr.append(_parse_json_arg(raw))
    new_doc = set_path(doc.doc, path, arr)
    ctx.db.set_value(key, JSONValue(new_doc), keep_ttl=True)
    ctx.mark_write(key)
    return len(arr)


@register("JSON.CLEAR", arity=-2, write=True)
def cmd_json_clear(ctx: CommandContext, argv: list[str]) -> object:
    key = argv[1]
    path = argv[2] if len(argv) > 2 else "$"
    doc = _get_doc(ctx, key)
    if doc is None:
        return 0
    found, value = get_path(doc.doc, path)
    if not found:
        return 0
    if isinstance(value, list):
        cleared = set_path(doc.doc, path, [])
    elif isinstance(value, dict):
        cleared = set_path(doc.doc, path, {})
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        cleared = set_path(doc.doc, path, 0)
    else:
        return 0
    ctx.db.set_value(key, JSONValue(cleared), keep_ttl=True)
    ctx.mark_write(key)
    return 1
