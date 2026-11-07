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


def _deployed(c) -> float:
    """$ committed to buys for a traded candidate: actual buy fills, else the approved order."""
    buys = sum(f.qty * f.price for f in c.fills if f.side == "buy")
    if buys:
        return buys
    shares = (c.human.approved_shares if c.human and c.human.approved_shares else 0)
    return shares * (c.proposed_trade.entry if c.proposed_trade else 0)


class BrokerCash:
    """Cash from whatever Broker is configured (paper Alpaca or the dry-run simulator)."""

    def __init__(self, broker) -> None:
        self.broker = broker

    def read(self) -> CashReading:
        a = self.broker.account()
        return CashReading(cash=a.cash, buying_power=a.buying_power)


class AccountProvider(Protocol):
    def state(self, now: datetime) -> AccountState: ...


class JournalAccountProvider:
    def __init__(self, journal: Journal, tickets: TicketQueue, cash: CashSource, refresh_sec: float = 60,
                 broker=None) -> None:
        self.broker = broker                 # when set, real broker positions count too
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
        deployed = sum(_deployed(c) for c in traded)
        open_ = [c for c in today if c.status in _OPEN]
        held: set[str] = set()
        open_pnl = 0.0
        if self.broker is not None:
            try:
                for p in self.broker.positions():
                    if p.qty > 0:
                        held.add(p.ticker)
                        open_pnl += p.unrealized_pl
            except Exception as exc:
                logger.warning("positions unavailable for risk check: {}", exc)
        return AccountState(
            cash=reading.cash,
            buying_power=reading.buying_power,
            day_start_cash=start,
            realized_pnl_today=round(sum(c.realized_pnl for c in traded), 2),
            trades_today=len(traded),
            deployed_today=round(deployed, 2),
            open_pnl=round(open_pnl, 2),
            open_positions=max(len(open_), len(held)),
            active_tickers=frozenset(c.ticker for c in open_) | held | self.tickets.pending_tickers(),
        )
