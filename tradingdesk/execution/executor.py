"""
Executor (plain Python). Places entries for an approved trade, through the
Broker interface only.

It refuses anything that didn't come through a Trade Desk approval: the
candidate must be human_approved, which the Candidate model only allows right
after a passing click-time Risk Officer re-check. Calling it twice for the same
candidate is a no-op (and client order ids are deterministic, so the broker
would refuse a duplicate anyway).

  regular hours  one bracket per leg: stop-limit entry at the breakout level,
                 stop loss and target held by the broker from the start
  premarket      no orders yet: the trade is ARMED and the Trade Manager submits a
                 plain extended-hours limit buy when price crosses the entry
                 (Alpaca can't hold a stop in extended hours: stop is software-managed)
"""
from __future__ import annotations

import math
import threading
from datetime import datetime

from loguru import logger

from tradingdesk.alerts import Notifier
from tradingdesk.broker.base import Broker, BrokerError, EntryRequest
from tradingdesk.execution.trade import Leg, ManagedTrade, TradeState
from tradingdesk.execution.trade_manager import TradeManager
from tradingdesk.journal import Journal
from tradingdesk.models import Agent, Candidate, CandidateStatus
from tradingdesk.sessions import now_et
from tradingdesk.settings import TradeManagerConfig


class NotApproved(RuntimeError):
    pass


def client_id(c: Candidate, role: str) -> str:
    return f"td-{c.id[:20]}-{role}"


class Executor:
    def __init__(self, broker: Broker, manager: TradeManager, cfg: TradeManagerConfig, journal: Journal,
                 notifier: Notifier) -> None:
        self.broker = broker
        self.manager = manager
        self.cfg = cfg
        self.journal = journal
        self.notifier = notifier
        self._lock = threading.Lock()

    @staticmethod
    def _check(c: Candidate) -> None:
        if c.status is not CandidateStatus.HUMAN_APPROVED or c.human is None or c.human.decision != "approved":
            raise NotApproved(f"{c.ticker}: not approved on the Trade Desk (status={c.status})")
        if not any(v.agent is Agent.RISK_RECHECK and v.decision == "pass" for v in c.verdicts):
            raise NotApproved(f"{c.ticker}: no click-time re-check")
        if not c.proposed_trade or not c.risk_sizing or not c.human.approved_shares:
            raise NotApproved(f"{c.ticker}: incomplete ticket")

    def legs_for(self, c: Candidate) -> list[Leg]:
        shares, t = c.human.approved_shares, c.proposed_trade  # type: ignore[union-attr]
        a = math.floor(shares * self.cfg.partial_take_fraction) if shares >= 2 else 0
        legs = []
        if a:
            legs.append(Leg("a", a, t.target))
        legs.append(Leg("b", shares - a, t.target_2 if (a and t.target_2) else t.target))
        return legs

    def execute(self, c: Candidate, now: datetime | None = None) -> ManagedTrade:
        self._check(c)
        now = now or now_et()
        with self._lock:
            existing = self.manager.get(c.id)
            if existing is not None:
                logger.warning("executor: {} already executed, ignoring duplicate", c.ticker)
                return existing
            t = c.proposed_trade
            trade = ManagedTrade(candidate=c, entry=t.entry, stop=t.stop, risk_per_share=t.entry - t.stop,
                                 legs=self.legs_for(c), state=TradeState.ARMED, session_entry=c.session)
            regular = self.broker.clock().is_open
            if regular:
                try:
                    self.place_brackets(trade)
                except BrokerError as exc:
                    c.reject(Agent.BROKER, [f"order rejected: {exc}"])
                    self.journal.save_candidate(c)
                    self.notifier.send(f"⚠️ {c.ticker}: broker rejected the entry: {exc}")
                    raise
            else:
                for leg in trade.legs:
                    leg.software = True
            self.manager.add(trade)
            self.journal.log_event("execute", candidate_id=c.id, ticker=c.ticker, session="regular" if regular else "extended",
                                   legs=[(leg.name, leg.qty, leg.target) for leg in trade.legs], state=trade.state.value)
            self.journal.save_candidate(c)
            return trade

    def place_brackets(self, trade: ManagedTrade) -> None:
        """Regular hours: one broker bracket per leg (stop-limit entry, stop, target)."""
        c = trade.candidate
        limit = trade.entry * (1 + self.cfg.entry_limit_slip_pct / 100)
        for leg in trade.legs:
            o = self.broker.submit_entry(EntryRequest(
                ticker=c.ticker, qty=leg.qty, client_order_id=client_id(c, f"entry-{leg.name}"),
                limit_price=limit, stop_price=trade.entry, stop_loss=trade.stop, take_profit=leg.target,
            ))
            leg.entry_id, leg.software = o.id, False
            trade.roles[o.id] = f"entry:{leg.name}"
            for child in o.legs:
                self._register_leg(trade, leg, child.id, child.type)
        trade.state = TradeState.WORKING
        trade.entry_submitted_at = now_et()

    def place_premarket_entry(self, trade: ManagedTrade, attempt: int) -> None:
        c = trade.candidate
        limit = trade.entry * (1 + self.cfg.entry_limit_slip_pct / 100)
        qty = sum(leg.qty for leg in trade.legs) - trade.held
        o = self.broker.submit_entry(EntryRequest(
            ticker=c.ticker, qty=qty, client_order_id=client_id(c, f"pm-entry-{attempt}"),
            limit_price=limit, extended_hours=True,
        ))
        trade.software_entry_id = o.id
        trade.roles[o.id] = "entry:pm"
        trade.state = TradeState.WORKING
        trade.entry_submitted_at = now_et()
        trade.entry_attempts = attempt

    @staticmethod
    def _register_leg(trade: ManagedTrade, leg: Leg, order_id: str, order_type: str) -> None:
        if order_type in ("stop", "stop_limit"):
            leg.stop_id = order_id
            trade.roles[order_id] = f"stop:{leg.name}"
        else:
            leg.target_id = order_id
            trade.roles[order_id] = f"target:{leg.name}"
