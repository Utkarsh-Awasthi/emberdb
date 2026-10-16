"""Channel + pattern pub/sub, transport-agnostic via the Subscriber protocol."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Protocol


class Subscriber(Protocol):
    def deliver(self, channel: str, message: str) -> None:
        ...


@dataclass
class _SubscriberState:
    channels: set[str] = field(default_factory=set)
    patterns: set[str] = field(default_factory=set)


class PubSubBroker:
    def __init__(self):
        self._channel_subs: dict[str, set[Subscriber]] = {}
        self._pattern_subs: dict[str, set[Subscriber]] = {}
        self._state: dict[int, _SubscriberState] = {}  # id(subscriber) -> state

    def _state_for(self, sub: Subscriber) -> _SubscriberState:
        key = id(sub)
        if key not in self._state:
            self._state[key] = _SubscriberState()
        return self._state[key]

    def subscribe(self, sub: Subscriber, channel: str) -> int:
        self._channel_subs.setdefault(channel, set()).add(sub)
        st = self._state_for(sub)
        st.channels.add(channel)
        return len(st.channels) + len(st.patterns)

    def unsubscribe(self, sub: Subscriber, channel: str) -> int:
        subs = self._channel_subs.get(channel)
        if subs is not None:
            subs.discard(sub)
            if not subs:
                del self._channel_subs[channel]
        st = self._state_for(sub)
        st.channels.discard(channel)
        return len(st.channels) + len(st.patterns)

    def psubscribe(self, sub: Subscriber, pattern: str) -> int:
        self._pattern_subs.setdefault(pattern, set()).add(sub)
        st = self._state_for(sub)
        st.patterns.add(pattern)
        return len(st.channels) + len(st.patterns)

    def punsubscribe(self, sub: Subscriber, pattern: str) -> int:
        subs = self._pattern_subs.get(pattern)
        if subs is not None:
            subs.discard(sub)
            if not subs:
                del self._pattern_subs[pattern]
        st = self._state_for(sub)
        st.patterns.discard(pattern)
        return len(st.channels) + len(st.patterns)

    def unsubscribe_all(self, sub: Subscriber) -> None:
        """Called on disconnect — removes a subscriber from every channel/pattern."""
        st = self._state.pop(id(sub), None)
        if st is None:
            return
        for ch in list(st.channels):
            subs = self._channel_subs.get(ch)
            if subs is not None:
                subs.discard(sub)
                if not subs:
                    del self._channel_subs[ch]
        for pat in list(st.patterns):
            subs = self._pattern_subs.get(pat)
            if subs is not None:
                subs.discard(sub)
                if not subs:
                    del self._pattern_subs[pat]

    def publish(self, channel: str, message: str) -> int:
        receivers = 0
        for sub in self._channel_subs.get(channel, ()):
            sub.deliver(channel, message)
            receivers += 1
        for pattern, subs in self._pattern_subs.items():
            if fnmatch.fnmatchcase(channel, pattern):
                for sub in subs:
                    sub.deliver(channel, message)
                    receivers += 1
        return receivers

    # -- introspection (PUBSUB CHANNELS / NUMSUB / NUMPAT, dashboard) ------

    def channels(self, pattern: str = "*") -> list[str]:
        return [c for c in self._channel_subs if fnmatch.fnmatchcase(c, pattern)]

    def numsub(self, channel: str) -> int:
        return len(self._channel_subs.get(channel, ()))

    def numpat(self) -> int:
        return len(self._pattern_subs)
