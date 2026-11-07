"""
Tickets awaiting a human decision, with the time each was shown (for expiry and
decision-time stats). The Trade Desk reads this queue.
"""
from __future__ import annotations

import threading
from datetime import datetime

from tradingdesk.models import Candidate, CandidateStatus
from tradingdesk.sessions import now_et


class TicketQueue:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tickets: dict[str, Candidate] = {}
        self._shown: dict[str, datetime] = {}

    def add(self, c: Candidate, shown_at: datetime | None = None) -> None:
        if c.status is not CandidateStatus.AWAITING_HUMAN:
            raise ValueError(f"only awaiting_human candidates become tickets (got {c.status})")
        with self._lock:
            self._tickets[c.id] = c
            self._shown[c.id] = shown_at or now_et()

    def pending(self) -> list[Candidate]:
        with self._lock:
            return [c for c in self._tickets.values() if c.status is CandidateStatus.AWAITING_HUMAN]

    def all(self) -> list[Candidate]:
        with self._lock:
            return list(self._tickets.values())

    def pending_tickers(self) -> frozenset[str]:
        return frozenset(c.ticker for c in self.pending())

    def get(self, candidate_id: str) -> Candidate | None:
        with self._lock:
            return self._tickets.get(candidate_id)

    def shown_at(self, candidate_id: str) -> datetime | None:
        with self._lock:
            return self._shown.get(candidate_id)
