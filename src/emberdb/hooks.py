"""TTL expiry webhooks: fire-and-forget HTTP POST when a key expires."""

from __future__ import annotations

import fnmatch
import json
import logging
import threading
import time
from dataclasses import dataclass
from urllib import request as urllib_request
from urllib.error import URLError

logger = logging.getLogger("emberdb.hooks")


@dataclass
class HookDef:
    name: str
    db_index: int
    pattern: str
    url: str
    event: str = "expired"  # the only event type wired up today; see module docstring


class HookManager:
    def __init__(self, http_timeout_seconds: float = 3.0):
        self.hooks: dict[str, HookDef] = {}
        self.http_timeout_seconds = http_timeout_seconds
        self.fired_count = 0
        self.failed_count = 0

    def register(self, name: str, db_index: int, pattern: str, url: str, event: str = "expired") -> None:
        self.hooks[name] = HookDef(name=name, db_index=db_index, pattern=pattern, url=url, event=event)

    def unregister(self, name: str) -> bool:
        return self.hooks.pop(name, None) is not None

    def list_hooks(self) -> list[dict]:
        return [
            {"name": h.name, "db": h.db_index, "pattern": h.pattern, "url": h.url, "event": h.event}
            for h in self.hooks.values()
        ]

    def on_expired(self, db_index: int, key: str) -> None:
        for hook in self.hooks.values():
            if hook.event == "expired" and hook.db_index == db_index and fnmatch.fnmatchcase(key, hook.pattern):
                self._fire(hook, key)

    def _fire(self, hook: HookDef, key: str) -> None:
        payload = json.dumps({
            "event": hook.event, "db": hook.db_index, "key": key, "ts": time.time(),
        }).encode("utf-8")

        def _send() -> None:
            try:
                req = urllib_request.Request(
                    hook.url, data=payload,
                    headers={"Content-Type": "application/json", "User-Agent": "EmberDB-Hook/1.0"},
                    method="POST",
                )
                urllib_request.urlopen(req, timeout=self.http_timeout_seconds)
                self.fired_count += 1  # best-effort counter; see module docstring
            except (URLError, OSError, ValueError):
                self.failed_count += 1
                logger.warning("hook %r failed to call %s for key %r", hook.name, hook.url, key)

        threading.Thread(target=_send, daemon=True, name=f"hook-{hook.name}").start()
