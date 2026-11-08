"""
Tickers the background agents (Newsie, Sentiment) research: the Scout's latest
movers first (biggest first), then recent ones; a ticker drops off after it
hasn't been a mover for `watch_minutes`.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta


class Watchlist:
    def __init__(self, watch_minutes: float, max_watch: int) -> None:
        self.watch_minutes = watch_minutes
        self.max_watch = max_watch
        self._lock = threading.Lock()
        self._seen: dict[str, datetime] = {}
        self._priority: list[str] = []

    def add(self, tickers: list[str], now: datetime) -> list[str]:
        """Record the latest movers; returns the tickers that are new to the list."""
        with self._lock:
            new = [t for t in tickers if t not in self._seen]
            for t in tickers:
                self._seen[t] = now
            self._priority = list(dict.fromkeys(tickers))
        return new

    def current(self, now: datetime) -> list[str]:
        cutoff = now - timedelta(minutes=self.watch_minutes)
        with self._lock:
            for t in [t for t, seen in self._seen.items() if seen < cutoff]:
                del self._seen[t]
            rest = sorted((t for t in self._seen if t not in self._priority), key=lambda t: self._seen[t],
                          reverse=True)
            order = [t for t in self._priority if t in self._seen] + rest
        return order[: self.max_watch]
