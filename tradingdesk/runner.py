"""
Phase 2 pipeline loop: Scout -> Manager (stub Skeptic -> Risk Officer) -> tickets.

Tickets stop at awaiting_human. No orders exist yet (the broker layer is phase 3),
so nothing can reach the broker from here.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from loguru import logger

from tradingdesk.account import AlpacaCash, CashSource, JournalAccountProvider
from tradingdesk.agents.manager import Manager
from tradingdesk.agents.risk_officer import RiskOfficer
from tradingdesk.agents.scout import Scout
from tradingdesk.agents.skeptic import StubSkeptic
from tradingdesk.journal import Journal
from tradingdesk.legacy.wtbot import config as wtbot_config
from tradingdesk.market import MarketData, WtbotMarketData
from tradingdesk.research_cache import ResearchCache
from tradingdesk.sessions import Phase, SessionClock, TradingSession, now_et
from tradingdesk.settings import Settings
from tradingdesk.tickets import TicketQueue


@dataclass
class Pipeline:
    clock: SessionClock
    scout: Scout
    manager: Manager
    tickets: TicketQueue
    journal: Journal


def build_pipeline(
    settings: Settings,
    journal: Journal,
    market: MarketData | None = None,
    force_session: TradingSession | None = None,
    cash: CashSource | None = None,
) -> Pipeline:
    wtbot_config.apply_settings(settings)
    clock = SessionClock(settings.config.sessions)
    if force_session:
        clock = clock.force(force_session)
    market = market or WtbotMarketData()
    cache = ResearchCache()
    tickets = TicketQueue()
    cash = cash or AlpacaCash(settings.secrets.alpaca_api_key, settings.secrets.alpaca_secret_key)
    scout = Scout(settings, market, cache, clock)
    manager = Manager(
        settings.config.manager, cache, StubSkeptic(), RiskOfficer(settings.config.risk, clock),
        JournalAccountProvider(journal, tickets, cash, settings.config.risk.cash_refresh_sec), market, journal, tickets,
    )
    return Pipeline(clock, scout, manager, tickets, journal)


def run_once(p: Pipeline) -> int:
    now = now_et()
    hits = p.scout.scan(now)
    for hit in hits:
        p.manager.process(hit, now)
    return len(hits)


def run_loop(p: Pipeline, poll_sec: float, once: bool = False) -> None:
    while True:
        now = now_et()
        phase = p.clock.phase_at(now)
        if p.clock.accepting_new_trades(now):
            try:
                run_once(p)
            except Exception:
                logger.exception("scan pass failed")
            logger.info("tickets waiting: {}", ", ".join(c.ticker for c in p.tickets.pending()) or "none")
        elif once:
            logger.warning("{}: not accepting new trades now (use --force-session to test)", phase.label)
        if once:
            return
        if phase is Phase.ENDED:
            logger.info("session ended")
            return
        wait = poll_sec
        if not p.clock.accepting_new_trades(now):
            nxt, at = p.clock.next_change(now)
            wait = min(max((at - now).total_seconds(), 1), 300)
        time.sleep(wait)
