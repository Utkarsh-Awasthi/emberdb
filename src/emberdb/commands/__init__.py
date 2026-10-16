"""Command registry: @register() populates COMMAND_TABLE; engine.py dispatches from it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, TYPE_CHECKING

from ..protocol import RespError

if TYPE_CHECKING:
    from ..engine import EmberEngine


@dataclass
class CommandContext:
    """Everything a handler needs; never touches sockets/RESP directly."""

    engine: "EmberEngine"
    db_index: int
    username: str
    protocol_version: int
    conn_state: Any  # duck-typed: server.ClientConnection or http_api's stand-in
    is_replay: bool = False  # True while replaying the AOF at startup

    # -- filled in BY the handler, read AFTERWARDS by engine.execute() ----
    written_keys: list[str] = field(default_factory=list)
    propagate_override: Optional[list[str]] = None  # e.g. EXPIRE -> PEXPIREAT rewrite
    flushed: bool = False  # FLUSHDB/FLUSHALL: a write with no discrete key

    @property
    def db(self):
        return self.engine.databases[self.db_index]

    def mark_write(self, key: str) -> None:
        self.written_keys.append(key)


HandlerFn = Callable[[CommandContext, list[str]], Any]


@dataclass
class CommandSpec:
    name: str
    handler: HandlerFn
    arity: int  # see module docstring
    write: bool = False
    admin: bool = False
    no_auth_required: bool = False
    pubsub_context_ok: bool = False  # allowed while a connection is in subscriber mode


COMMAND_TABLE: dict[str, CommandSpec] = {}


def register(name: str, arity: int, write: bool = False, admin: bool = False,
             no_auth_required: bool = False, pubsub_context_ok: bool = False):
    """Register a command handler into COMMAND_TABLE (name stored upper-case)."""

    def deco(fn: HandlerFn) -> HandlerFn:
        COMMAND_TABLE[name.upper()] = CommandSpec(
            name=name.upper(), handler=fn, arity=arity, write=write, admin=admin,
            no_auth_required=no_auth_required, pubsub_context_ok=pubsub_context_ok,
        )
        return fn

    return deco


def check_arity(spec: CommandSpec, argv: list[str]) -> None:
    n = len(argv)
    ok = (n == spec.arity) if spec.arity >= 0 else (n >= -spec.arity)
    if not ok:
        raise RespError(f"wrong number of arguments for '{spec.name.lower()}' command")


# -- argument-parsing helpers shared across many handlers ---------------------

def parse_int(raw: str, what: str = "value") -> int:
    try:
        return int(raw)
    except ValueError:
        raise RespError(f"{what} is not an integer or out of range")


def parse_float(raw: str, what: str = "value") -> float:
    try:
        return float(raw)
    except ValueError:
        raise RespError(f"{what} is not a valid float")


# submodules must import after the names above exist
from . import strings        # noqa: E402,F401
from . import generic        # noqa: E402,F401
from . import lists          # noqa: E402,F401
from . import hashes         # noqa: E402,F401
from . import sets           # noqa: E402,F401
from . import zsets          # noqa: E402,F401
from . import transactions   # noqa: E402,F401
from . import pubsub_cmds    # noqa: E402,F401
from . import server_admin   # noqa: E402,F401
from . import json_docs      # noqa: E402,F401
from . import query_ext      # noqa: E402,F401
