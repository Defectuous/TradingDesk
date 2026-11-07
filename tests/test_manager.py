from __future__ import annotations

import pytest

from tests.conftest import et
from tests.fakes import FakeMarket
from tradingdesk.agents.risk_officer import AccountState
from tradingdesk.journal import Journal
from tradingdesk.market import Quote
from tradingdesk.models import Agent, Candidate, CandidateStatus, NewsData, Verdict
from tradingdesk.runner import build_pipeline

S = CandidateStatus
PM_NOW = et(2026, 11, 9, 7, 15)
OPEN_NOW = et(2026, 11, 9, 9, 45)


@pytest.fixture
def env(make_settings, tmp_path):
    settings = make_settings()
    market = FakeMarket()
    with Journal(tmp_path / "j.db") as journal:
        p = build_pipeline(settings, journal, market=market, buying_power=10_000)
        yield p, market, journal


def run(p, now):
    return [p.manager.process(h, now) for h in p.scout.scan(now)]


def test_happy_path_stops_at_awaiting_human(env):
    p, market, journal = env
    market.add("ABCD")
    [c] = run(p, PM_NOW)
    assert c.status is S.AWAITING_HUMAN
    assert [h.status for h in c.status_history] == [S.RESEARCHED, S.CHALLENGED, S.RISK_CLEARED, S.AWAITING_HUMAN]
    assert [v.agent for v in c.verdicts] == [Agent.SCOUT, Agent.MANAGER, Agent.SKEPTIC, Agent.RISK_OFFICER]
    assert c.proposed_trade.entry == 4.24 and c.risk_sizing.software_managed_stop
    assert p.tickets.pending() == [c]
    assert journal.get_candidate(c.id).status is S.AWAITING_HUMAN
    assert any("catalyst pending" in r for r in c.verdicts[1].reasons)


def test_never_goes_past_awaiting_human(env):
    p, market, _ = env
    market.add("ABCD")
    [c] = run(p, PM_NOW)
    assert c.human is None and not c.fills
    assert not hasattr(p.manager, "executor")


def test_no_setup_rejected_by_manager_and_journaled(env):
    p, market, journal = env
    market.add("ABCD", lows=[3.0] * 5)          # stop would be ~29% away
    [c] = run(p, PM_NOW)
    assert c.status is S.REJECTED and c.rejected_by is Agent.MANAGER
    assert "no setup" in c.rejection_reasons[0]
    assert journal.list_candidates(status="rejected")[0].id == c.id
    assert p.tickets.pending() == []


def test_no_catalyst_grade_rejected(env):
    p, market, _ = env
    market.add("ABCD", change=40.0)            # strong momentum: reduced path lets the Scout pass it
    p.manager.cache.put_news("ABCD", NewsData(catalyst_grade="F", catalyst_type="None",
                                              grade_reason="no news for the move", graded_at=PM_NOW))
    [c] = run(p, PM_NOW)
    assert c.rejected_by is Agent.MANAGER and "no identifiable news catalyst" in c.rejection_reasons[0]
    assert c.news.catalyst_grade == "F"


def test_cached_news_attached(env):
    p, market, _ = env
    market.add("ABCD")
    p.manager.cache.put_news("ABCD", NewsData(catalyst_grade="A", catalyst_type="FDA",
                                              grade_reason="Fast Track today", graded_at=PM_NOW))
    [c] = run(p, PM_NOW)
    assert c.status is S.AWAITING_HUMAN and c.news.catalyst_type == "FDA"


class RejectingSkeptic:
    def review(self, c: Candidate) -> Verdict:
        return Verdict(agent=Agent.SKEPTIC, decision="reject", reasons=("S-3 shelf on file",))


class MeddlingSkeptic:
    """Tries to slip extra verdicts onto the candidate it was given."""

    def review(self, c: Candidate) -> Verdict:
        c.add_verdict(Verdict(agent=Agent.SKEPTIC, decision="pass", reasons=("sneaky",)))
        return Verdict(agent=Agent.SKEPTIC, decision="pass", reasons=("fine",))


