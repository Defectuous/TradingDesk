"""
Manager (plain Python state machine, no LLM).

  spotted --attach cached research, plan breakout--> researched
          --Skeptic--> challenged --Risk Officer--> risk_cleared --> awaiting_human (ticket)

Any stage saying no ends the chain. Every candidate is journaled either way.
The Manager never calls the Executor; only a Trade Desk approval can (phase 3).
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from loguru import logger

from tradingdesk.account import AccountProvider
from tradingdesk.agents.breakout import plan_breakout
from tradingdesk.agents.risk_officer import RiskOfficer
from tradingdesk.agents.scout import ScoutHit
from tradingdesk.agents.skeptic import Skeptic
from tradingdesk.journal import Journal
from tradingdesk.market import MarketData
from tradingdesk.models import ALLOWED_TRANSITIONS, Agent, Candidate, CandidateStatus, Verdict
from tradingdesk.research_cache import ResearchCache
from tradingdesk.settings import ManagerConfig
from tradingdesk.tickets import TicketQueue

S = CandidateStatus


class Manager:
    def __init__(
        self,
        cfg: ManagerConfig,
        cache: ResearchCache,
        skeptic: Skeptic,
        risk_officer: RiskOfficer,
        accounts: AccountProvider,
        market: MarketData,
        journal: Journal,
        tickets: TicketQueue,
    ) -> None:
        self.cfg = cfg
        self.cache = cache
        self.skeptic = skeptic
        self.risk = risk_officer
        self.accounts = accounts
        self.market = market
        self.journal = journal
        self.tickets = tickets
        self.on_ticket: Callable[[Candidate], None] | None = None   # Trade Desk alert hook

    def process(self, hit: ScoutHit, now: datetime) -> Candidate:
        c = hit.candidate
        try:
            self._run(c, hit, now)
        except Exception as exc:
            logger.exception("manager: {} failed", c.ticker)
            if not c.is_terminal and S.REJECTED in ALLOWED_TRANSITIONS[c.status]:
                c.reject(Agent.MANAGER, [f"internal error: {type(exc).__name__}: {exc}"])
        finally:
            self.journal.save_candidate(c)
        self._log(c)
        return c

    # ------------------------------------------------------------------------------
    def _run(self, c: Candidate, hit: ScoutHit, now: datetime) -> None:
        # 1. research from the background cache (never a fresh LLM call)
        news = self.cache.news(c.ticker, now)
        sentiment = self.cache.sentiment(c.ticker, now)
        if news:
            c.attach("news", news)
        if sentiment:
            c.attach("sentiment", sentiment)
        if news and self.cfg.reject_no_catalyst and news.catalyst_grade == "F" and news.catalyst_type.lower() == "none":
            c.reject(Agent.MANAGER, ["no identifiable news catalyst (Newsie grade F, type None)"])
            return

        # 2. plan the one breakout setup
        plan = plan_breakout(c.session, c.scout.price, hit.premarket_levels, hit.session_levels, self.cfg)
        if isinstance(plan, str):
            c.reject(Agent.MANAGER, [f"no setup: {plan}"])
            return
        c.attach("proposed_trade", plan)
        notes = [plan.strategy, f"R:R {plan.risk_reward} at proposed entry"]
        if news is None:
            notes.append("catalyst pending (no Newsie grade yet)")
        c.record_pass(Agent.MANAGER, notes)
        c.transition(S.RESEARCHED, by=Agent.MANAGER)

        # 3. Skeptic: gets a copy, returns only pass/reject
        try:
            verdict = self.skeptic.review(c.model_copy(deep=True))
            if not isinstance(verdict, Verdict) or verdict.agent is not Agent.SKEPTIC:
                raise TypeError(f"Skeptic returned {verdict!r}")
        except Exception as exc:
            c.reject(Agent.SKEPTIC, [f"Skeptic unavailable, default reject ({type(exc).__name__}: {exc})"])
            return
        if verdict.decision == "reject":
            c.reject(Agent.SKEPTIC, list(verdict.reasons) or ["Skeptic rejected"], **verdict.details)
            return
        c.add_verdict(verdict)
        c.transition(S.CHALLENGED, by=Agent.SKEPTIC)

        # 4. Risk Officer: final veto and share size, against the live quote
        decision = self.risk.evaluate(c, self.accounts.state(now), self.market.quote(c.ticker), now)
        if not decision.ok:
            c.reject(Agent.RISK_OFFICER, decision.reasons, **decision.checks)
            return
        c.attach("risk_sizing", decision.sizing)
        c.record_pass(Agent.RISK_OFFICER, [
            f"{decision.sizing.shares} sh, ${decision.sizing.dollars_at_risk} at risk, "
            f"R:R {decision.checks.get('risk_reward')} from current price"
        ], **decision.checks)
        c.transition(S.RISK_CLEARED, by=Agent.RISK_OFFICER)

        # 5. ticket for the human
        c.transition(S.AWAITING_HUMAN, by=Agent.MANAGER)
        self.tickets.add(c, now)
        if self.on_ticket:
            try:
                self.on_ticket(c)
            except Exception:
                logger.exception("ticket alert failed for {}", c.ticker)

    @staticmethod
    def _log(c: Candidate) -> None:
        if c.status is S.AWAITING_HUMAN:
            t, r = c.proposed_trade, c.risk_sizing
            logger.success(
                "TICKET {} [{}] entry {} stop {} target {} | {} sh, ${} risk{}",
                c.ticker, c.session.upper(), t.entry, t.stop, t.target, r.shares, r.dollars_at_risk,
                " | stop is software-managed" if r.software_managed_stop else "",
            )
        elif c.status is S.REJECTED:
            logger.info("rejected {} by {}: {}", c.ticker, c.rejected_by, "; ".join(c.rejection_reasons))

