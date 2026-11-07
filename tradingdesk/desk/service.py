"""
Trade Desk logic. The NiceGUI screen (phase 3b) only displays this and calls it.

Rules enforced here, not in the UI:
- approve() re-runs the Risk Officer against the live quote, account and daily
  limits before anything is sent; if it fails, nothing trades and the reason is
  returned
- approve() is idempotent per ticket: a second call (double click, two browser
  tabs) returns the first result and never sends a second order
- size can only be reduced; the stop can't be edited
- tickets expire after desk.ticket_ttl_sec, or immediately when price moves
  more than desk.max_entry_drift_pct from the proposed entry; expired tickets
  can't be approved
- approvals are disabled for the rest of the day once the daily loss limit is hit
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime

from loguru import logger

from tradingdesk.account import AccountProvider
from tradingdesk.agents.risk_officer import RiskOfficer
from tradingdesk.alerts import Notifier, ticket_message
from tradingdesk.execution.executor import Executor
from tradingdesk.execution.trade_manager import TradeManager
from tradingdesk.journal import Journal
from tradingdesk.market import MarketData
from tradingdesk.models import Agent, Candidate, CandidateStatus, HumanDecision
from tradingdesk.sessions import now_et
from tradingdesk.settings import DeskConfig
from tradingdesk.tickets import TicketQueue

S = CandidateStatus


@dataclass(frozen=True)
class DeskResult:
    ok: bool
    message: str
    candidate_id: str
    status: str


class DeskService:
    def __init__(self, cfg: DeskConfig, tickets: TicketQueue, risk: RiskOfficer, accounts: AccountProvider,
                 market: MarketData, executor: Executor, manager: TradeManager, journal: Journal,
                 notifier: Notifier) -> None:
        self.cfg = cfg
        self.tickets = tickets
        self.risk = risk
        self.accounts = accounts
        self.market = market
        self.executor = executor
        self.manager = manager
        self.journal = journal
        self.notifier = notifier
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._results: dict[tuple[str, str], DeskResult] = {}   # (action, ticket id) -> first result

    @property
    def url(self) -> str:
        return f"http://{self.cfg.host}:{self.cfg.port}"

    def _lock_for(self, cid: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(cid, threading.Lock())

    # -- new tickets ---------------------------------------------------------------
    def on_new_ticket(self, c: Candidate) -> None:
        self.notifier.send(ticket_message(c, self.url))

    def seconds_left(self, c: Candidate, now: datetime | None = None) -> float:
        shown = self.tickets.shown_at(c.id)
        if shown is None:
            return 0.0
        return max(0.0, self.cfg.ticket_ttl_sec - ((now or now_et()) - shown).total_seconds())

    # -- expiry -------------------------------------------------------------------------
    def expire_stale(self, now: datetime | None = None) -> list[Candidate]:
        now = now or now_et()
        expired = []
        for c in self.tickets.pending():
            why = self._expiry_reason(c, now)
            if why:
                with self._lock_for(c.id):
                    if c.status is S.AWAITING_HUMAN:
                        self._expire(c, now, why)
                        expired.append(c)
        return expired

    def _expiry_reason(self, c: Candidate, now: datetime) -> str | None:
        if self.seconds_left(c, now) <= 0:
            return f"no decision within {self.cfg.ticket_ttl_sec:g}s"
        q = self.market.quote(c.ticker)
        if q and c.proposed_trade:
            drift = abs(q.price - c.proposed_trade.entry) / c.proposed_trade.entry * 100
            if drift > self.cfg.max_entry_drift_pct:
                return f"price {q.price} moved {drift:.1f}% from entry {c.proposed_trade.entry}"
        return None

    def _expire(self, c: Candidate, now: datetime, why: str) -> None:
        shown = self.tickets.shown_at(c.id) or now
        c.record_human(HumanDecision(decision="expired", shown_at=shown, decided_at=max(now, shown), note=why[:200]))
        self.journal.save_candidate(c)
        logger.info("ticket {} expired: {}", c.ticker, why)

    # -- decisions --------------------------------------------------------------------------
    def approvals_enabled(self, now: datetime | None = None) -> bool:
        return not self.risk.daily_loss_hit(self.accounts.state(now or now_et()))

    def approve(self, candidate_id: str, shares: int | None = None, note: str | None = None,
                now: datetime | None = None) -> DeskResult:
        now = now or now_et()
        with self._lock_for(candidate_id):
            if ("approve", candidate_id) in self._results:   # idempotent: same answer, no second order
                return self._results[("approve", candidate_id)]
            c = self.tickets.get(candidate_id)
            if c is None:
                return DeskResult(False, "no such ticket", candidate_id, "unknown")
            if c.status is not S.AWAITING_HUMAN:
                return DeskResult(False, f"ticket is {c.status.value}", candidate_id, c.status.value)
            why = self._expiry_reason(c, now)
            if why:
                self._expire(c, now, why)
                return self._done("approve", c, False, f"expired: {why}")
            account = self.accounts.state(now)
            if self.risk.daily_loss_hit(account):
                return DeskResult(False, "daily loss limit hit: approvals disabled for today", c.id, c.status.value)
            sized = c.risk_sizing.shares if c.risk_sizing else 0
            requested = sized if shares is None else shares
            if requested > sized:
                return DeskResult(False, f"size can only be reduced (max {sized})", c.id, c.status.value)

            # click-time re-check: live quote, account and limits
            decision = self.risk.evaluate(c, account, self.market.quote(c.ticker), now, requested_shares=requested)
            if not decision.ok:
                c.reject(Agent.RISK_RECHECK, decision.reasons, **decision.checks)
                self.journal.save_candidate(c)
                return self._done("approve", c, False, "re-check failed: " + "; ".join(decision.reasons))
            c.record_pass(Agent.RISK_RECHECK, [f"{decision.sizing.shares} sh ok at click time"], **decision.checks)
            shown = self.tickets.shown_at(c.id) or now
            c.record_human(HumanDecision(decision="approved", shown_at=shown, decided_at=max(now, shown),
                                         approved_shares=decision.sizing.shares, note=note))
            self.journal.save_candidate(c)
            try:
                self.executor.execute(c, now)
            except Exception as exc:
                logger.exception("execute failed for {}", c.ticker)
                return self._done("approve", c, False, f"order failed: {exc}")
            soft = " (stop is software-managed until 9:30)" if c.session == "premarket" else ""
            return self._done("approve", c, True, f"approved {decision.sizing.shares} {c.ticker}{soft}")

    def pass_ticket(self, candidate_id: str, note: str | None = None, now: datetime | None = None) -> DeskResult:
        now = now or now_et()
        with self._lock_for(candidate_id):
            if ("pass", candidate_id) in self._results:
                return self._results[("pass", candidate_id)]
            c = self.tickets.get(candidate_id)
            if c is None or c.status is not S.AWAITING_HUMAN:
                return DeskResult(False, "ticket not waiting", candidate_id, c.status.value if c else "unknown")
            shown = self.tickets.shown_at(c.id) or now
            c.record_human(HumanDecision(decision="passed", shown_at=shown, decided_at=max(now, shown),
                                         note=(note or None) and note[:200]))
            self.journal.save_candidate(c)
            return self._done("pass", c, True, "passed")

    def _done(self, action: str, c: Candidate, ok: bool, msg: str) -> DeskResult:
        r = DeskResult(ok, msg, c.id, c.status.value)
        self._results[(action, c.id)] = r
        return r

    # -- positions --------------------------------------------------------------------------
    def flatten(self, ticker: str) -> str:
        self.manager.flatten(ticker, "flatten")
        pos = self.manager.broker.position(ticker)
        return "flat" if pos is None or pos.qty <= 0 else f"still open: {pos.qty} sh"

    def kill_switch(self, now: datetime | None = None) -> dict[str, str]:
        now = now or now_et()
        for c in self.tickets.pending():                 # nothing new gets approved mid-kill
            with self._lock_for(c.id):
                if c.status is S.AWAITING_HUMAN:
                    self._expire(c, now, "kill switch")
        return self.manager.kill_switch(now)
