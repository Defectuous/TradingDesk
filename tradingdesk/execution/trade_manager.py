"""
Trade Manager (plain Python, fully automated after approval).

Every tick (trade_manager.poll_interval_sec):
  - sync fills from the broker for every order we placed (handles partial fills)
  - premarket: fire the software entry trigger, time out / re-arm entry orders,
    watch the software stop and targets, exit with marketable limits
  - 9:30: hand software-managed positions to the broker (stop + target per leg)
  - move the stop to breakeven at +breakeven_at_r R; after the first target
    fills, trail the runner's stop
  - 11:00 (new_trade_cutoff): cancel unfilled entries so a late fill can't open
    a position; optionally tighten stops
  - wind_down (11:30, mode "close"): close everything still open, including
    positions the desk didn't open, then send a Discord summary
  - halted stocks: shown as "stuck - halted", Discord alert, retried until the halt lifts
Every action is journaled. Flatten / Flatten All (kill switch) are always available.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from loguru import logger

from tradingdesk.alerts import Notifier
from tradingdesk.broker.base import Broker, BrokerError, OrderStatus
from tradingdesk.execution.closer import Closer, CloseResult
from tradingdesk.execution.trade import ManagedTrade, TradeState
from tradingdesk.journal import Journal
from tradingdesk.models import Agent, CandidateStatus, Fill
from tradingdesk.sessions import ET, Phase, SessionClock, now_et
from tradingdesk.settings import SessionsConfig, TradeManagerConfig, WindDownConfig

S = CandidateStatus


@dataclass
class WindDownSummary:
    closed: list[str] = field(default_factory=list)
    still_open: dict[str, str] = field(default_factory=dict)
    day_pnl: float = 0.0


class TradeManager:
    def __init__(self, broker: Broker, closer: Closer, journal: Journal, notifier: Notifier, clock: SessionClock,
                 cfg: TradeManagerConfig, sessions: SessionsConfig, wind: WindDownConfig,
                 threaded_closes: bool = True) -> None:
        self.broker = broker
        self.closer = closer
        self.journal = journal
        self.notifier = notifier
        self.clock = clock
        self.cfg = cfg
        self.sessions = sessions
        self.wind = wind
        self.executor: Any = None                     # set by build_execution (avoids an import cycle)
        self._trades: dict[str, ManagedTrade] = {}
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="close") if threaded_closes else None
        self._untracked_alerted: set[str] = set()
        self._cutoff_done: set[str] = set()           # trade dates
        self._wind_down_started: set[str] = set()
        self._summary_sent: set[str] = set()
        self.last_summary: WindDownSummary | None = None

    # -- registry ---------------------------------------------------------------------
    def add(self, trade: ManagedTrade) -> None:
        with self._lock:
            self._trades[trade.candidate.id] = trade

    def get(self, candidate_id: str) -> ManagedTrade | None:
        with self._lock:
            return self._trades.get(candidate_id)

    def trades(self, active_only: bool = False) -> list[ManagedTrade]:
        with self._lock:
            return [t for t in self._trades.values() if t.active or not active_only]

    def _event(self, t: ManagedTrade | None, event: str, **detail: Any) -> None:
        self.journal.log_event(event, candidate_id=t.candidate.id if t else None,
                               ticker=t.ticker if t else detail.pop("ticker", None), **detail)

    # -- main loop --------------------------------------------------------------------------
    def tick(self, now: datetime | None = None) -> None:
        now = now or now_et()
        with self._lock:
            try:
                regular = self.broker.clock().is_open
            except BrokerError as exc:
                logger.warning("trade manager: clock unavailable ({}), skipping tick", exc)
                return
            for t in self.trades(active_only=True):
                try:
                    self._sync_fills(t, now)
                    self._manage(t, now, regular)
                    self._check_closed(t, now)
                except BrokerError as exc:
                    logger.warning("trade manager: {}: {}", t.ticker, exc)
            self._time_rules(now, regular)

    # -- fills ---------------------------------------------------------------------------------
    def _sync_fills(self, t: ManagedTrade, now: datetime) -> None:
        c = t.candidate
        for oid, role in list(t.roles.items()):
            if oid in t.done_orders:
                continue
            o = self.broker.get_order(oid)
            for child in o.legs:                                   # discover bracket legs
                if child.id not in t.roles:
                    leg = next((lg for lg in t.legs if f"entry:{lg.name}" == role), None)
                    if leg is not None and self.executor is not None:
                        self.executor._register_leg(t, leg, child.id, child.type)
            prev_qty, prev_avg = t.seen.get(oid, (0, 0.0))
            delta = o.filled_qty - prev_qty
            if delta > 0 and o.filled_avg_price:
                px = (o.filled_avg_price * o.filled_qty - prev_avg * prev_qty) / delta
                reason = role.split(":", 1)[1] if role.startswith("close:") else role.split(":")[0]
                c.add_fill(Fill(side=o.side, qty=delta, price=round(px, 4), at=now, order_id=oid, reason=reason))
                t.seen[oid] = (o.filled_qty, o.filled_avg_price)
                self._event(t, "fill", side=o.side, qty=delta, price=round(px, 4), role=role)
                if o.side == "buy" and c.status is S.HUMAN_APPROVED:
                    c.transition(S.FILLED, by=Agent.EXECUTOR, at=now)
                    self.notifier.send(f"✅ Bought {delta} {c.ticker} @ {px:.2f}")
                if role.startswith("target"):
                    leg = next((lg for lg in t.legs if role.endswith(f":{lg.name}")), None)
                    if leg:
                        leg.target_done = True
                if t.state in (TradeState.WORKING, TradeState.ARMED) and t.held > 0:
                    t.state = TradeState.OPEN
                self.journal.save_candidate(c)
            if o.status is OrderStatus.REJECTED and role.startswith("entry"):
                self._event(t, "order_rejected", order_id=oid, reason=o.reject_reason)
            if not o.status.is_open:
                t.done_orders.add(oid)

    def _check_closed(self, t: ManagedTrade, now: datetime) -> None:
        c = t.candidate
        if c.status is S.FILLED and t.held <= 0 and t.state is not TradeState.WORKING:
            pos = self.broker.position(t.ticker)
            if pos is None or pos.qty <= 0:
                self.broker.cancel_all(t.ticker)                   # no orphan legs left behind
                c.transition(S.CLOSED, by=Agent.TRADE_MANAGER, at=now)
                t.state = TradeState.CLOSED
                self.journal.save_candidate(c)
                self._event(t, "closed", pnl=c.realized_pnl, r=c.r_multiple, reason=t.close_reason or "exit")
                self.notifier.send(f"{'🟢' if c.realized_pnl >= 0 else '🔴'} Closed {c.ticker}: "
                                   f"{c.realized_pnl:+.2f} ({c.r_multiple}R) — {t.close_reason or 'exit'}")

    # -- per-trade management ---------------------------------------------------------------------
    def _manage(self, t: ManagedTrade, now: datetime, regular: bool) -> None:
        if t.state is TradeState.CLOSING:
            self._collect_close(t, now)
            return
        if t.state is TradeState.STUCK_HALTED:
            if t.last_close_try is None or (now - t.last_close_try).total_seconds() >= self.wind.halt_retry_interval_sec:
                self.start_close(t, t.close_reason or "retry", now)
            return
        if t.state is TradeState.ARMED:
            self._armed(t, now, regular)
            return
        if t.state is TradeState.WORKING and t.software_entry_id:
            self._premarket_entry_timeout(t, now)
        if t.held <= 0:
            return

        q = self.broker.quote(t.ticker)
        if q is None:
            return
        price, bid = q.price, (q.bid or q.price)
        t.high = max(t.high, price)

        # 9:30 handoff: software legs -> broker stop + target
        if regular and t.software_managed:
            self._handoff(t)

        # software stop / targets (premarket, or if the handoff failed)
        if t.software_managed:
            if bid <= t.stop:
                self.start_close(t, "stop", now)
                return
            self._software_targets(t, bid, regular)

        # breakeven
        if not t.breakeven_done and price >= t.entry + self.cfg.breakeven_at_r * t.risk_per_share:
            self.move_stop(t, t.entry, "breakeven")
            t.breakeven_done = True

        # trail the runner after the first target
        if self.cfg.trail_after_partial and any(lg.target_done for lg in t.legs if lg.name == "a"):
            new = round(t.high * (1 - self.cfg.trail_pct / 100), 2)
            if new > t.stop * (1 + self.cfg.trail_min_step_pct / 100) and new < price:
                self.move_stop(t, new, "trail")

    def _armed(self, t: ManagedTrade, now: datetime, regular: bool) -> None:
        if not self.clock.accepting_new_trades(now):
            self._expire(t, now, "entry never triggered before the cutoff")
            return
        if regular:
            # premarket trade still waiting at the open: let the broker hold trigger + stop + target
            for leg in t.legs:
                leg.software = False
            self.executor.place_brackets(t)
            self._event(t, "converted_to_bracket")
            return
        q = self.broker.quote(t.ticker)
        if q is None:
            return
        trigger_hit = (q.ask or q.price) >= t.entry or q.price >= t.entry
        too_far = q.price > t.entry * (1 + self.cfg.entry_limit_slip_pct / 100)
        if trigger_hit and not too_far:
            self.executor.place_premarket_entry(t, t.entry_attempts + 1)
            self._event(t, "premarket_entry", price=q.price, attempt=t.entry_attempts)

    def _premarket_entry_timeout(self, t: ManagedTrade, now: datetime) -> None:
        oid = t.software_entry_id
        o = self.broker.get_order(oid)
        if not o.status.is_open:                      # cancelled / rejected / filled elsewhere
            if t.held <= 0 and t.state is TradeState.WORKING:
                if t.entry_attempts < self.cfg.max_entry_attempts:
                    t.state = TradeState.ARMED
                else:
                    self._expire(t, now, "premarket entry did not fill")
            return
        if t.entry_submitted_at and (now - t.entry_submitted_at).total_seconds() >= self.cfg.entry_timeout_sec:
            self.broker.cancel(oid)
            self._sync_fills(t, now)
            self._event(t, "entry_timeout", attempt=t.entry_attempts, filled=t.held)
            if t.held > 0:
                t.state = TradeState.OPEN
                # a partial premarket fill: resize legs to what we actually hold
                self._resize_legs(t, t.held)
            elif t.entry_attempts < self.cfg.max_entry_attempts:
                t.state = TradeState.ARMED
            else:
                self._expire(t, now, f"premarket entry unfilled after {t.entry_attempts} attempts")

    @staticmethod
    def _resize_legs(t: ManagedTrade, held: int) -> None:
        remaining = held
        for leg in t.legs:
            leg.qty = min(leg.qty, remaining)
            remaining -= leg.qty
        t.legs = [leg for leg in t.legs if leg.qty > 0]

    def _software_targets(self, t: ManagedTrade, bid: float, regular: bool) -> None:
        for leg in t.legs:
            if leg.target_done or leg.target_id or bid < leg.target:
                continue
            qty = min(leg.qty, t.held)
            if qty <= 0:
                continue
            o = self.broker.submit_limit(t.ticker, "sell", qty, bid, f"td-{t.candidate.id[:20]}-tgt-{leg.name}",
                                         extended_hours=not regular)
            leg.target_id = o.id
            t.roles[o.id] = f"target:{leg.name}"
            self._event(t, "software_target", leg=leg.name, qty=qty, price=bid)

    def _handoff(self, t: ManagedTrade) -> None:
        """Regular session open: put a broker-held stop + target on each remaining leg."""
        remaining = t.held
        for leg in t.legs:
            if not leg.software or leg.target_done or remaining <= 0:
                continue
            if leg.target_id:                     # a software target sell still working: let it finish
                continue
            qty = min(leg.qty, remaining)
            o = self.broker.submit_exit_oco(t.ticker, qty, t.stop, leg.target, f"td-{t.candidate.id[:20]}-oco-{leg.name}")
            leg.target_id = o.id
            t.roles[o.id] = f"target:{leg.name}"
            for child in o.legs:
                leg.stop_id = child.id
                t.roles[child.id] = f"stop:{leg.name}"
            leg.software = False
            remaining -= qty
            self._event(t, "handoff_to_broker", leg=leg.name, qty=qty, stop=t.stop, target=leg.target)

    def move_stop(self, t: ManagedTrade, new_stop: float, why: str) -> None:
        if new_stop <= t.stop:
            return
        old, t.stop = t.stop, round(new_stop, 4)
        for leg in t.legs:
            if leg.stop_id and leg.stop_id not in t.done_orders:
                try:
                    self.broker.replace_order(leg.stop_id, stop_price=t.stop)
                except BrokerError as exc:
                    logger.warning("{}: could not move stop on leg {}: {}", t.ticker, leg.name, exc)
        self._event(t, "stop_moved", old=old, new=t.stop, reason=why)

    def _expire(self, t: ManagedTrade, now: datetime, why: str) -> None:
        self.broker.cancel_all(t.ticker)
        self._sync_fills(t, now)
        if t.held > 0:
            t.state = TradeState.OPEN
            return
        t.state = TradeState.EXPIRED
        c = t.candidate
        if c.status is S.HUMAN_APPROVED:
            c.transition(S.EXPIRED, by=Agent.TRADE_MANAGER, at=now)
            self.journal.save_candidate(c)
        self._event(t, "entry_expired", reason=why)

    # -- closing ---------------------------------------------------------------------------------
    def start_close(self, t: ManagedTrade, reason: str, now: datetime | None = None) -> None:
        if t.state is TradeState.CLOSING:
            return
        t.state, t.close_reason, t.last_close_try = TradeState.CLOSING, reason, now or now_et()
        self._event(t, "close_started", reason=reason)

        def register(oid: str, role: str) -> None:
            with self._lock:
                t.roles[oid] = role

        job = lambda: self.closer.close(t.ticker, reason, client_prefix=f"td-{t.candidate.id[:12]}-x", register=register)
        if self._pool is None:
            from concurrent.futures import Future

            f: Future = Future()
            f.set_result(job())
            t.close_future = f
        else:
            t.close_future = self._pool.submit(job)

    def _collect_close(self, t: ManagedTrade, now: datetime) -> None:
        f = t.close_future
        if f is None or not f.done():
            return
        res: CloseResult = f.result()
        t.close_future = None
        self._sync_fills(t, now)
        if res.flat:
            t.state = TradeState.OPEN            # _check_closed moves it to CLOSED
        elif res.stuck_halted:
            t.state = TradeState.STUCK_HALTED
            if not t.stuck_alerted:
                self.notifier.send(f"🚨 {t.ticker} is HALTED and still open ({t.held} sh). Retrying when the halt lifts.")
                t.stuck_alerted = True
            self._event(t, "stuck_halted", held=t.held)
        else:
            t.state = TradeState.OPEN
            self.notifier.send(f"🚨 {t.ticker}: close failed ({res.message}); {t.held} sh still open")
            self._event(t, "close_failed", message=res.message)

    def wait_idle(self, timeout: float = 30) -> None:
        """Block until background closes finish (tests, kill switch)."""
        for t in self.trades():
            if t.close_future is not None:
                t.close_future.result(timeout=timeout)

    def flatten(self, ticker: str, reason: str = "flatten", now: datetime | None = None) -> list[CloseResult]:
        """Cancel the ticker's entries and close whatever is held (tracked or not). Blocks until done."""
        now = now or now_et()
        with self._lock:
            mine = [t for t in self.trades(active_only=True) if t.ticker == ticker]
            for t in mine:
                if t.held <= 0:
                    self._expire(t, now, reason)
            holders = [t for t in mine if t.active and t.held > 0]
            for t in holders:
                self.start_close(t, reason, now)
        results: list[CloseResult] = []
        if not holders:
            pos = self.broker.position(ticker)
            if pos and pos.qty > 0:
                results.append(self.closer.close(ticker, reason))
        self.wait_idle()
        with self._lock:
            for t in holders:
                self._collect_close(t, now)
                self._check_closed(t, now)
        return results

    def kill_switch(self, now: datetime | None = None) -> dict[str, str]:
        """Cancel every open order in the account, then close every position (tracked or not)."""
        now = now or now_et()
        self.notifier.send("🛑 KILL SWITCH: cancelling all orders and closing all positions")
        self._event(None, "kill_switch", ticker=None)
        self.broker.cancel_all()
        tickers = {p.ticker for p in self.broker.positions()} | {t.ticker for t in self.trades(active_only=True)}
        out: dict[str, str] = {}
        for tk in sorted(tickers):
            self.flatten(tk, "kill-switch", now)
            pos = self.broker.position(tk)
            out[tk] = "flat" if pos is None or pos.qty <= 0 else f"still open: {pos.qty} sh"
        return out

    # -- session clock rules ---------------------------------------------------------------------
    def _time_rules(self, now: datetime, regular: bool) -> None:
        day = now.astimezone(ET).date().isoformat()
        phase = self.clock.phase_at(now)
        past_cutoff = phase in (Phase.WIND_DOWN, Phase.ENDED)
        if past_cutoff and day not in self._cutoff_done:
            self._cutoff_done.add(day)
            for t in self.trades(active_only=True):
                if t.state in (TradeState.ARMED, TradeState.WORKING):
                    self._expire(t, now, "11:00 cutoff: unfilled entry cancelled")
            self._event(None, "cutoff", ticker=None)
        if past_cutoff and self.sessions.tighten_stops_after_cutoff and self.cfg.tighten_to_breakeven:
            for t in self.trades(active_only=True):
                q = self.broker.quote(t.ticker) if t.held > 0 and not t.tightened else None
                if q and q.price > t.entry:
                    self.move_stop(t, t.entry, "tighten after cutoff")
                    t.tightened = True

        wind_at = datetime.combine(now.astimezone(ET).date(), self.sessions.wind_down, ET)
        if self.sessions.wind_down_mode == "close" and now >= wind_at and self.clock.is_trading_weekday(now.date()):
            if day not in self._wind_down_started:
                self._wind_down_started.add(day)
                self.notifier.send("⏳ Wind-down: closing all open positions")
                self._event(None, "wind_down_started", ticker=None)
            for t in self.trades(active_only=True):
                if t.held > 0 and t.state not in (TradeState.CLOSING, TradeState.STUCK_HALTED):
                    self.start_close(t, "wind-down", now)
            self._close_untracked(now)
            if day not in self._summary_sent and not any(t.active for t in self.trades()):
                self._send_summary(day)

    def _close_untracked(self, now: datetime) -> None:
        tracked = {t.ticker for t in self.trades(active_only=True)}
        for p in self.broker.positions():
            if p.ticker in tracked or p.qty <= 0:
                continue
            if p.ticker not in self._untracked_alerted:
                self._untracked_alerted.add(p.ticker)
                self.notifier.send(f"⚠️ {p.ticker}: {p.qty} sh open that TradingDesk didn't open; closing at wind-down")
            res = self.closer.close(p.ticker, "wind-down")
            self._event(None, "close_untracked", ticker=p.ticker, flat=res.flat, message=res.message)

    def _send_summary(self, day: str) -> None:
        trades = [t for t in self.trades() if t.candidate.trade_date == day]
        s = WindDownSummary(
            closed=[t.ticker for t in trades if t.state is TradeState.CLOSED],
            still_open={t.ticker: t.state.value for t in trades if t.active},
            day_pnl=round(sum(t.candidate.realized_pnl for t in trades), 2),
        )
        for p in self.broker.positions():
            if p.qty > 0:
                s.still_open.setdefault(p.ticker, "open at broker")
        self.last_summary = s
        self._summary_sent.add(day)
        lines = [f"📋 Wind-down done — day P&L {s.day_pnl:+.2f}",
                 f"Closed: {', '.join(s.closed) or 'none'}",
                 f"Still open: {', '.join(f'{k} ({v})' for k, v in s.still_open.items()) or 'none'}"]
        self.notifier.send("\n".join(lines))
        self._event(None, "wind_down_summary", ticker=None, closed=s.closed, still_open=s.still_open, pnl=s.day_pnl)

    # -- startup -------------------------------------------------------------------------------------
    def reconcile(self) -> list[str]:
        """Positions at the broker that no tracked trade owns (e.g. after a restart)."""
        tracked = {t.ticker for t in self.trades(active_only=True)}
        stray = [p.ticker for p in self.broker.positions() if p.qty > 0 and p.ticker not in tracked]
        for tk in stray:
            if tk not in self._untracked_alerted:
                self._untracked_alerted.add(tk)
                self.notifier.send(f"⚠️ {tk}: open position TradingDesk isn't tracking (restart?). Flatten it or it "
                                   f"will be closed at wind-down.")
        return stray

