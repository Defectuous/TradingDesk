"""Approval path, Executor, Trade Manager and kill switch, against the dry-run broker."""
from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from tests.conftest import et
from tests.fakes import FakeMarket
from tradingdesk.agents.risk_officer import AccountState
from tradingdesk.alerts import NullNotifier
from tradingdesk.broker.dry_run import DryRunBroker
from tradingdesk.execution.executor import NotApproved
from tradingdesk.execution.trade import TradeState
from tradingdesk.journal import Journal
from tradingdesk.market import Quote
from tradingdesk.models import Agent, CandidateStatus, HumanDecision, IllegalTransition
from tradingdesk.runner import build_app

S = CandidateStatus
OPEN_NOW = et(2026, 11, 9, 9, 45)
PM_NOW = et(2026, 11, 9, 7, 15)


class Rig:
    def __init__(self, settings, tmp_path, regular: bool):
        self.market = FakeMarket()
        self.broker = DryRunBroker(cash=1000, regular_hours=regular)
        self.notifier = NullNotifier()
        self.journal = Journal(tmp_path / "j.db")
        self.app = build_app(settings, self.journal, market=self.market, broker=self.broker, notifier=self.notifier,
                             threaded_closes=False, sleep=lambda s: None)
        self.desk, self.tm = self.app.desk, self.app.trade_manager

    def price(self, t: str, p: float, bid: float | None = None, ask: float | None = None) -> None:
        bid = round(p - 0.01, 2) if bid is None else bid
        ask = round(p + 0.01, 2) if ask is None else ask
        self.market.quotes[t] = Quote(p, bid, ask)
        self.broker.set_quote(t, p, bid, ask)

    def ticket(self, now):
        p = self.app.pipeline
        [c] = [p.manager.process(h, now) for h in p.scout.scan(now)]
        assert c.status is S.AWAITING_HUMAN, c.rejection_reasons
        return c

    def submits(self) -> list[str]:
        return [x for x in self.broker.log if x.startswith("submit")]


@pytest.fixture
def open_rig(make_settings, tmp_path):
    r = Rig(make_settings(), tmp_path, regular=True)
    r.market.add("ABCD", price=4.95, pm_high=4.60, hod=5.00, quote=Quote(4.95, 4.94, 4.96))
    r.price("ABCD", 4.95)
    yield r
    r.journal.close()


@pytest.fixture
def pm_rig(make_settings, tmp_path):
    r = Rig(make_settings(), tmp_path, regular=False)
    r.market.add("ABCD")                       # price 4.20, premarket high 4.24
    r.price("ABCD", 4.20)
    yield r
    r.journal.close()


def approved(r: Rig, now, **kw):
    c = r.ticket(now)
    res = r.desk.approve(c.id, now=now + timedelta(seconds=5), **kw)
    assert res.ok, res.message
    return c, r.tm.get(c.id)


# ---------------------------------------------------------------------------------
# Approval path
# ---------------------------------------------------------------------------------

def test_no_order_without_approval(open_rig):
    c = open_rig.ticket(OPEN_NOW)
    open_rig.tm.tick(OPEN_NOW + timedelta(seconds=10))
    assert open_rig.submits() == []
    with pytest.raises(NotApproved):
        open_rig.app.executor.execute(c)
    assert open_rig.submits() == []


def test_approval_requires_click_time_recheck(open_rig):
    c = open_rig.ticket(OPEN_NOW)
    with pytest.raises(IllegalTransition):     # nobody can mark it approved without the re-check
        c.record_human(HumanDecision(decision="approved", shown_at=OPEN_NOW, decided_at=OPEN_NOW, approved_shares=1))
    assert open_rig.submits() == []


def test_approve_places_brackets(open_rig):
    c, trade = approved(open_rig, OPEN_NOW)
    assert c.status is S.HUMAN_APPROVED and trade.state is TradeState.WORKING
    subs = open_rig.submits()
    assert len(subs) == 6                                       # 2 legs x (entry + target + stop)
    assert sum("class=bracket" in s and "stop_limit" in s for s in subs) == 2
    assert [leg.qty for leg in trade.legs] == [20, 20]
    assert any(v.agent is Agent.RISK_RECHECK and v.decision == "pass" for v in c.verdicts)
    assert c.human.decision_seconds == 5.0


