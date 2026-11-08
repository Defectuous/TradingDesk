"""Trade Desk view model: what each panel shows, built from a running App (dry-run broker)."""
from __future__ import annotations

from datetime import timedelta

import pytest

from tests.conftest import et
from tests.test_execution import OPEN_NOW, PM_NOW, Rig, approved
from tradingdesk.agents.risk_officer import AccountState
from tradingdesk.desk.view import DeskView, chart_svg, countdown
from tradingdesk.execution.trade import TradeState
from tradingdesk.llm.costs import LLMUsage
from tradingdesk.market import Quote
from tradingdesk.models import CandidateStatus

S = CandidateStatus


@pytest.fixture
def rig(make_settings, tmp_path):
    r = Rig(make_settings(), tmp_path, regular=True)
    r.market.add("ABCD", price=4.95, pm_high=4.60, hod=5.00, quote=Quote(4.95, 4.94, 4.96))
    r.price("ABCD", 4.95)
    r.view = DeskView(r.app, snapshot_ttl=0, slow_ttl=0)
    yield r
    r.journal.close()


@pytest.fixture
def pm(make_settings, tmp_path):
    r = Rig(make_settings(), tmp_path, regular=False)
    r.market.add("ABCD")
    r.price("ABCD", 4.20)
    r.view = DeskView(r.app, snapshot_ttl=0, slow_ttl=0)
    yield r
    r.journal.close()


def test_ticket_shows_everything(rig):
    c = rig.ticket(OPEN_NOW)
    snap = rig.view.build(OPEN_NOW + timedelta(seconds=10))
    [t] = snap.tickets
    assert t.id == c.id and t.ticker == "ABCD" and not t.premarket
    assert t.entry == c.proposed_trade.entry and t.stop == c.proposed_trade.stop
    assert t.shares == c.risk_sizing.shares and t.dollars_at_risk == c.risk_sizing.dollars_at_risk
    assert t.risk_reward == c.proposed_trade.risk_reward
    assert {p.name for p in t.pillars} == set(c.scout.pillars)
    assert t.objections and "stub" in t.objections[0]          # the Skeptic's case, even on a pass
    assert t.catalyst_badge.startswith("Catalyst pending")       # no Newsie grade cached
    assert t.seconds_left == pytest.approx(80) and t.ttl == 90
    assert t.live_price == 4.95 and t.drift_pct is not None


def test_premarket_ticket_flags_software_stop(pm):
    pm.ticket(PM_NOW)
    [t] = pm.view.build(PM_NOW).tickets
    assert t.premarket and t.software_managed_stop


def test_decided_ticket_leaves_queue(rig):
    c = rig.ticket(OPEN_NOW)
    rig.desk.pass_ticket(c.id, now=OPEN_NOW + timedelta(seconds=3))
    assert rig.view.build(OPEN_NOW + timedelta(seconds=4)).tickets == ()


def test_status_bar(rig):
    rig.journal.log_llm_usage(LLMUsage(agent="newsie", model="claude-sonnet-5", input_tokens=10, output_tokens=10,
                                       cost_usd=0.25, at=OPEN_NOW))
    s = rig.view.build(OPEN_NOW).status
    assert s.phase == "Open" and s.next_phase == "Wind-down" and s.accepting_new_trades
    assert s.seconds_to_next == pytest.approx(75 * 60)
    assert s.approvals_enabled and s.max_daily_loss == 60.0      # 6% of $1,000
    assert s.trades_today == 0 and s.max_trades == 5
    assert s.llm_spend == pytest.approx(0.25) and s.llm_cap == 2.0
    assert s.broker.endswith("(dry-run)")
    names = {h.name: h.state for h in s.health}
    assert names["Broker"] == "ok" and names["Newsie"] == "warn" and names["Discord"] == "off"   # no pass yet


def test_loss_limit_disables_approvals(rig, monkeypatch):
    losing = AccountState(cash=1000, buying_power=1000, realized_pnl_today=-60)
    monkeypatch.setattr(rig.app.pipeline.accounts, "state", lambda now: losing)
    s = rig.view.build(OPEN_NOW).status
    assert not s.approvals_enabled and s.day_pnl == -60


def test_unknown_account_fails_closed(rig, monkeypatch):
    def boom(now):
        raise RuntimeError("alpaca down")
    monkeypatch.setattr(rig.app.pipeline.accounts, "state", boom)
    s = rig.view.build(OPEN_NOW).status
    assert not s.approvals_enabled and "alpaca down" in s.error


