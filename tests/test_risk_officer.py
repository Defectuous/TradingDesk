from __future__ import annotations

import pytest

from tests import factories as f
from tests.conftest import et
from tradingdesk.agents.risk_officer import AccountState, RiskOfficer
from tradingdesk.market import Quote
from tradingdesk.models import Agent, Candidate, CandidateStatus, ProposedTrade
from tradingdesk.sessions import SessionClock

OPEN_NOW = et(2026, 11, 9, 9, 45)       # Monday, open session
PM_NOW = et(2026, 11, 9, 7, 15)         # premarket
QUOTE = Quote(price=4.20, bid=4.19, ask=4.21)
ACCOUNT = AccountState(buying_power=10_000)


@pytest.fixture
def ro(make_settings) -> RiskOfficer:
    s = make_settings()
    return RiskOfficer(s.config.risk, SessionClock(s.config.sessions))


def challenged(session: str = "open", trade: ProposedTrade | None = None, **scout_kw) -> Candidate:
    """A candidate that has passed Manager and Skeptic, ready for the Risk Officer."""
    c = Candidate(ticker="ABCD", session=session, created_at=OPEN_NOW if session == "open" else PM_NOW)
    c.attach("scout", f.scout_data(**scout_kw))
    c.attach("proposed_trade", trade or ProposedTrade(entry=4.25, stop=4.05, target=4.75))
    c.transition(CandidateStatus.RESEARCHED, by=Agent.MANAGER)
    c.record_pass(Agent.SKEPTIC)
    c.transition(CandidateStatus.CHALLENGED, by=Agent.SKEPTIC)
    return c


def test_clean_candidate_sized_by_risk(ro):
    d = ro.evaluate(challenged(), ACCOUNT, QUOTE, OPEN_NOW)
    assert d.ok, d.reasons
    # open: $20 risk / $0.20 per share = 100 shares, but $200 max position / 4.25 = 47 shares
    assert d.sizing.shares == 47
    assert d.sizing.dollars_at_risk == round(47 * 0.20, 2)
    assert d.sizing.position_value <= 200
    assert not d.sizing.software_managed_stop


def test_risk_limit_binds_when_stop_is_wide(ro):
    t = ProposedTrade(entry=4.25, stop=3.85, target=5.25)   # $0.40/share risk, R:R 2.5
    d = ro.evaluate(challenged(trade=t), ACCOUNT, QUOTE, OPEN_NOW)
    assert d.ok and d.sizing.shares == 47                     # position cap (47) < risk cap (50)
    t2 = ProposedTrade(entry=4.25, stop=3.65, target=5.75)   # $0.60/share -> risk cap 33
    assert ro.evaluate(challenged(trade=t2), ACCOUNT, QUOTE, OPEN_NOW).sizing.shares == 33


def test_premarket_limits_are_stricter(ro):
    c = challenged(session="premarket")
    d = ro.evaluate(c, ACCOUNT, QUOTE, PM_NOW)
    assert d.ok, d.reasons
    assert d.sizing.shares == 23          # $100 premarket max position / 4.25
    assert d.sizing.software_managed_stop
    assert d.sizing.session == "premarket"


def test_premarket_spread_limit(ro):
    wide = Quote(price=4.20, bid=4.15, ask=4.21)   # ~1.4% spread: OK open, too wide premarket
    assert ro.evaluate(challenged(), ACCOUNT, wide, OPEN_NOW).ok
    d = ro.evaluate(challenged(session="premarket"), ACCOUNT, wide, PM_NOW)
    assert not d.ok and any("spread" in r for r in d.reasons)


@pytest.mark.parametrize("now, why", [
    (et(2026, 11, 9, 11, 0), "outside trading windows"),
    (et(2026, 11, 9, 5, 45), "outside trading windows"),
    (et(2026, 11, 7, 10, 0), "outside trading windows"),    # Saturday
])
def test_outside_session_rejected(ro, now, why):
    d = ro.evaluate(challenged(), ACCOUNT, QUOTE, now)
    assert not d.ok and any(why in r for r in d.reasons)


def test_session_change_rejected(ro):
    d = ro.evaluate(challenged(session="premarket"), ACCOUNT, QUOTE, OPEN_NOW)
    assert not d.ok and any("session changed" in r for r in d.reasons)


def test_daily_loss_limit(ro):
    acct = AccountState(buying_power=10_000, realized_pnl_today=-45, open_pnl=-15)
    d = ro.evaluate(challenged(), acct, QUOTE, OPEN_NOW)
    assert ro.daily_loss_hit(acct)
    assert not d.ok and any("daily loss limit" in r for r in d.reasons)


def test_remaining_loss_budget_caps_risk(ro):
    acct = AccountState(buying_power=10_000, realized_pnl_today=-55)   # $5 of $60 left
    d = ro.evaluate(challenged(), acct, QUOTE, OPEN_NOW)
    assert d.ok and d.sizing.shares == 25 and d.sizing.dollars_at_risk <= 5


def test_max_trades_and_positions(ro):
    d = ro.evaluate(challenged(), AccountState(buying_power=10_000, trades_today=5), QUOTE, OPEN_NOW)
    assert any("max trades per day" in r for r in d.reasons)
    d = ro.evaluate(challenged(), AccountState(buying_power=10_000, open_positions=2), QUOTE, OPEN_NOW)
    assert any("max open positions" in r for r in d.reasons)


