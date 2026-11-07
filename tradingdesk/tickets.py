"""
Tickets awaiting a human decision. Phase 2 keeps them in memory; phase 3's Trade
Desk reads this queue, handles expiry, and records decisions.
"""
from __future__ import annotations

import threading

from tradingdesk.models import Candidate, CandidateStatus


class TicketQueue:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tickets: dict[str, Candidate] = {}

    def add(self, c: Candidate) -> None:
        if c.status is not CandidateStatus.AWAITING_HUMAN:
            raise ValueError(f"only awaiting_human candidates become tickets (got {c.status})")
        with self._lock:
            self._tickets[c.id] = c

    def pending(self) -> list[Candidate]:
        with self._lock:
            return [c for c in self._tickets.values() if c.status is CandidateStatus.AWAITING_HUMAN]

    def pending_tickers(self) -> frozenset[str]:
        return frozenset(c.ticker for c in self.pending())

    def get(self, candidate_id: str) -> Candidate | None:
        with self._lock:
            return self._tickets.get(candidate_id)
