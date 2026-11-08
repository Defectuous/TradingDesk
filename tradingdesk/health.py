"""
Heartbeats from the loops and feeds, for the Trade Desk status bar.

Each component reports ok(name) after a successful pass or error(name, why) when
one fails. The desk decides what "stale" means for each name.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime

from tradingdesk.sessions import now_et


@dataclass(frozen=True)
class Beat:
    at: datetime
    ok: bool
    detail: str = ""
    last_ok: datetime | None = None


class Health:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._beats: dict[str, Beat] = {}

    def ok(self, name: str, detail: str = "", now: datetime | None = None) -> None:
        now = now or now_et()
        with self._lock:
            self._beats[name] = Beat(now, True, detail, now)

    def error(self, name: str, detail: str, now: datetime | None = None) -> None:
        now = now or now_et()
        with self._lock:
            prev = self._beats.get(name)
            self._beats[name] = Beat(now, False, detail[:200], prev.last_ok if prev else None)

    def get(self, name: str) -> Beat | None:
        with self._lock:
            return self._beats.get(name)