def test_health_rows(rig):
    h = rig.app.health
    s = rig.view.build(OPEN_NOW).status
    rows = {r.name: r for r in s.health}
    assert rows["Scout / market data"].state == "warn"           # no pass yet
    h.ok("scout", now=OPEN_NOW)
    h.error("trade_manager", "boom", now=OPEN_NOW)
    rows = {r.name: r for r in rig.view.build(OPEN_NOW + timedelta(seconds=5)).status.health}
    assert rows["Scout / market data"].state == "ok"
    assert rows["Trade Manager"].state == "error" and rows["Trade Manager"].detail == "boom"
    rows = {r.name: r for r in rig.view.build(OPEN_NOW + timedelta(minutes=5)).status.health}
    assert rows["Scout / market data"].state == "warn"           # stale
    rows = {r.name: r for r in rig.view.build(et(2026, 11, 9, 12, 0)).status.health}
    assert rows["Scout / market data"].state == "off"            # outside the trading window


def test_positions_panel(rig):
    c, t = approved(rig, OPEN_NOW)
    [p] = rig.view.build(OPEN_NOW + timedelta(seconds=6)).positions
    assert p.ticker == "ABCD" and p.state == "working" and p.qty == 0 and not p.untracked
    rig.price("ABCD", 5.01, ask=5.02)
    rig.tm.tick(OPEN_NOW + timedelta(seconds=10))
    [p] = rig.view.build(OPEN_NOW + timedelta(seconds=11)).positions
    assert p.state == "open" and p.qty == c.position_qty and p.stop == t.stop
    assert p.targets == tuple(sorted({leg.target for leg in t.legs}))
    assert p.unrealized_pl is not None and p.price is not None
    t.state = TradeState.STUCK_HALTED
    [p] = rig.view.build(OPEN_NOW + timedelta(seconds=12)).positions
    assert p.stuck_halted


def test_untracked_broker_position_shown(rig):
    rig.market.add("ZZZZ", price=2.0)
    rig.price("ZZZZ", 2.0)
    rig.broker.submit_market("ZZZZ", "buy", 10, "manual-1")
    rig.broker.match()
    rows = rig.view.build(OPEN_NOW).positions
    [p] = [r for r in rows if r.ticker == "ZZZZ"]
    assert p.untracked and p.qty == 10 and p.key == "untracked:ZZZZ"


def test_rejected_panel(rig):
    c = rig.ticket(OPEN_NOW)
    rig.price("ABCD", 4.95, bid=4.80, ask=4.96)                   # spread blows out -> re-check rejects
    assert not rig.desk.approve(c.id, now=OPEN_NOW + timedelta(seconds=5)).ok
    [r] = rig.view.build(OPEN_NOW + timedelta(seconds=6)).rejected
    assert r.ticker == "ABCD" and r.rejected_by == "risk recheck" and any("spread" in x for x in r.reasons)


def test_session_ended_summary(rig):
    c = rig.ticket(OPEN_NOW)
    rig.desk.pass_ticket(c.id, now=OPEN_NOW + timedelta(seconds=3))
    s = rig.view.build(et(2026, 11, 9, 12, 0)).status
    assert s.session_ended and s.phase == "Ended"
    assert any("1 passed" in line for line in s.summary)


def test_countdown():
    assert countdown(75) == "1:15" and countdown(-3) == "0:00" and countdown(3 * 3600 + 120) == "3h 02m"


def test_chart_svg():
    bars = [{"t": "", "o": 4.6 + i * .01, "h": 4.7 + i * .01, "l": 4.55 + i * .01, "c": 4.65 + i * .01}
            for i in range(10)]
    svg = chart_svg(bars, entry=5.0, stop=4.87, target=5.26)
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert svg.count("<rect") == 10 and "entry 5" in svg and "stop 4.87" in svg and "target 5.26" in svg
    assert chart_svg([], 5.0, 4.87, 5.26) == ""


def test_ticket_shows_newsie_grade_and_sources(rig):
    from tests.factories import news_data

    n = news_data()
    rig.app.pipeline.cache.put_news("ABCD", n.model_copy(update={"graded_at": OPEN_NOW}))
    rig.ticket(OPEN_NOW)
    [t] = rig.view.build(OPEN_NOW).tickets
    assert t.catalyst_badge == "Catalyst A · FDA" and t.grade_reason == n.grade_reason
    [h] = t.headlines
    assert h.grade == "A" and h.sources == ("google_news",)


def test_ticket_shows_buzz_regime_and_promo(rig):
    from tradingdesk.models import SentimentData

    rig.app.pipeline.cache.put_sentiment("ABCD", SentimentData(
        buzz_score=40, buzz_trend="rising", promo_warning=True, promo_reasons=("4 of 6 recent posts look like promotion",),
        crowd_score=78, crowd_label="EXTREMELY_BULLISH", tone=0.5, sources=("stocktwits",),
        market_regime="hostile", regime_detail={"summary": "SPY -1.2%, VIX 27.0"}, as_of=OPEN_NOW))
    rig.ticket(OPEN_NOW)
    [t] = rig.view.build(OPEN_NOW).tickets
    assert t.buzz == "40/100 · rising · StockTwits 78 extremely bullish · tone +0.50"
    assert t.regime == "hostile (SPY -1.2%, VIX 27.0)"
    assert t.promo_warning and t.promo_reasons == ("4 of 6 recent posts look like promotion",)
