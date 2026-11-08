"""
Wiring and the main loops.

  scan loop (main thread)      Scout -> Manager (Skeptic -> Risk Officer) -> tickets
  manage loop (thread)         ticket expiry + Trade Manager tick every poll_interval_sec

Orders only happen through DeskService.approve() (the Trade Desk). The Manager
has no reference to the Executor.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from loguru import logger

from tradingdesk.account import AlpacaCash, BrokerCash, CashSource, JournalAccountProvider
from tradingdesk.agents.manager import Manager
from tradingdesk.agents.risk_officer import RiskOfficer
from tradingdesk.agents.scout import Scout
from tradingdesk.agents.skeptic import StubSkeptic
from tradingdesk.alerts import DiscordNotifier, Notifier, NullNotifier
from tradingdesk.broker import make_broker
from tradingdesk.broker.base import Broker
from tradingdesk.desk.service import DeskService
from tradingdesk.execution.closer import Closer
from tradingdesk.execution.executor import Executor
from tradingdesk.execution.trade_manager import TradeManager
from tradingdesk.health import Health
from tradingdesk.journal import Journal
from tradingdesk.legacy.wtbot import config as wtbot_config
from tradingdesk.market import MarketData, WtbotMarketData
from tradingdesk.news.newsie import Newsie, build_newsie
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
    risk: RiskOfficer
    accounts: JournalAccountProvider
    market: MarketData
    cache: ResearchCache


@dataclass
class App:
    pipeline: Pipeline
    broker: Broker
    trade_manager: TradeManager
    executor: Executor
    desk: DeskService
    notifier: Notifier
    settings: Settings
    health: Health
    newsie: Newsie | None = None


def build_pipeline(
    settings: Settings,
    journal: Journal,
    market: MarketData | None = None,
    force_session: TradingSession | None = None,
    cash: CashSource | None = None,
    broker: Broker | None = None,
) -> Pipeline:
    wtbot_config.apply_settings(settings)
    clock = SessionClock(settings.config.sessions)
    if force_session:
        clock = clock.force(force_session)
    market = market or WtbotMarketData()
    cache = ResearchCache()
    tickets = TicketQueue()
    if cash is None:
        cash = BrokerCash(broker) if broker else AlpacaCash(settings.secrets.alpaca_api_key,
                                                             settings.secrets.alpaca_secret_key)
    accounts = JournalAccountProvider(journal, tickets, cash, settings.config.risk.cash_refresh_sec, broker=broker)
    risk = RiskOfficer(settings.config.risk, clock)
    scout = Scout(settings, market, cache, clock)
    manager = Manager(settings.config.manager, cache, StubSkeptic(), risk, accounts, market, journal, tickets)
    return Pipeline(clock, scout, manager, tickets, journal, risk, accounts, market, cache)


def build_app(
    settings: Settings,
    journal: Journal,
    market: MarketData | None = None,
    broker: Broker | None = None,
    notifier: Notifier | None = None,
    force_session: TradingSession | None = None,
    cash: CashSource | None = None,
    threaded_closes: bool = True,
    sleep=time.sleep,
    with_newsie: bool = True,
) -> App:
    market = market or WtbotMarketData()
    broker = broker or make_broker(settings, market)
    p = build_pipeline(settings, journal, market, force_session, cash=cash, broker=broker)
    if notifier is None:
        url = settings.secrets.discord_webhook_url
        notifier = DiscordNotifier(url) if (url and settings.config.alerts.discord_enabled) else NullNotifier()
    cfg = settings.config
    closer = Closer(broker, cfg.wind_down, sleep=sleep)
    tm = TradeManager(broker, closer, journal, notifier, p.clock, cfg.trade_manager, cfg.sessions, cfg.wind_down,
                      threaded_closes=threaded_closes)
    executor = Executor(broker, tm, cfg.trade_manager, journal, notifier)
    tm.executor = executor
    desk = DeskService(cfg.desk, p.tickets, p.risk, p.accounts, market, executor, tm, journal, notifier)
    p.manager.on_ticket = desk.on_new_ticket
    health = Health()
    newsie = None
    if with_newsie:
        newsie = build_newsie(settings, journal, p.cache, p.clock, health)
        p.scout.on_movers = newsie.watch
    return App(p, broker, tm, executor, desk, notifier, settings, health, newsie)


def run_once(p: Pipeline, health: Health | None = None) -> int:
    now = now_et()
    try:
        hits = p.scout.scan(now)
    except Exception as exc:
        if health:
            health.error("scout", f"scan failed: {exc}")
        raise
    if health:
        health.ok("scout", f"{len(hits)} hit(s)")
    for hit in hits:
        p.manager.process(hit, now)
    return len(hits)


def start_manage_loop(app: App, poll_sec: float, stop: threading.Event) -> threading.Thread:
    def loop() -> None:
        while not stop.is_set():
            try:
                app.desk.expire_stale()
                app.trade_manager.tick()
                app.health.ok("trade_manager")
            except Exception as exc:
                logger.exception("manage loop error")
                app.health.error("trade_manager", str(exc))
            stop.wait(poll_sec)

    th = threading.Thread(target=loop, name="trade-manager", daemon=True)
    th.start()
    return th


def run_loop(app: App, poll_sec: float, once: bool = False, stop: threading.Event | None = None) -> None:
    """Scan loop. Returns when the session has ended (or after one pass with once=True, or when stop is set)."""
    p = app.pipeline
    try:
        stray = app.trade_manager.reconcile()
    except Exception as exc:
        logger.error("could not reconcile broker positions: {}", exc)
        stray = []
    if stray:
        logger.warning("untracked broker positions: {}", ", ".join(stray))
    stop = stop or threading.Event()
    start_manage_loop(app, app.trade_manager.cfg.poll_interval_sec, stop)
    if app.newsie is not None and not once:
        app.newsie.start(stop)
    try:
        while not stop.is_set():
            now = now_et()
            phase = p.clock.phase_at(now)
            if phase is Phase.STARTUP and app.newsie is not None:
                try:                                 # let Newsie research premarket movers before tickets
                    p.scout.peek_movers()
                except Exception:
                    logger.exception("startup movers peek failed")
            if p.clock.accepting_new_trades(now):
                try:
                    run_once(p, app.health)
                except Exception:
                    logger.exception("scan pass failed")
                logger.info("tickets waiting: {}", ", ".join(c.ticker for c in p.tickets.pending()) or "none")
            elif once:
                logger.warning("{}: not accepting new trades now (use --force-session to test)", phase.label)
            if once:
                return
            if phase is Phase.ENDED and not app.trade_manager.trades(active_only=True):
                logger.info("session ended")
                return
            wait = poll_sec
            if phase is Phase.STARTUP:
                wait = min(poll_sec * 2, max((p.clock.next_change(now)[1] - now).total_seconds(), 1))
            elif not p.clock.accepting_new_trades(now):
                _, at = p.clock.next_change(now)
                wait = min(max((at - now).total_seconds(), 1), 60)
            stop.wait(wait)
    finally:
        stop.set()
