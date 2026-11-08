"""
What the Trade Desk shows, as plain data. The NiceGUI page (desk/ui.py) only
renders these snapshots and calls DeskService; no trading logic lives in the UI.

One DeskView is shared by every open browser tab. Snapshots are cached for
`SNAPSHOT_TTL` seconds and broker/quote reads for `SLOW_TTL` seconds, so extra
tabs don't multiply broker API calls.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, TypeVar

from loguru import logger

from tradingdesk.agents.risk_officer import AccountState
from tradingdesk.broker.base import Position
from tradingdesk.execution.trade import TradeState
from tradingdesk.market import Quote
from tradingdesk.models import Agent, Candidate, CandidateStatus
from tradingdesk.sessions import ET, Phase, now_et

S = CandidateStatus
T = TypeVar("T")

SNAPSHOT_TTL = 1.0
SLOW_TTL = 3.0
REJECTED_LIMIT = 50


# ---------------------------------------------------------------------------
# View records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PillarView:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class HeadlineView:
    title: str
    grade: str | None
    sources: tuple[str, ...]
    url: str
    published: str
    outlet: str = ""


@dataclass(frozen=True)
class TicketView:
    id: str
    ticker: str
    premarket: bool
    price: float
    change_pct: float
    relative_volume: float
    float_shares: int | None
    spread_pct: float | None
    pillars: tuple[PillarView, ...]
    catalyst_badge: str                  # "Catalyst A · FDA" or "Catalyst pending"
    catalyst_grade: str | None
    grade_reason: str
    headlines: tuple[HeadlineView, ...]
    filing_flags: tuple[str, ...]
    buzz: str
    regime: str
    promo_warning: bool
    promo_reasons: tuple[str, ...]
    entry: float
    stop: float
    target: float
    target_2: float | None
    risk_reward: float
    risk_per_share: float
    shares: int
    dollars_at_risk: float
    position_value: float
    software_managed_stop: bool
    objections: tuple[str, ...]          # the Skeptic's case against, even though it passed
    seconds_left: float
    ttl: float
    live_price: float | None
    drift_pct: float | None
    max_drift_pct: float
    strategy: str


@dataclass(frozen=True)
class PositionView:
    key: str                             # candidate id, or "untracked:TICKER"
    ticker: str
    state: str                           # armed / working / open / closing / stuck_halted / untracked
    qty: int
    avg_entry: float | None
    price: float | None
    unrealized_pl: float | None
    realized_pl: float
    stop: float | None
    targets: tuple[float, ...]
    software_managed: bool
    stuck_halted: bool
    untracked: bool


@dataclass(frozen=True)
class RejectedView:
    id: str
    ticker: str
    at: str
    session: str
    rejected_by: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class HealthRow:
    name: str
    state: str                           # ok / warn / error / off
    detail: str


@dataclass(frozen=True)
class StatusView:
    phase: str
    phase_key: str
    next_phase: str
    seconds_to_next: float
    accepting_new_trades: bool
    approvals_enabled: bool
    day_pnl: float
    max_daily_loss: float
    trades_today: int
    max_trades: int
    llm_spend: float
    llm_cap: float
    broker: str
    health: tuple[HealthRow, ...]
    session_ended: bool
    summary: tuple[str, ...]
    error: str | None = None


@dataclass(frozen=True)
class DeskSnapshot:
    at: datetime
    status: StatusView
    tickets: tuple[TicketView, ...]
    positions: tuple[PositionView, ...]
    rejected: tuple[RejectedView, ...]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _TTL:
    """Thread-safe single-value cache."""

    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self._lock = threading.Lock()
        self._vals: dict[str, tuple[float, Any]] = {}

    def get(self, key: str, fn: Callable[[], T]) -> T:
        with self._lock:
            hit = self._vals.get(key)
            if hit and time.monotonic() - hit[0] < self.ttl:
                return hit[1]
        val = fn()
        with self._lock:
            self._vals[key] = (time.monotonic(), val)
        return val


def _fmt_pillar(name: str, p) -> str:
    bits = []
    if p.value is not None:
        bits.append(f"{p.value:,.2f}" if isinstance(p.value, float) else str(p.value))
    if p.threshold is not None:
        bits.append(f"need {p.threshold:,.0f}" if isinstance(p.threshold, (int, float)) else f"need {p.threshold}")
    if p.note:
        bits.append(p.note)
    return " · ".join(bits)


def ticket_view(c: Candidate, seconds_left: float, ttl: float, quote: Quote | None, max_drift: float) -> TicketView:
    sc, n, se, t, r = c.scout, c.news, c.sentiment, c.proposed_trade, c.risk_sizing
    assert sc and t and r, "a ticket has scout data, a trade and sizing"
    flags = []
    if n:
        ff = n.filing_flags
        flags = [label for on, label in ((ff.offering, "offering"), (ff.dilution, "dilution"),
                                         (ff.insider_buy, "insider buy"), (ff.insider_sell, "insider sell")) if on]
        if ff.forms:
            flags.append("forms: " + ", ".join(ff.forms))
    heads = tuple(
        HeadlineView(g.headline.title, "SEC" if g.headline.source == "edgar" else g.grade,
                     (g.headline.source, *g.headline.also_seen_in), g.headline.url,
                     f"{g.headline.published_at.astimezone(ET):%a %H:%M}", g.headline.outlet)
        for g in (n.headlines if n else ())
    )
    objections = tuple(reason for v in c.verdicts if v.agent is Agent.SKEPTIC for reason in v.reasons)
    drift = None
    if quote:
        drift = round((quote.price - t.entry) / t.entry * 100, 2)
    return TicketView(
        id=c.id, ticker=c.ticker, premarket=c.session == "premarket",
        price=sc.price, change_pct=sc.change_pct, relative_volume=sc.relative_volume,
        float_shares=sc.float_shares, spread_pct=sc.spread_pct,
        pillars=tuple(PillarView(k, p.passed, _fmt_pillar(k, p)) for k, p in sc.pillars.items()),
        catalyst_badge=(f"Catalyst {n.catalyst_grade} · {n.catalyst_type}"
                        + ("" if n.graded_by == "claude" else f" ({n.graded_by})")) if n
        else "Catalyst pending (Newsie hasn't graded it yet)",
        catalyst_grade=n.catalyst_grade if n else None,
        grade_reason=n.grade_reason if n else "",
        headlines=heads, filing_flags=tuple(flags),
        buzz=buzz_text(se),
        regime=(se.market_regime + (f" ({se.regime_detail['summary']})" if se.regime_detail.get("summary") else ""))
        if se else "pending",
        promo_warning=bool(se and se.promo_warning),
        promo_reasons=se.promo_reasons if se else (),
        entry=t.entry, stop=t.stop, target=t.target, target_2=t.target_2,
        risk_reward=t.risk_reward, risk_per_share=t.risk_per_share,
        shares=r.shares, dollars_at_risk=r.dollars_at_risk, position_value=r.position_value,
        software_managed_stop=r.software_managed_stop,
        objections=objections, seconds_left=round(seconds_left, 1), ttl=ttl,
        live_price=quote.price if quote else None, drift_pct=drift, max_drift_pct=max_drift,
        strategy=t.strategy,
    )


def buzz_text(se) -> str:
    if se is None:
        return "pending (Sentiment hasn't read it yet)"
    if se.buzz_score is None:
        return "no social data" + (f" ({', '.join(se.sources)})" if se.sources else "")
    bits = [f"{se.buzz_score:g}/100 · {se.buzz_trend}"]
    if se.crowd_score is not None:
        bits.append(f"StockTwits {se.crowd_score:g} {(se.crowd_label or '').replace('_', ' ').lower()}".rstrip())
    if se.tone is not None:
        bits.append(f"tone {se.tone:+.2f}")
    return " · ".join(bits)


def rejected_view(c: Candidate) -> RejectedView:
    at = c.status_history[-1].at if c.status_history else c.created_at
    return RejectedView(c.id, c.ticker, f"{at.astimezone(ET):%H:%M:%S}", c.session,
                        (c.rejected_by.value if c.rejected_by else "?").replace("_", " "), c.rejection_reasons)


# ---------------------------------------------------------------------------
# DeskView
# ---------------------------------------------------------------------------

class DeskView:
    """Builds DeskSnapshots from the running App (runner.App)."""

    def __init__(self, app, snapshot_ttl: float = SNAPSHOT_TTL, slow_ttl: float = SLOW_TTL) -> None:
        self.app = app
        self.settings = app.settings
        self._snap = _TTL(snapshot_ttl)
        self._slow = _TTL(slow_ttl)
        self._bars: dict[str, list[dict[str, Any]] | None] = {}
        self._bars_lock = threading.Lock()
        self._bar_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="desk-bars")

    # -- public --------------------------------------------------------------------------
    def snapshot(self) -> DeskSnapshot:
        return self._snap.get("snap", lambda: self.build(now_et()))

    def build(self, now: datetime) -> DeskSnapshot:
        positions, broker_err = self._positions()
        account, acct_err = self._account(now)
        tickets = tuple(self._tickets(now))
        return DeskSnapshot(
            at=now,
            status=self._status(now, account, broker_err or acct_err),
            tickets=tickets,
            positions=tuple(self._position_views(positions)),
            rejected=tuple(self._rejected(now)),
        )

    def chart(self, candidate_id: str) -> list[dict[str, Any]] | None:
        """One-minute bars for a ticket's chart. Fetched once in the background; None until loaded."""
        with self._bars_lock:
            if candidate_id in self._bars:
                return self._bars[candidate_id]
            self._bars[candidate_id] = None
        c = self.app.pipeline.tickets.get(candidate_id)
        if c is not None:
            self._bar_pool.submit(self._load_bars, c)
        return None

    # -- pieces ---------------------------------------------------------------------------
    def _quote(self, ticker: str) -> Quote | None:
        def read() -> Quote | None:
            try:
                return self.app.pipeline.market.quote(ticker)
            except Exception as exc:
                logger.debug("desk quote {} failed: {}", ticker, exc)
                return None
        return self._slow.get(f"quote:{ticker}", read)

    def _positions(self) -> tuple[list[Position], str | None]:
        def read() -> tuple[list[Position], str | None]:
            try:
                return list(self.app.broker.positions()), None
            except Exception as exc:
                return [], f"positions unavailable: {exc}"
        return self._slow.get("positions", read)

    def _account(self, now: datetime) -> tuple[AccountState | None, str | None]:
        def read() -> tuple[AccountState | None, str | None]:
            try:
                return self.app.pipeline.accounts.state(now), None
            except Exception as exc:
                return None, f"account unavailable: {exc}"
        return self._slow.get("account", read)

    def _tickets(self, now: datetime) -> list[TicketView]:
        desk = self.app.desk
        out = []
        for c in self.app.pipeline.tickets.pending():
            try:
                out.append(ticket_view(c, desk.seconds_left(c, now), desk.cfg.ticket_ttl_sec,
                                       self._quote(c.ticker), desk.cfg.max_entry_drift_pct))
            except Exception:
                logger.exception("desk: could not render ticket {}", c.ticker)
        out.sort(key=lambda t: t.seconds_left)
        return out

    def _position_views(self, broker_positions: list[Position]) -> list[PositionView]:
        by_ticker = {p.ticker: p for p in broker_positions if p.qty > 0}
        out: list[PositionView] = []
        seen: set[str] = set()
        for t in self.app.trade_manager.trades(active_only=True):
            p = by_ticker.get(t.ticker)
            seen.add(t.ticker)
            targets = tuple(sorted({leg.target for leg in t.legs if not leg.target_done}))
            buys = [f for f in t.candidate.fills if f.side == "buy"]
            avg = round(sum(f.qty * f.price for f in buys) / sum(f.qty for f in buys), 4) if buys else None
            out.append(PositionView(
                key=t.candidate.id, ticker=t.ticker, state=t.state.value, qty=t.held,
                avg_entry=p.avg_entry if p else avg, price=p.market_price if p else None,
                unrealized_pl=p.unrealized_pl if p else None, realized_pl=t.candidate.realized_pnl,
                stop=t.stop, targets=targets, software_managed=t.software_managed,
                stuck_halted=t.state is TradeState.STUCK_HALTED, untracked=False,
            ))
        for tk, p in by_ticker.items():
            if tk not in seen:
                out.append(PositionView(
                    key=f"untracked:{tk}", ticker=tk, state="untracked", qty=p.qty, avg_entry=p.avg_entry,
                    price=p.market_price, unrealized_pl=p.unrealized_pl, realized_pl=0.0, stop=None, targets=(),
                    software_managed=False, stuck_halted=False, untracked=True,
                ))
        return out

    def _rejected(self, now: datetime) -> list[RejectedView]:
        def read() -> list[Candidate]:
            try:
                return self.app.pipeline.journal.list_candidates(trade_date=now.date().isoformat(),
                                                                 status=S.REJECTED)
            except Exception as exc:
                logger.warning("desk: rejected list unavailable: {}", exc)
                return []
        rows = [rejected_view(c) for c in self._slow.get("rejected", read)]
        rows.sort(key=lambda r: r.at, reverse=True)
        return rows[:REJECTED_LIMIT]

    def _status(self, now: datetime, account: AccountState | None, err: str | None) -> StatusView:
        app, cfg = self.app, self.settings.config
        p = app.pipeline
        phase = p.clock.phase_at(now)
        nxt, at = p.clock.next_change(now)
        accepting = p.clock.accepting_new_trades(now)
        session = p.clock.trading_session_at(now) or "open"
        if account is not None:
            max_loss = p.risk.dollar_limits(session, account).max_daily_loss
            loss_hit = p.risk.daily_loss_hit(account)
            day_pnl, trades = account.day_pnl, account.trades_today
        else:
            max_loss, loss_hit, day_pnl, trades = 0.0, True, 0.0, 0   # unknown account: fail closed
        try:
            spend = p.journal.llm_spend(now.date().isoformat())
        except Exception:
            spend = 0.0
        caps = app.broker.capabilities()
        mode = "dry-run" if caps.dry_run else ("paper" if caps.paper else "LIVE")
        ended = phase is Phase.ENDED and not app.trade_manager.trades(active_only=True)
        return StatusView(
            phase=phase.label, phase_key=phase.value, next_phase=nxt.label,
            seconds_to_next=max(0.0, (at - now).total_seconds()),
            accepting_new_trades=accepting, approvals_enabled=not loss_hit,
            day_pnl=round(day_pnl, 2), max_daily_loss=max_loss,
            trades_today=trades, max_trades=cfg.risk.max_trades_per_day,
            llm_spend=round(spend, 4), llm_cap=cfg.llm.daily_spend_cap_usd,
            broker=f"{app.broker.name} ({mode})",
            health=tuple(self._health(now, accepting, err)),
            session_ended=ended, summary=tuple(self._summary(now)) if ended else (),
            error=err,
        )

    def _health(self, now: datetime, accepting: bool, broker_err: str | None) -> list[HealthRow]:
        h = self.app.health
        cfg = self.settings.config
        rows: list[HealthRow] = []

        def beat_row(name: str, label: str, stale_after: float, active: bool) -> HealthRow:
            b = h.get(name)
            if not active:
                return HealthRow(label, "off", "outside trading window")
            if b is None:
                return HealthRow(label, "warn", "no pass yet")
            age = (now - b.at).total_seconds()
            if not b.ok:
                return HealthRow(label, "error", b.detail)
            if age > stale_after:
                return HealthRow(label, "warn", f"last pass {age:.0f}s ago")
            return HealthRow(label, "ok", f"{age:.0f}s ago" + (f" · {b.detail}" if b.detail else ""))

        rows.append(beat_row("scout", "Scout / market data", cfg.scout.poll_interval_sec * 3 + 30, accepting))
        rows.append(beat_row("trade_manager", "Trade Manager", cfg.trade_manager.poll_interval_sec * 5 + 10, True))
        rows.append(HealthRow("Broker", "error" if broker_err else "ok", broker_err or self.app.broker.name))
        notifier = self.app.notifier
        if hasattr(notifier, "url"):
            last = getattr(notifier, "last_error", None)
            rows.append(HealthRow("Discord", "warn" if last else "ok", last or "webhook set"))
        else:
            rows.append(HealthRow("Discord", "off", "no webhook (DISCORD_WEBHOOK_URL)"))
        rows.append(self._agent_health(now, "Newsie", "newsie", "news", self.settings.config.news.poll_interval_sec))
        rows.append(self._agent_health(now, "Sentiment", "sentiment", "social",
                                       self.settings.config.sentiment.poll_interval_sec))
        rows.append(HealthRow("Skeptic", "warn", "stub, always passes (phase 6)"))
        return rows

    def _agent_health(self, now: datetime, label: str, attr: str, prefix: str, poll_sec: float) -> HealthRow:
        agent = getattr(self.app, attr, None)
        if agent is None:
            return HealthRow(label, "off", "not running")
        h = self.app.health
        phase = self.app.pipeline.clock.phase_at(now)
        if phase not in (Phase.STARTUP, Phase.PREMARKET, Phase.OPEN):
            return HealthRow(label, "off", "polls only during Startup / Premarket / Open")
        parts, state = [], "ok"
        if attr == "sentiment":
            rb = h.get("regime")
            parts.append("regime: waiting" if rb is None else (f"regime: {rb.detail}" if rb.ok
                                                               else f"regime: ERROR {rb.detail[:60]}"))
        for src in agent.sources:
            b = h.get(f"{prefix}:{src.name}")
            if b is None:
                parts.append(f"{src.name}: waiting")
            elif b.ok:
                parts.append(f"{src.name}: ok")
            else:
                parts.append(f"{src.name}: ERROR {b.detail[:60]}")
                state = "warn"
        parts += [f"{name}: off ({why})" for name, why in agent.status().items()]
        b = h.get(attr)
        if b is None:
            state, lead = ("warn", "no pass yet")
        elif not b.ok:
            state, lead = "error", b.detail
        else:
            age = (now - b.at).total_seconds()
            stale = age > poll_sec * 10 + 120
            state, lead = ("warn" if stale or state == "warn" else "ok"), f"{b.detail} ({age:.0f}s ago)"
        return HealthRow(label, state, lead + " | " + "; ".join(parts))

    def _summary(self, now: datetime) -> list[str]:
        s = self.app.trade_manager.last_summary
        if s is not None:
            return [f"Day P&L {s.day_pnl:+.2f}",
                    f"Closed: {', '.join(s.closed) or 'none'}",
                    f"Still open: {', '.join(f'{k} ({v})' for k, v in s.still_open.items()) or 'none'}"]
        try:
            today = self.app.pipeline.journal.list_candidates(trade_date=now.date().isoformat())
        except Exception:
            return []
        traded = [c for c in today if c.fills]
        pnl = round(sum(c.realized_pnl for c in traded), 2)
        decided = [c for c in today if c.human]
        approved = sum(1 for c in decided if c.human.decision == "approved")
        passed = sum(1 for c in decided if c.human.decision == "passed")
        expired = sum(1 for c in decided if c.human.decision == "expired")
        return [f"Day P&L {pnl:+.2f} on {len(traded)} trade(s)",
                f"Tickets: {approved} approved · {passed} passed · {expired} expired",
                f"Rejected by gates: {sum(1 for c in today if c.status is S.REJECTED)}"]

    # -- chart data -------------------------------------------------------------------------
    def _load_bars(self, c: Candidate) -> None:
        bars: list[dict[str, Any]] = []
        try:
            m = self.app.pipeline.market
            for src in (m.premarket_levels(c.ticker), m.session_levels(c.ticker)):
                for b in (src or {}).get("bars") or []:
                    ts = getattr(b, "timestamp", None)
                    bars.append({
                        "t": f"{ts.astimezone(ET):%H:%M}" if isinstance(ts, datetime) else "",
                        "o": float(b.open), "h": float(b.high), "l": float(b.low), "c": float(b.close),
                    })
        except Exception as exc:
            logger.debug("desk chart {} failed: {}", c.ticker, exc)
        with self._bars_lock:
            self._bars[c.id] = bars[-120:]


