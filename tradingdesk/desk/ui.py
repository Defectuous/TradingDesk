"""
The Trade Desk page (NiceGUI). Renders DeskView snapshots and calls DeskService.

No trading rules live here. Approve / Pass / size / Flatten / Kill switch all go
through DeskService, which re-checks risk, refuses to widen size and is
idempotent. The UI only disables buttons to make that obvious.

Elements are built once and then updated in place every second, so a note or
size you're typing isn't wiped by the live refresh.
"""
from __future__ import annotations

from typing import Any, Callable

from loguru import logger
from nicegui import run, ui

from tradingdesk.desk.service import DeskResult, DeskService
from tradingdesk.desk.view import (ActionLog, DeskSnapshot, DeskView, PositionView, TicketView, chart_svg,
                                   countdown)

REFRESH_SEC = 1.0

_HEALTH_COLOR = {"ok": "positive", "warn": "warning", "error": "negative", "off": "grey"}

# Two short tones. Browsers only allow sound after a click on the page, so the
# head script unlocks the AudioContext on the first click anywhere.
_HEAD = """
<script>
window.__tdAudioUnlock = function () {
  const C = window.AudioContext || window.webkitAudioContext;
  if (!C) return null;
  window.__tdAudio = window.__tdAudio || new C();
  window.__tdAudio.resume();
  return window.__tdAudio;
};
document.addEventListener('click', window.__tdAudioUnlock, {once: false, passive: true});
window.__tdBeep = function () {
  const a = window.__tdAudioUnlock();
  if (!a) return;
  [[0, 880], [0.18, 1320]].forEach(([d, f]) => {
    const o = a.createOscillator(), g = a.createGain();
    o.frequency.value = f; o.connect(g); g.connect(a.destination);
    g.gain.setValueAtTime(0.18, a.currentTime + d);
    g.gain.exponentialRampToValueAtTime(0.001, a.currentTime + d + 0.15);
    o.start(a.currentTime + d); o.stop(a.currentTime + d + 0.16);
  });
};
</script>
<style>
  .td-mono { font-variant-numeric: tabular-nums; }
  .td-ticket { width: 100%; max-width: 760px; }
  .td-k { opacity: .65; font-size: .8rem; }
</style>
"""


def _money(x: float | None, sign: bool = False) -> str:
    if x is None:
        return "—"
    return f"{x:+,.2f}" if sign else f"{x:,.2f}"


def _price(x: float | None) -> str:
    return "—" if x is None else (f"{x:,.4f}" if x < 1 else f"{x:,.2f}")


def _float(n: int | None) -> str:
    if n is None:
        return "?"
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}K"