def test_approve_is_idempotent(open_rig):
    c = open_rig.ticket(OPEN_NOW)
    results = []
    threads = [threading.Thread(target=lambda: results.append(open_rig.desk.approve(c.id, now=OPEN_NOW)))
               for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({(r.ok, r.message) for r in results}) == 1 and results[0].ok
    assert len(open_rig.submits()) == 6                          # still just one trade's orders
    assert open_rig.desk.approve(c.id, now=OPEN_NOW).ok          # a later click: same answer, no orders
    assert len(open_rig.submits()) == 6
    open_rig.app.executor.execute(c)                             # even a direct second execute is a no-op
    assert len(open_rig.submits()) == 6


def test_expired_ticket_cannot_be_approved(open_rig):
    c = open_rig.ticket(OPEN_NOW)
    res = open_rig.desk.approve(c.id, now=OPEN_NOW + timedelta(seconds=91))
    assert not res.ok and "expired" in res.message
    assert c.status is S.EXPIRED and open_rig.submits() == []
    assert not open_rig.desk.approve(c.id, now=OPEN_NOW + timedelta(seconds=92)).ok
    assert open_rig.journal.get_candidate(c.id).status is S.EXPIRED


def test_ticket_expires_on_price_drift(open_rig):
    c = open_rig.ticket(OPEN_NOW)
    open_rig.price("ABCD", 5.20)                                 # >1.5% from entry 5.0
    [gone] = open_rig.desk.expire_stale(OPEN_NOW + timedelta(seconds=3))
    assert gone.id == c.id and c.status is S.EXPIRED and "moved" in c.human.note


def test_ttl_expiry_sweep(open_rig):
    c = open_rig.ticket(OPEN_NOW)
    assert open_rig.desk.expire_stale(OPEN_NOW + timedelta(seconds=30)) == []
    assert open_rig.desk.seconds_left(c, OPEN_NOW + timedelta(seconds=30)) == pytest.approx(60)
    assert [x.id for x in open_rig.desk.expire_stale(OPEN_NOW + timedelta(seconds=90))] == [c.id]


def test_recheck_blocks_when_daily_loss_hit(open_rig, monkeypatch):
    c = open_rig.ticket(OPEN_NOW)
    losing = AccountState(cash=1000, buying_power=1000, realized_pnl_today=-60)
    monkeypatch.setattr(open_rig.app.pipeline.accounts, "state", lambda now: losing)
    assert not open_rig.desk.approvals_enabled(OPEN_NOW)
    res = open_rig.desk.approve(c.id, now=OPEN_NOW)
    assert not res.ok and "daily loss" in res.message and open_rig.submits() == []


def test_recheck_rejects_when_conditions_changed(open_rig):
    c = open_rig.ticket(OPEN_NOW)
    open_rig.price("ABCD", 4.95, bid=4.80, ask=4.96)             # spread blew out after the ticket
    res = open_rig.desk.approve(c.id, now=OPEN_NOW + timedelta(seconds=5))
    assert not res.ok and "re-check failed" in res.message and "spread" in res.message
    assert c.status is S.REJECTED and c.rejected_by is Agent.RISK_RECHECK
    assert open_rig.submits() == []


def test_reduce_size_only(open_rig):
    c = open_rig.ticket(OPEN_NOW)
    assert not open_rig.desk.approve(c.id, shares=41, now=OPEN_NOW).ok       # larger refused
    assert c.status is S.AWAITING_HUMAN
    res = open_rig.desk.approve(c.id, shares=10, now=OPEN_NOW)
    assert res.ok and c.human.approved_shares == 10
    trade = open_rig.tm.get(c.id)
    assert sum(leg.qty for leg in trade.legs) == 10


def test_pass_with_note(open_rig):
    c = open_rig.ticket(OPEN_NOW)
    assert open_rig.desk.pass_ticket(c.id, note="too extended", now=OPEN_NOW + timedelta(seconds=7)).ok
    assert c.status is S.HUMAN_PASSED and c.human.note == "too extended" and c.human.decision_seconds == 7.0
    assert not open_rig.desk.approve(c.id, now=OPEN_NOW).ok and open_rig.submits() == []


def test_new_ticket_alert(open_rig):
    open_rig.ticket(OPEN_NOW)
    assert any("ABCD" in m and "R:R" in m and "127.0.0.1:8765" in m for m in open_rig.notifier.sent)


# ---------------------------------------------------------------------------------
# Trade management, regular hours
# ---------------------------------------------------------------------------------

def test_fill_breakeven_partial_trail_and_stop(open_rig):
    r = open_rig
    c, t = approved(r, OPEN_NOW)
    now = OPEN_NOW + timedelta(seconds=10)
    r.price("ABCD", 5.01, ask=5.02)                              # breakout triggers, fills within the limit
    r.tm.tick(now)
    assert c.status is S.FILLED and c.position_qty == 40 and t.state is TradeState.OPEN
    entry, R = t.entry, t.risk_per_share

    r.price("ABCD", round(entry + R + 0.02, 2))                  # +1R
    r.tm.tick(now)
    assert t.breakeven_done and t.stop == entry
    assert sum(1 for x in r.broker.log if x.startswith("replace")) == 2   # both legs' stops moved

    tgt1 = t.legs[0].target
    r.price("ABCD", tgt1 + 0.02)                                 # first target: leg A sells
    r.tm.tick(now)
    r.tm.tick(now)
    assert c.position_qty == 20 and t.legs[0].target_done
    r.price("ABCD", tgt1 + 0.30)                                 # runner keeps going: stop trails
    r.tm.tick(now)
    assert t.stop > entry

    r.price("ABCD", t.stop - 0.05)                               # trailing stop hit
    r.tm.tick(now)
    r.tm.tick(now)
    assert c.position_qty == 0 and c.status is S.CLOSED and t.state is TradeState.CLOSED
    assert c.realized_pnl > 0
    events = [e["event"] for e in r.journal.events(candidate_id=c.id)]
    assert {"execute", "fill", "stop_moved", "closed"} <= set(events)


def test_cutoff_cancels_unfilled_entries(open_rig):
    c, t = approved(open_rig, OPEN_NOW)
    open_rig.tm.tick(et(2026, 11, 9, 11, 0, 1))
    assert t.state is TradeState.EXPIRED and c.status is S.EXPIRED
    assert open_rig.broker.open_orders() == []


def test_partial_fills(open_rig):
    r = open_rig
    r.broker.fill_ratio = 0.5
    c, t = approved(r, OPEN_NOW)
    r.price("ABCD", 5.01, ask=5.02)
    for _ in range(8):
        r.tm.tick(OPEN_NOW + timedelta(seconds=20))
    assert c.position_qty == 40
    buys = [f for f in c.fills if f.side == "buy"]
    assert len(buys) > 2 and sum(f.qty for f in buys) == 40


def test_wind_down_closes_everything(open_rig):
    r = open_rig
    c, t = approved(r, OPEN_NOW)
    r.price("ABCD", 5.01, ask=5.02)
    r.tm.tick(OPEN_NOW + timedelta(seconds=10))
    assert c.position_qty == 40
    r.tm.tick(et(2026, 11, 9, 11, 30, 1))
    r.tm.tick(et(2026, 11, 9, 11, 30, 3))
    assert c.status is S.CLOSED and r.broker.positions() == [] and r.broker.open_orders() == []
    assert all(f.reason == "wind-down" for f in c.fills if f.side == "sell")
    assert r.tm.last_summary is not None and r.tm.last_summary.closed == ["ABCD"]
    assert any("Wind-down done" in m for m in r.notifier.sent)


def test_hold_mode_skips_wind_down(make_settings, tmp_path):
    r = Rig(make_settings(sessions={"wind_down_mode": "hold"}), tmp_path, regular=True)
    r.market.add("ABCD", price=4.95, pm_high=4.60, hod=5.00, quote=Quote(4.95, 4.94, 4.96))
    r.price("ABCD", 4.95)
    c, t = approved(r, OPEN_NOW)
    r.price("ABCD", 5.01, ask=5.02)
    r.tm.tick(OPEN_NOW)
    r.tm.tick(et(2026, 11, 9, 11, 45))
    assert c.position_qty == 40 and c.status is S.FILLED


def test_halted_position_is_stuck_then_closed(open_rig):
    r = open_rig
    c, t = approved(r, OPEN_NOW)
    r.price("ABCD", 5.01, ask=5.02)
    r.tm.tick(OPEN_NOW)
    r.broker.set_halted("ABCD")
    r.desk.flatten("ABCD")
    assert t.state is TradeState.STUCK_HALTED and c.position_qty == 40
    assert any("HALTED" in m for m in r.notifier.sent)
    r.broker.set_halted("ABCD", False)                            # halt lifts
    r.tm.tick(OPEN_NOW + timedelta(seconds=20))
    r.tm.tick(OPEN_NOW + timedelta(seconds=21))
    assert c.status is S.CLOSED and r.broker.positions() == []


# ---------------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------------

def test_kill_switch_cancels_and_flattens_everything(open_rig):
    r = open_rig
    c, t = approved(r, OPEN_NOW)
    r.price("ABCD", 5.01, ask=5.02)
    r.tm.tick(OPEN_NOW)
    assert c.position_qty == 40
    r.price("XYZ", 10.0)                                          # a position TradingDesk didn't open
    r.broker.submit_market("XYZ", "buy", 5, "manual-1")
    r.broker.submit_limit("XYZ", "buy", 5, 5.0, "manual-2")       # and a stray working order
    out = r.desk.kill_switch(OPEN_NOW + timedelta(seconds=30))
    assert out == {"ABCD": "flat", "XYZ": "flat"}
    assert r.broker.positions() == [] and r.broker.open_orders() == []
    assert c.status is S.CLOSED and all(f.reason == "kill-switch" for f in c.fills if f.side == "sell")


def test_kill_switch_expires_waiting_tickets(open_rig):
    c = open_rig.ticket(OPEN_NOW)
    open_rig.desk.kill_switch(OPEN_NOW + timedelta(seconds=2))
    assert c.status is S.EXPIRED and not open_rig.desk.approve(c.id, now=OPEN_NOW).ok
    assert open_rig.submits() == []


def test_close_cancels_legs_before_selling(open_rig):
    """Attached stop/target must be gone before the closing sell, or a leg could fire after and go short."""
    r = open_rig
    c, t = approved(r, OPEN_NOW)
    r.price("ABCD", 5.01, ask=5.02)
    r.tm.tick(OPEN_NOW)
    r.desk.flatten("ABCD")
    log = r.broker.log
    first_close = next(i for i, x in enumerate(log) if x.startswith("submit sell") and "class=simple" in x)
    cancels = [i for i, x in enumerate(log) if x.startswith("cancel")]
    assert cancels and max(cancels) < first_close


# ---------------------------------------------------------------------------------
# Premarket: software-managed stop, trigger, handoff
# ---------------------------------------------------------------------------------

def test_premarket_trade_is_software_managed(pm_rig):
    r = pm_rig
    c, t = approved(r, PM_NOW)
    assert t.state is TradeState.ARMED and t.software_managed and r.submits() == []
    assert c.risk_sizing.software_managed_stop

    r.price("ABCD", 4.25, ask=4.26)                               # crosses 4.24 entry
    r.tm.tick(PM_NOW + timedelta(seconds=10))
    r.tm.tick(PM_NOW + timedelta(seconds=11))
    assert c.position_qty == 23 and t.state is TradeState.OPEN
    assert all("ext=True" in s and "limit" in s for s in r.submits())   # extended hours: plain limits only

    r.price("ABCD", 4.05, bid=4.04)                               # under the 4.09 software stop
    r.tm.tick(PM_NOW + timedelta(seconds=20))
    r.tm.tick(PM_NOW + timedelta(seconds=21))
    assert c.position_qty == 0 and c.status is S.CLOSED
    assert all(f.reason == "stop" for f in c.fills if f.side == "sell")


def test_premarket_entry_timeout_rearms_then_expires(pm_rig):
    r = pm_rig
    c, t = approved(r, PM_NOW)
    r.price("ABCD", 4.25, bid=4.24, ask=4.30)                     # triggers but ask is above our limit
    now = PM_NOW + timedelta(seconds=10)
    r.tm.tick(now)
    assert t.state is TradeState.WORKING
    r.tm.tick(now + timedelta(seconds=31))                        # timeout -> re-armed
    assert t.state is TradeState.ARMED
    r.tm.tick(now + timedelta(seconds=32))                        # attempt 2
    r.tm.tick(now + timedelta(seconds=70))                        # timeout again -> out of attempts
    assert t.state is TradeState.EXPIRED and c.status is S.EXPIRED


def test_premarket_handoff_at_open(pm_rig):
    r = pm_rig
    c, t = approved(r, PM_NOW)
    r.price("ABCD", 4.25, ask=4.26)
    r.tm.tick(PM_NOW + timedelta(seconds=10))
    r.tm.tick(PM_NOW + timedelta(seconds=11))
    assert c.position_qty == 23
    r.broker.set_regular_hours(True)                              # 9:30
    r.tm.tick(et(2026, 11, 9, 9, 30, 5))
    assert not t.software_managed
    ocos = [s for s in r.submits() if "class=oco" in s]
    assert len(ocos) == 4                                         # 2 legs x (target + stop)


def test_premarket_software_target(pm_rig):
    r = pm_rig
    c, t = approved(r, PM_NOW)
    r.price("ABCD", 4.25, ask=4.26)
    r.tm.tick(PM_NOW + timedelta(seconds=10))
    r.tm.tick(PM_NOW + timedelta(seconds=11))
    r.price("ABCD", t.legs[0].target + 0.03)
    r.tm.tick(PM_NOW + timedelta(seconds=20))
    r.tm.tick(PM_NOW + timedelta(seconds=21))
    assert t.legs[0].target_done and c.position_qty == t.legs[1].qty


def test_armed_premarket_trade_converts_to_bracket_at_open(pm_rig):
    r = pm_rig
    c, t = approved(r, PM_NOW)
    r.broker.set_regular_hours(True)
    r.tm.tick(et(2026, 11, 9, 9, 30, 5))
    assert t.state is TradeState.WORKING
    assert sum("class=bracket" in s and "stop_limit" in s for s in r.submits()) == 2   # 2 entries (+4 legs)