def countdown(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}:{s:02d}"


@dataclass
class ActionLog:
    """Recent desk actions and their results, newest first (shown under the status bar)."""

    lines: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, line: str) -> None:
        with self._lock:
            self.lines.insert(0, f"{now_et():%H:%M:%S} {line}")
            del self.lines[20:]

    def recent(self) -> list[str]:
        with self._lock:
            return list(self.lines)


def chart_svg(bars: list[dict[str, Any]], entry: float, stop: float, target: float,
              width: int = 720, height: int = 170) -> str:
    """One-minute candles with entry/stop/target lines, as a self-contained SVG (numbers only, no user text)."""
    if not bars:
        return ""
    levels = (("entry", entry, "#2196f3"), ("stop", stop, "#f44336"), ("target", target, "#4caf50"))
    lo = min(min(b["l"] for b in bars), stop)
    hi = max(max(b["h"] for b in bars), target)
    if hi <= lo:
        hi = lo + 0.01
    pad_r, pad_y = 78, 8
    plot_w, plot_h = width - pad_r, height - 2 * pad_y

    def y(v: float) -> float:
        return round(pad_y + (hi - v) / (hi - lo) * plot_h, 1)

    step = plot_w / len(bars)
    body_w = max(1.0, step * 0.6)
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
             f'xmlns="http://www.w3.org/2000/svg" role="img" style="display:block">']
    for i, b in enumerate(bars):
        cx = round(i * step + step / 2, 1)
        color = "#26a69a" if b["c"] >= b["o"] else "#ef5350"
        top, bot = y(max(b["o"], b["c"])), y(min(b["o"], b["c"]))
        parts.append(f'<line x1="{cx}" x2="{cx}" y1="{y(b["h"])}" y2="{y(b["l"])}" stroke="{color}"/>')
        parts.append(f'<rect x="{round(cx - body_w / 2, 1)}" y="{top}" width="{round(body_w, 1)}" '
                     f'height="{max(1.0, round(bot - top, 1))}" fill="{color}"/>')
    for name, v, color in levels:
        parts.append(f'<line x1="0" x2="{plot_w}" y1="{y(v)}" y2="{y(v)}" stroke="{color}" stroke-dasharray="5 4"/>')
        parts.append(f'<text x="{plot_w + 4}" y="{y(v) + 4}" fill="{color}" font-size="11" '
                     f'font-family="sans-serif">{name} {v:g}</text>')
    parts.append("</svg>")
    return "".join(parts)
