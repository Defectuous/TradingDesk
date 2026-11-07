"""
Account state for the Risk Officer.

Cash and buying power come from a CashSource: Alpaca's paper account (read-only)
live, or a fixed number in tests / `--cash`. Cash is cached for
`risk.cash_refresh_sec`, and the first reading of each trading day is pinned as
the daily-loss basis.

Today's trades, P&L and open positions still come from the Journal. Phase 3's
Broker layer replaces those with real positions and orders, keeping AccountState.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from loguru import logger

from tradingdesk.agents.risk_officer import AccountState
from tradingdesk.journal import Journal
from tradingdesk.models import CandidateStatus
from tradingdesk.tickets import TicketQueue

_TRADED = {CandidateStatus.HUMAN_APPROVED, CandidateStatus.FILLED, CandidateStatus.CLOSED}
_OPEN = {CandidateStatus.HUMAN_APPROVED, CandidateStatus.FILLED}


@dataclass(frozen=True)
class CashReading:
    cash: float
    buying_power: float


class CashSource(Protocol):
    def read(self) -> CashReading: ...


class StaticCash:
    def __init__(self, cash: float, buying_power: float | None = None) -> None:
        self._r = CashReading(cash, cash if buying_power is None else buying_power)

    def read(self) -> CashReading:
        return self._r


class AlpacaCash:
    """Read-only cash/buying power from the Alpaca *paper* account."""

    def __init__(self, api_key: str, secret_key: str) -> None:
        from alpaca.trading.client import TradingClient

        self._client = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)

    def read(self) -> CashReading:
        a = self._client.get_account()
        return CashReading(cash=float(a.cash), buying_power=float(a.buying_power))


class AccountProvider(Protocol):
    def state(self, now: datetime) -> AccountState: ...


class JournalAccountProvider:
    def __init__(self, journal: Journal, tickets: TicketQueue, cash: CashSource, refresh_sec: float = 60) -> None:
        self.journal = journal
        self.tickets = tickets
        self.cash_source = cash
        self.refresh_sec = refresh_sec
        self._lock = threading.Lock()
        self._last: CashReading | None = None
        self._read_at = 0.0
        self._day_start: dict[str, float] = {}

    def _cash(self) -> CashReading:
        with self._lock:
            if self._last is None or time.monotonic() - self._read_at >= self.refresh_sec:
                try:
                    self._last = self.cash_source.read()
                    self._read_at = time.monotonic()
                except Exception as exc:
                    if self._last is None:
                        raise
                    logger.warning("cash refresh failed, using last reading: {}", exc)
            return self._last

    def state(self, now: datetime) -> AccountState:
        day = now.date().isoformat()
        reading = self._cash()
        start = self._day_start.setdefault(day, reading.cash)
        today = self.journal.list_candidates(trade_date=day)
        traded = [c for c in today if c.status in _TRADED]
        open_ = [c for c in today if c.status in _OPEN]
        return AccountState(
            cash=reading.cash,
            buying_power=reading.buying_power,
            day_start_cash=start,
            realized_pnl_today=round(sum(c.realized_pnl for c in traded), 2),
            trades_today=len(traded),
            open_positions=len(open_),
            active_tickers=frozenset(c.ticker for c in open_) | self.tickets.pending_tickers(),
        )