def test_duplicate_ticker_rejected(ro):
    acct = AccountState(buying_power=10_000, active_tickers=frozenset({"ABCD"}))
    d = ro.evaluate(challenged(), acct, QUOTE, OPEN_NOW)
    assert any("already held" in r for r in d.reasons)


def test_rr_measured_from_current_price(ro):
    """Proposed R:R is 2.5, but price has run to 4.40: (4.75-4.41)/(4.41-4.05) = 0.94."""
    d = ro.evaluate(challenged(), ACCOUNT, Quote(4.40, 4.39, 4.41), OPEN_NOW)
    assert not d.ok and any("R:R" in r for r in d.reasons)
    assert d.checks["effective_entry"] == 4.41


def test_price_under_entry_keeps_proposed_entry(ro):
    """Before the breakout triggers, risk is measured from the proposed entry."""
    d = ro.evaluate(challenged(), ACCOUNT, Quote(4.00, 3.99, 4.01), OPEN_NOW)
    assert d.ok and d.checks["effective_entry"] == 4.25


def test_price_far_past_entry_kills_rr(ro):
    d = ro.evaluate(challenged(), ACCOUNT, Quote(4.80, 4.79, 4.81), OPEN_NOW)
    assert not d.ok and d.checks["risk_reward"] < 0


def test_no_quote_rejected(ro):
    d = ro.evaluate(challenged(), ACCOUNT, None, OPEN_NOW)
    assert not d.ok and any("no live bid/ask" in r for r in d.reasons)


def test_low_volume_rejected(ro):
    d = ro.evaluate(challenged(volume=50_000), ACCOUNT, QUOTE, OPEN_NOW)
    assert any("volume" in r for r in d.reasons)


def test_halted_rejected(ro):
    d = ro.evaluate(challenged(halted=True), ACCOUNT, QUOTE, OPEN_NOW)
    assert any("halted" in r for r in d.reasons)


def test_buying_power_caps_size(ro):
    d = ro.evaluate(challenged(), AccountState(buying_power=100), QUOTE, OPEN_NOW)
    assert d.ok and d.sizing.shares == 23


def test_stop_too_wide_for_any_share(ro):
    """$18.50/share risk: the $20 per-trade limit buys 1 share, a $15 remaining budget buys none."""
    t = ProposedTrade(entry=19.0, stop=0.5, target=60.0)
    q = Quote(19.0, 18.99, 19.01)
    d = ro.evaluate(challenged(trade=t, price=19.0), ACCOUNT, q, OPEN_NOW)
    assert d.ok and d.sizing.shares == 1
    acct = AccountState(buying_power=10_000, realized_pnl_today=-45)
    d = ro.evaluate(challenged(trade=t, price=19.0), acct, q, OPEN_NOW)
    assert not d.ok and any("too wide" in r for r in d.reasons)


def test_reasons_accumulate(ro):
    acct = AccountState(buying_power=10_000, trades_today=9, open_positions=9)
    d = ro.evaluate(challenged(volume=1), acct, None, et(2026, 11, 9, 12, 0))
    assert len(d.reasons) >= 4


# -- click-time re-check (used by the Trade Desk in phase 3) --------------------

def test_recheck_allows_smaller_size(ro):
    d = ro.evaluate(challenged(), ACCOUNT, QUOTE, OPEN_NOW, requested_shares=20)
    assert d.ok and d.sizing.shares == 20 and d.agent is Agent.RISK_RECHECK


def test_recheck_refuses_larger_size(ro):
    d = ro.evaluate(challenged(), ACCOUNT, QUOTE, OPEN_NOW, requested_shares=48)
    assert not d.ok and any("requested 48" in r for r in d.reasons)


def test_recheck_blocks_after_limit_hit(ro):
    acct = AccountState(buying_power=10_000, realized_pnl_today=-60)
    d = ro.evaluate(challenged(), acct, QUOTE, OPEN_NOW, requested_shares=10)
    assert not d.ok


# -- cannot be bypassed --------------------------------------------------------------

def test_cannot_reach_risk_cleared_without_risk_officer():
    from tradingdesk.models import IllegalTransition

    c = challenged()
    with pytest.raises(IllegalTransition):
        c.transition(CandidateStatus.RISK_CLEARED, by=Agent.MANAGER)
    c.attach("risk_sizing", f.sizing())
    with pytest.raises(IllegalTransition):          # sizing alone isn't enough
        c.transition(CandidateStatus.RISK_CLEARED, by=Agent.MANAGER)
    c.record_pass(Agent.SKEPTIC)                    # another agent's pass doesn't count
    with pytest.raises(IllegalTransition):
        c.transition(CandidateStatus.RISK_CLEARED, by=Agent.MANAGER)


def test_cannot_reach_challenged_without_skeptic():
    from tradingdesk.models import IllegalTransition

    c = Candidate(ticker="ABCD", session="open", created_at=OPEN_NOW)
    c.transition(CandidateStatus.RESEARCHED, by=Agent.MANAGER)
    c.record_pass(Agent.MANAGER)
    with pytest.raises(IllegalTransition):
        c.transition(CandidateStatus.CHALLENGED, by=Agent.MANAGER)