class DeskPage:
    """One browser tab. Holds the elements so updates don't rebuild them."""

    def __init__(self, view: DeskView, desk: DeskService, log: ActionLog, alert_sound: bool) -> None:
        self.view = view
        self.desk = desk
        self.log = log
        self.sound = alert_sound
        self.tickets: dict[str, TicketCard] = {}
        self.positions: dict[str, PositionRow] = {}
        self.rejected_ids: tuple[str, ...] | None = None
        self.log_lines: list[str] | None = None
        self.seen_ticket_ids: set[str] | None = None     # None until the first refresh (no beep on load)
        self.approvals_ok = False
        self.health_sig: tuple | None = None
        self.title_count: int | None = None
        self._busy = False
        self._build()

    # -- layout -----------------------------------------------------------------------------
    def _build(self) -> None:
        ui.add_head_html(_HEAD)
        with ui.header().classes("items-center gap-x-6 gap-y-1 flex-wrap py-2 px-4"):
            with ui.column().classes("gap-0"):
                self.phase_lbl = ui.label("…").classes("text-lg font-bold")
                self.next_lbl = ui.label("").classes("text-xs td-mono")
            with ui.column().classes("gap-0"):
                ui.label("Day P&L / max loss").classes("td-k")
                self.pnl_lbl = ui.label("—").classes("td-mono")
                self.pnl_bar = ui.linear_progress(value=0, show_value=False).props("color=negative").classes("w-32")
            with ui.column().classes("gap-0"):
                ui.label("Trades").classes("td-k")
                self.trades_lbl = ui.label("—").classes("td-mono")
            with ui.column().classes("gap-0"):
                ui.label("LLM spend").classes("td-k")
                self.llm_lbl = ui.label("—").classes("td-mono")
            with ui.column().classes("gap-0"):
                ui.label("Broker").classes("td-k")
                self.broker_lbl = ui.label("—")
            ui.space()
            with ui.row().classes("items-center gap-3 no-wrap"):
                ui.switch("Sound", value=self.sound, on_change=self._toggle_sound).props("dense color=white")
                ui.button("FLATTEN ALL / KILL", on_click=self._confirm_kill, icon="dangerous") \
                    .props("color=negative unelevated").classes("font-bold").mark("kill")
            self.health_row = ui.row().classes("w-full gap-1 flex-wrap")

        self.banner = ui.label("").classes("w-full text-center font-bold py-1 rounded").mark("banner")
        self.banner.set_visibility(False)
        self.summary_card = ui.card().classes("w-full")
        self.summary_card.set_visibility(False)

        with ui.row().classes("w-full items-start gap-4 no-wrap max-lg:flex-wrap"):
            with ui.column().classes("grow gap-3 min-w-0"):
                ui.label("Tickets").classes("text-xl font-bold")
                self.empty_lbl = ui.label("No tickets waiting.").classes("opacity-60")
                self.ticket_col = ui.column().classes("w-full gap-3")
            with ui.column().classes("w-full lg:w-[480px] gap-3 shrink-0"):
                ui.label("Open positions").classes("text-xl font-bold")
                self.no_pos_lbl = ui.label("Flat.").classes("opacity-60")
                self.pos_col = ui.column().classes("w-full gap-2")
                self.rejected_exp = ui.expansion("Rejected today (0)", icon="block").classes("w-full")
                with self.rejected_exp:
                    self.rejected_col = ui.column().classes("w-full gap-1")
                self.log_exp = ui.expansion("Desk log", icon="history").classes("w-full")
                with self.log_exp:
                    self.log_col = ui.column().classes("w-full gap-0 td-mono text-xs")

        self.timer = ui.timer(REFRESH_SEC, self.refresh)

    # -- refresh ------------------------------------------------------------------------------
    async def refresh(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            snap: DeskSnapshot = await run.io_bound(self.view.snapshot)
            self.apply(snap)
        except Exception:
            logger.exception("desk refresh failed")
        finally:
            self._busy = False

    def apply(self, snap: DeskSnapshot) -> None:
        self._status(snap)
        self._tickets(snap)
        self._positions(snap)
        self._rejected(snap)
        self._log()

    def _status(self, snap: DeskSnapshot) -> None:
        s = snap.status
        self.approvals_ok = s.approvals_enabled and s.accepting_new_trades
        self.phase_lbl.set_text(f"{s.phase}" + ("" if s.accepting_new_trades else " · no new trades"))
        self.next_lbl.set_text(f"{s.next_phase} in {countdown(s.seconds_to_next)}")
        self.pnl_lbl.set_text(f"{_money(s.day_pnl, True)} / -{_money(s.max_daily_loss)}")
        self.pnl_bar.set_value(min(1.0, max(0.0, -s.day_pnl / s.max_daily_loss)) if s.max_daily_loss else 1.0)
        self.trades_lbl.set_text(f"{s.trades_today} / {s.max_trades}")
        self.llm_lbl.set_text(f"${s.llm_spend:.2f} / ${s.llm_cap:.2f}")
        self.broker_lbl.set_text(s.broker)

        sig = tuple((h.name, h.state, h.detail if h.state != "ok" else "") for h in s.health)
        if sig != self.health_sig:
            self.health_sig = sig
            self.health_row.clear()
            with self.health_row:
                for h in s.health:
                    ui.badge(h.name, color=_HEALTH_COLOR.get(h.state, "grey")).tooltip(f"{h.state}: {h.detail}")

        if not s.approvals_enabled:
            msg, color = ("Daily loss limit hit — approvals disabled for the rest of the day"
                          if not s.error else f"Account unknown ({s.error}) — approvals disabled"), "bg-negative"
        elif s.session_ended:
            msg, color = "Session ended", "bg-grey-7"
        elif not s.accepting_new_trades:
            msg, color = f"{s.phase}: no new trades. Open positions are still managed.", "bg-grey-7"
        elif s.error:
            msg, color = s.error, "bg-warning"
        else:
            msg, color = "", ""
        self.banner.set_text(msg)
        self.banner.classes(replace=f"w-full text-center font-bold py-1 rounded text-white {color}")
        self.banner.set_visibility(bool(msg))

        self.summary_card.set_visibility(s.session_ended)
        if s.session_ended:
            self.summary_card.clear()
            with self.summary_card:
                ui.label("Day summary").classes("font-bold")
                for line in s.summary:
                    ui.label(line)

        n = len(snap.tickets)
        if n != self.title_count:
            self.title_count = n
            ui.run_javascript(f"document.title = '{f'({n}) ' if n else ''}Trade Desk'")

    def _tickets(self, snap: DeskSnapshot) -> None:
        live = {t.id: t for t in snap.tickets}
        for tid in list(self.tickets):
            if tid not in live:
                self.tickets.pop(tid).finish()
        new = [t for t in snap.tickets if t.id not in self.tickets]
        for t in new:
            with self.ticket_col:
                self.tickets[t.id] = TicketCard(self, t)
        if self.seen_ticket_ids is not None and any(t.id not in self.seen_ticket_ids for t in new) and self.sound:
            ui.run_javascript("window.__tdBeep && window.__tdBeep()")
        self.seen_ticket_ids = (self.seen_ticket_ids or set()) | set(live)
        for t in snap.tickets:
            self.tickets[t.id].update(t, self.approvals_ok)
        self.empty_lbl.set_visibility(not snap.tickets)

    def _positions(self, snap: DeskSnapshot) -> None:
        live = {p.key: p for p in snap.positions}
        for key in list(self.positions):
            if key not in live:
                self.positions.pop(key).row.delete()
        for p in snap.positions:
            if p.key not in self.positions:
                with self.pos_col:
                    self.positions[p.key] = PositionRow(self, p)
            self.positions[p.key].update(p)
        self.no_pos_lbl.set_visibility(not snap.positions)

    def _rejected(self, snap: DeskSnapshot) -> None:
        ids = tuple(r.id for r in snap.rejected)
        if ids == self.rejected_ids:
            return
        self.rejected_ids = ids
        self.rejected_exp.text = f"Rejected today ({len(ids)})"
        self.rejected_exp.update()
        self.rejected_col.clear()
        with self.rejected_col:
            for r in snap.rejected:
                with ui.row().classes("w-full gap-2 items-baseline no-wrap"):
                    ui.label(r.at).classes("td-mono text-xs opacity-60")
                    ui.label(r.ticker).classes("font-bold")
                    ui.badge(r.rejected_by, color="grey-7")
                    ui.label("; ".join(r.reasons)).classes("text-sm")

    def _log(self) -> None:
        lines = self.log.recent()
        if lines == self.log_lines:
            return
        self.log_lines = lines
        self.log_col.clear()
        with self.log_col:
            for line in lines:
                ui.label(line)

    # -- actions --------------------------------------------------------------------------------
    def _toggle_sound(self, e: Any) -> None:
        self.sound = bool(e.value)
        if self.sound:
            ui.run_javascript("window.__tdAudioUnlock && window.__tdAudioUnlock()")

    def report(self, what: str, res: DeskResult | str, ok: bool | None = None) -> None:
        msg = res.message if isinstance(res, DeskResult) else res
        ok = res.ok if isinstance(res, DeskResult) else (True if ok is None else ok)
        self.log.add(f"{what}: {msg}")
        ui.notify(f"{what}: {msg}", type="positive" if ok else "negative", position="top", timeout=6000)

    def _confirm_kill(self) -> None:
        with ui.dialog() as dlg, ui.card():
            ui.label("Kill switch").classes("text-lg font-bold")
            ui.label("Expire every waiting ticket, cancel ALL open orders and close ALL positions?")
            with ui.row():
                ui.button("Cancel", on_click=dlg.close).props("flat")
                ui.button("FLATTEN ALL", on_click=lambda: self._kill(dlg)).props("color=negative").mark("kill-confirm")
        dlg.open()

    async def _kill(self, dlg: Any) -> None:
        dlg.close()
        ui.notify("Kill switch: closing everything…", type="warning", position="top")
        try:
            out = await run.io_bound(self.desk.kill_switch)
        except Exception as exc:
            logger.exception("kill switch failed")
            self.report("KILL SWITCH", f"failed: {exc}", ok=False)
            return
        still = {k: v for k, v in out.items() if v != "flat"}
        self.report("KILL SWITCH", ", ".join(f"{k} {v}" for k, v in out.items()) or "nothing was open",
                    ok=not still)

    def confirm_flatten(self, ticker: str) -> None:
        with ui.dialog() as dlg, ui.card():
            ui.label(f"Flatten {ticker}?").classes("text-lg font-bold")
            ui.label("Cancels its orders and closes the position now.")
            with ui.row():
                ui.button("Cancel", on_click=dlg.close).props("flat")
                ui.button(f"Flatten {ticker}", on_click=lambda: self._flatten(dlg, ticker)).props("color=negative").mark(f"flatten-confirm:{ticker}")
        dlg.open()

    async def _flatten(self, dlg: Any, ticker: str) -> None:
        dlg.close()
        try:
            msg = await run.io_bound(self.desk.flatten, ticker)
        except Exception as exc:
            logger.exception("flatten failed")
            self.report(f"Flatten {ticker}", f"failed: {exc}", ok=False)
            return
        self.report(f"Flatten {ticker}", msg, ok=msg == "flat")


class TicketCard:
    def __init__(self, page: DeskPage, t: TicketView) -> None:
        self.page = page
        self.t = t
        self.done = False
        self.chart_done = False
        with ui.card().classes("td-ticket") as self.card:
            self._header(t)
            self._body(t)
            self._actions(t)

    # -- build -------------------------------------------------------------------------------
    def _header(self, t: TicketView) -> None:
        with ui.row().classes("w-full items-center gap-2"):
            ui.label(t.ticker).classes("text-2xl font-bold")
            if t.premarket:
                ui.badge("PREMARKET", color="deep-orange")
            grade_color = {"A": "positive", "B": "light-green", "C": "amber", "D": "orange", "F": "negative"}
            ui.badge(t.catalyst_badge, color=grade_color.get(t.catalyst_grade or "", "grey"))
            if t.software_managed_stop:
                ui.badge("stop is software-managed", color="warning").tooltip(
                    "The broker can't hold this stop in extended hours; the Trade Manager watches it.")
            ui.space()
            self.countdown_lbl = ui.label("").classes("td-mono font-bold")
        self.ttl_bar = ui.linear_progress(value=1.0, show_value=False).classes("w-full")
        if t.grade_reason:
            ui.label(t.grade_reason).classes("text-sm italic")

    def _body(self, t: TicketView) -> None:
        with ui.row().classes("w-full gap-6 flex-wrap td-mono"):
            for k, v in (("Price", _price(t.price)), ("Change", f"{t.change_pct:+.1f}%"),
                         ("RVol", f"{t.relative_volume:.1f}x"), ("Float", _float(t.float_shares)),
                         ("Spread", "—" if t.spread_pct is None else f"{t.spread_pct:.2f}%")):
                with ui.column().classes("gap-0"):
                    ui.label(k).classes("td-k")
                    ui.label(v)
            with ui.column().classes("gap-0"):
                ui.label("Live").classes("td-k")
                self.live_lbl = ui.label("—")

        with ui.row().classes("w-full gap-1 flex-wrap"):
            for p in t.pillars:
                ui.badge(("✓ " if p.passed else "✗ ") + p.name, color="positive" if p.passed else "negative") \
                    .props("outline").tooltip(p.detail or p.name)

        with ui.row().classes("w-full gap-6 flex-wrap td-mono items-end"):
            for k, v in (("Entry", _price(t.entry)), ("Stop", _price(t.stop)),
                         ("Target", _price(t.target) + (f" / {_price(t.target_2)}" if t.target_2 else "")),
                         ("R:R", f"{t.risk_reward:.2f}"), ("Risk/sh", _price(t.risk_per_share))):
                with ui.column().classes("gap-0"):
                    ui.label(k).classes("td-k")
                    ui.label(v).classes("font-bold")
            with ui.column().classes("gap-0"):
                ui.label("$ at risk").classes("td-k")
                self.risk_lbl = ui.label(_money(t.dollars_at_risk)).classes("font-bold")
            with ui.column().classes("gap-0"):
                ui.label("Position").classes("td-k")
                self.value_lbl = ui.label(_money(t.position_value))

        if t.headlines or t.filing_flags:
            with ui.column().classes("w-full gap-0"):
                for h in t.headlines:
                    with ui.row().classes("gap-2 items-baseline no-wrap"):
                        ui.badge(h.grade or "?", color="grey-7")
                        (ui.link(h.title, h.url, new_tab=True) if h.url else ui.label(h.title)).classes("text-sm")
                        ui.label(f"{h.published} · {', '.join(h.sources)}").classes("text-xs opacity-60")
                if t.filing_flags:
                    with ui.row().classes("gap-1"):
                        for f in t.filing_flags:
                            ui.badge(f, color="negative" if f in ("offering", "dilution") else "grey-7")
        with ui.row().classes("w-full gap-6 text-sm"):
            ui.label(f"Buzz: {t.buzz}")
            ui.label(f"Regime: {t.regime}")
            if t.strategy:
                ui.label(f"Setup: {t.strategy}").classes("opacity-70")

        with ui.column().classes("w-full gap-0"):
            ui.label("Skeptic's case against").classes("td-k")
            for o in t.objections or ("(none recorded)",):
                ui.label("• " + o).classes("text-sm")

        self.chart_box = ui.column().classes("w-full")
        with self.chart_box:
            ui.label("chart loading…").classes("text-xs opacity-50")

    def _actions(self, t: TicketView) -> None:
        with ui.row().classes("w-full items-center gap-2 flex-wrap"):
            self.size = ui.number("Shares (reduce only)", value=t.shares, min=1, max=t.shares, step=1,
                                  precision=0, on_change=self._size_changed).classes("w-40").mark(f"size:{t.ticker}")
            self.note = ui.input("Note (optional)", validation={"max 200 chars": lambda v: len(v or "") <= 200}) \
                .classes("grow").mark(f"note:{t.ticker}")
            self.approve_btn = ui.button("Approve", on_click=self._confirm_approve, icon="check") \
                .props("color=positive unelevated").mark(f"approve:{t.ticker}")
            self.pass_btn = ui.button("Pass", on_click=self._pass, icon="close").props("color=grey-8 unelevated").mark(f"pass:{t.ticker}")
        self.result_lbl = ui.label("").classes("text-sm font-bold").mark(f"result:{t.ticker}")

    # -- updates ------------------------------------------------------------------------------
    def requested_shares(self) -> int | None:
        try:
            n = int(self.size.value)
        except (TypeError, ValueError):
            return None
        return n if 1 <= n <= self.t.shares else None

    def _size_changed(self, _: Any = None) -> None:
        n = self.requested_shares()
        if n is None:
            self.risk_lbl.set_text("invalid size")
            return
        self.risk_lbl.set_text(_money(n * self.t.risk_per_share))
        self.value_lbl.set_text(_money(n * self.t.entry))

    def update(self, t: TicketView, approvals_ok: bool) -> None:
        self.t = t
        self.countdown_lbl.set_text(f"{countdown(t.seconds_left)} left")
        self.ttl_bar.set_value(t.seconds_left / t.ttl if t.ttl else 0)
        if t.live_price is not None:
            warn = t.drift_pct is not None and abs(t.drift_pct) > t.max_drift_pct * 0.66
            self.live_lbl.set_text(f"{_price(t.live_price)} ({t.drift_pct:+.2f}% vs entry)")
            self.live_lbl.classes(replace="text-warning font-bold" if warn else "")
        if not self.done:
            self.approve_btn.set_enabled(approvals_ok)
        if not self.chart_done:
            bars = self.page.view.chart(t.id)
            if bars is not None:
                self.chart_done = True
                self._draw_chart(bars)

    def _draw_chart(self, bars: list[dict[str, Any]]) -> None:
        # Inline SVG: no chart library to load into the page, and nothing that can stall the live refresh.
        self.chart_box.clear()
        svg = chart_svg(bars, self.t.entry, self.t.stop, self.t.target)
        if svg:
            with self.chart_box:
                ui.html(svg, sanitize=False).classes("w-full")   # numbers only, built server-side

    def _lock(self, text: str) -> None:
        self.done = True
        for el in (self.approve_btn, self.pass_btn, self.size, self.note):
            el.disable()
        self.result_lbl.set_text(text)

    def _unlock(self) -> None:
        self.done = False
        for el in (self.approve_btn, self.pass_btn, self.size, self.note):
            el.enable()

    def _confirm_approve(self) -> None:
        """Approve opens a summary; nothing is sent until Confirm. The Risk Officer re-check runs on Confirm."""
        if self.done:
            return
        shares = self.requested_shares()
        if shares is None:
            self.page.report(f"Approve {self.t.ticker}", f"size must be 1–{self.t.shares}", ok=False)
            return
        t = self.t
        with ui.dialog() as dlg, ui.card().classes("min-w-[320px]"):
            ui.label(f"Buy {shares} {t.ticker}?").classes("text-lg font-bold")
            if t.premarket:
                ui.badge("PREMARKET", color="deep-orange")
            with ui.grid(columns=2).classes("gap-x-6 gap-y-1 td-mono"):
                for k, v in (("Entry", _price(t.entry)), ("Stop", _price(t.stop)),
                             ("Target", _price(t.target)), ("R:R", f"{t.risk_reward:.2f}"),
                             ("$ at risk", _money(shares * t.risk_per_share)),
                             ("Position", _money(shares * t.entry))):
                    ui.label(k).classes("td-k")
                    ui.label(v).classes("font-bold")
            if t.software_managed_stop:
                ui.label("Stop is software-managed (the broker can't hold it in extended hours).") \
                    .classes("text-sm text-warning")
            ui.label("Risk is re-checked against the live price when you confirm.").classes("text-xs opacity-70")
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=dlg.close).props("flat").mark(f"approve-cancel:{t.ticker}")
                ui.button(f"Confirm buy {shares}", on_click=lambda: self._approve(dlg, shares), icon="check") \
                    .props("color=positive unelevated").mark(f"approve-confirm:{t.ticker}")
        dlg.open()

    async def _approve(self, dlg: Any, shares: int) -> None:
        dlg.close()
        if self.done:                          # second click on Confirm, or ticket already decided
            return
        self._lock("sending…")
        res = await run.io_bound(self.page.desk.approve, self.t.id, shares, self.note.value or None)
        self.page.report(f"Approve {self.t.ticker}", res)
        if res.ok or res.status != "awaiting_human":
            self._lock(("✓ " if res.ok else "✗ ") + res.message)
        else:
            self._unlock()                     # e.g. bad size: ticket still waiting, try again
            self.result_lbl.set_text("✗ " + res.message)

    async def _pass(self) -> None:
        if self.done:
            return
        self._lock("passing…")
        res = await run.io_bound(self.page.desk.pass_ticket, self.t.id, self.note.value or None)
        self.page.report(f"Pass {self.t.ticker}", res)
        self._lock(("passed" if res.ok else "✗ " + res.message))

    def finish(self) -> None:
        """Ticket left the queue (decided, expired, rejected at re-check). Show it briefly, then remove."""
        if not self.done:
            c = self.page.desk.tickets.get(self.t.id)
            status = c.status.value if c else "gone"
            why = c.human.note if c and c.human and c.human.decision == "expired" and c.human.note else ""
            self._lock(f"{status.replace('_', ' ')}" + (f": {why}" if why else ""))
        self.countdown_lbl.set_text("")
        if not self.chart_done:
            self.chart_done = True
            self.chart_box.clear()
        self.card.classes(add="opacity-50")
        ui.timer(8.0, self.card.delete, once=True)


