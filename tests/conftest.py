"""
conftest.py — shared fixtures for the whole EmberDB test suite.

Every test gets a brand-new `EmberEngine` (the `engine` fixture) rooted in
pytest's own `tmp_path`, so tests never share state or files with each
other and never touch the real filesystem outside pytest's managed temp
directory. Most tests dispatch commands the same way a real client would
— through `engine.dispatch_from_client()` — rather than calling into
individual commands/*.py handlers directly, so what's being tested is the
same code path a RESP connection actually uses.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from emberdb.engine import EmberEngine, InternalConnState


class FakeConn(InternalConnState):
    """A stand-in connection for tests. InternalConnState's deliver() /
    deliver_cdc_event() are no-ops by design (they're meant for AOF-replay
    and replication contexts with no real socket) — this subclass
    captures what would have been pushed to a real client instead, so
    pub/sub and CDC.TAIL behavior can be asserted on directly.
    """

    def __init__(self):
        super().__init__()
        self.messages: list[tuple[str, str]] = []
        self.cdc_events: list[dict] = []

    def deliver(self, channel: str, message: str) -> None:
        self.messages.append((channel, message))

    def deliver_cdc_event(self, event: dict) -> None:
        self.cdc_events.append(event)


@pytest.fixture
def engine(tmp_path):
    cfg = {
        "databases": 4,
        "appendonly": True,
        "snapshot_path": str(tmp_path / "snap.json"),
        "aof_path": str(tmp_path / "test.aof.jsonl"),
        "cdc_path": str(tmp_path / "test.cdc.jsonl"),
    }
    eng = EmberEngine(cfg)
    yield eng
    eng.shutdown()


@pytest.fixture
def conn():
    return FakeConn()


@pytest.fixture
def d(engine):
    """Shorthand fixture: `d(conn, "SET", "k", "v")` dispatches through
    the engine exactly as a real client connection would, and returns the
    reply."""
    def _dispatch(connection, *argv):
        return engine.dispatch_from_client(connection, list(argv))
    return _dispatch
