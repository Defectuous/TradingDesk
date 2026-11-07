"""
Account state for the Risk Officer.

Phase 2 derives today's trades and P&L from the Journal, since there's no broker
layer yet. Phase 3 replaces this with the Broker interface (real buying power,
positions, open orders), keeping the same AccountState shape.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from tradingdesk.agents.risk_officer import AccountState
from tradingdesk.journal import Journal
from tradingdesk.models import CandidateStatus
from tradingdesk.tickets import TicketQueue

_TRADED = {CandidateStatus.HUMAN_APPROVED, CandidateStatus.FILLED, CandidateStatus.CLOSED}
_OPEN = {CandidateStatus.HUMAN_APPROVED, CandidateStatus.FILLED}


class AccountProvider(Protocol):
    def state(self, now: datetime) -> AccountState: ...


class JournalAccountProvider:
    def __init__(self, journal: Journal, tickets: TicketQueue, buying_power: float) -> None:
        self.journal = journal
        self.tickets = tickets
        self.buying_power = buying_power

    def state(self, now: datetime) -> AccountState:
        today = [c for c in self.journal.list_candidates(trade_date=now.date().isoformat())]
        traded = [c for c in today if c.status in _TRADED]
        open_ = [c for c in today if c.status in _OPEN]
        return AccountState(
            buying_power=self.buying_power,
            realized_pnl_today=round(sum(c.realized_pnl for c in traded), 2),
            trades_today=len(traded),
            open_positions=len(open_),
            active_tickers=frozenset(c.ticker for c in open_) | self.tickets.pending_tickers(),
        )