class BrokenSkeptic:
    def review(self, c: Candidate) -> Verdict:
        raise TimeoutError("Claude timed out")


class WrongAgentSkeptic:
    def review(self, c: Candidate) -> Verdict:
        return Verdict(agent=Agent.RISK_OFFICER, decision="pass")


def test_skeptic_rejection_ends_chain(env):
    p, market, _ = env
    p.manager.skeptic = RejectingSkeptic()
    market.add("ABCD")
    [c] = run(p, PM_NOW)
    assert c.status is S.REJECTED and c.rejected_by is Agent.SKEPTIC
    assert c.risk_sizing is None                 # Risk Officer never ran


def test_skeptic_works_on_a_copy(env):
    p, market, _ = env
    p.manager.skeptic = MeddlingSkeptic()
    market.add("ABCD")
    [c] = run(p, PM_NOW)
    skeptic_verdicts = [v for v in c.verdicts if v.agent is Agent.SKEPTIC]
    assert len(skeptic_verdicts) == 1 and skeptic_verdicts[0].reasons == ("fine",)


@pytest.mark.parametrize("skeptic", [BrokenSkeptic(), WrongAgentSkeptic()])
def test_skeptic_failure_defaults_to_reject(env, skeptic):
    p, market, _ = env
    p.manager.skeptic = skeptic
    market.add("ABCD")
    [c] = run(p, PM_NOW)
    assert c.rejected_by is Agent.SKEPTIC and "default reject" in c.rejection_reasons[0]


def test_risk_officer_veto(env):
    p, market, _ = env
    market.add("ABCD", quote=Quote(4.20, 4.10, 4.21))   # 2.6% spread, premarket max is 1%
    [c] = run(p, PM_NOW)
    assert c.rejected_by is Agent.RISK_OFFICER and any("spread" in r for r in c.rejection_reasons)
    assert c.status is S.REJECTED and p.tickets.pending() == []


def test_daily_loss_limit_blocks_everything(env):
    p, market, _ = env

    class LosingDay:
        def state(self, now):
            return AccountState(buying_power=10_000, realized_pnl_today=-60)

    p.manager.accounts = LosingDay()
    market.add("ABCD")
    market.add("WXYZ", change=45.0)
    results = run(p, PM_NOW)
    assert len(results) == 2 and all(c.rejected_by is Agent.RISK_OFFICER for c in results)


def test_second_ticket_same_ticker_blocked(env):
    p, market, _ = env
    market.add("ABCD")
    [first] = run(p, PM_NOW)
    p.scout._last_emitted.clear()               # bypass the Scout cooldown
    [second] = run(p, PM_NOW)
    assert first.status is S.AWAITING_HUMAN
    assert second.rejected_by is Agent.RISK_OFFICER and "already held" in second.rejection_reasons[0]


def test_open_session_plans_off_high_of_day(env):
    p, market, _ = env
    market.add("ABCD", price=4.95, pm_high=4.60, hod=5.00, quote=Quote(4.95, 4.94, 4.96))
    [c] = run(p, OPEN_NOW)
    assert c.session == "open" and "high of day" in c.proposed_trade.strategy
    assert c.status is S.AWAITING_HUMAN and not c.risk_sizing.software_managed_stop


def test_internal_error_is_journaled_as_rejection(env):
    p, market, journal = env
    market.add("ABCD")
    p.manager.market = None                      # quote lookup will raise
    [c] = run(p, PM_NOW)
    assert c.rejected_by is Agent.MANAGER and "internal error" in c.rejection_reasons[0]
    assert journal.get_candidate(c.id).status is S.REJECTED


def test_everything_is_journaled(env):
    p, market, journal = env
    market.add("ABCD")
    market.add("NOPE", lows=[2.0] * 5, change=33.0)
    run(p, PM_NOW)
    stored = {c.ticker: c.status for c in journal.list_candidates(trade_date="2026-11-09")}
    assert stored == {"ABCD": S.AWAITING_HUMAN, "NOPE": S.REJECTED}