class PositionRow:
    def __init__(self, page: DeskPage, p: PositionView) -> None:
        self.page = page
        with ui.card().classes("w-full p-3") as self.row:
            with ui.row().classes("w-full items-center gap-2"):
                ui.label(p.ticker).classes("text-lg font-bold")
                self.state = ui.badge(p.state, color="primary")
                self.halted = ui.badge("stuck — halted", color="negative")
                self.soft = ui.badge("stop is software-managed", color="warning")
                if p.untracked:
                    ui.badge("not opened by TradingDesk", color="grey-7")
                ui.space()
                ui.button("Flatten", on_click=lambda: page.confirm_flatten(p.ticker)) \
                    .props("color=negative outline dense").mark(f"flatten:{p.ticker}")
            with ui.row().classes("w-full gap-5 td-mono text-sm"):
                self.qty = self._kv("Qty")
                self.avg = self._kv("Avg")
                self.px = self._kv("Last")
                self.upl = self._kv("Open P&L")
                self.rpl = self._kv("Realized")
                self.stop = self._kv("Stop")
                self.targets = self._kv("Targets")

    @staticmethod
    def _kv(k: str) -> Any:
        with ui.column().classes("gap-0"):
            ui.label(k).classes("td-k")
            return ui.label("—")

    def update(self, p: PositionView) -> None:
        self.state.set_text(p.state.replace("_", " "))
        self.halted.set_visibility(p.stuck_halted)
        self.soft.set_visibility(p.software_managed)
        self.qty.set_text(str(p.qty))
        self.avg.set_text(_price(p.avg_entry))
        self.px.set_text(_price(p.price))
        self.upl.set_text(_money(p.unrealized_pl, True))
        self.upl.classes(replace="text-positive" if (p.unrealized_pl or 0) > 0 else
                         "text-negative" if (p.unrealized_pl or 0) < 0 else "")
        self.rpl.set_text(_money(p.realized_pl, True))
        self.stop.set_text(_price(p.stop))
        self.targets.set_text(" / ".join(_price(x) for x in p.targets) or "—")


def build_page(view: DeskView, desk: DeskService, log: ActionLog, alert_sound: bool = True) -> DeskPage:
    return DeskPage(view, desk, log, alert_sound)


def page_factory(view: DeskView, desk: DeskService, log: ActionLog, alert_sound: bool) -> Callable[[], None]:
    def root() -> None:
        build_page(view, desk, log, alert_sound)
    return root
