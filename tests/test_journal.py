from __future__ import annotations

import pytest

from tests import factories as f
from tests.conftest import et
from tradingdesk.journal import Journal
from tradingdesk.llm.costs import LLMUsage, estimate_cost, with_cost
from tradingdesk.models import Agent, Candidate, CandidateStatus
from tradingdesk.settings import ModelPrice

PRICING = {
    "claude-opus-5-5": ModelPrice(input=4.0, output=20.0, cache_write=5.0, cache_read=0.2),
    "claude-sonnet-5": ModelPrice(input=2.0, output=10.0, cache_write=2.5, cache_read=0.2),
}


@pytest.fixture
def journal(tmp_path):
    with Journal(tmp_path / "journal.db") as j:
        yield j


def test_save_and_load_candidate(journal):
    c = f.awaiting_human()
    journal.save_candidate(c)
    loaded = journal.get_candidate(c.id)
    assert loaded == c


def test_resave_updates_row_and_verdicts(journal):
    c = f.awaiting_human()
    journal.save_candidate(c)
    f.approve(c, shares=30, seconds=8)
    journal.save_candidate(c)
    rows = journal._conn.execute("SELECT status, human_decision, decision_seconds, shares FROM candidates").fetchall()
    assert len(rows) == 1
    assert tuple(rows[0]) == ("human_approved", "approved", 8.0, 30)
    n = journal._conn.execute("SELECT COUNT(*) FROM verdicts WHERE candidate_id = ?", (c.id,)).fetchone()[0]
    assert n == len(c.verdicts) == 4   # scout, skeptic, risk officer, click-time re-check


def test_rejected_candidates_are_journaled(journal):
    c = Candidate(ticker="WXYZ", session="premarket", created_at=et(2026, 11, 9, 7, 5))
    c.attach("scout", f.scout_data())
    c.transition(CandidateStatus.RESEARCHED, by=Agent.MANAGER)
    c.reject(Agent.SKEPTIC, ["424B5 filed this morning", "spread 3%"])
    journal.save_candidate(c)
    rows = journal._conn.execute(
        "SELECT ticker, session, status, rejected_by, trade_date FROM candidates"
    ).fetchall()
    assert tuple(rows[0]) == ("WXYZ", "premarket", "rejected", "skeptic", "2026-11-09")
    v = journal._conn.execute("SELECT agent, decision, reasons FROM verdicts").fetchall()
    assert tuple(v[0]) == ("skeptic", "reject", '["424B5 filed this morning", "spread 3%"]')


def test_list_candidates_filters(journal):
    a = f.awaiting_human()
    b = Candidate(ticker="WXYZ", session="open", created_at=f.T0)
    b.reject(Agent.RISK_OFFICER, ["max trades per day"])
    journal.save_candidate(a)
    journal.save_candidate(b)
    assert {c.ticker for c in journal.list_candidates(trade_date="2026-11-09")} == {"ABCD", "WXYZ"}
    assert [c.ticker for c in journal.list_candidates(status="rejected")] == ["WXYZ"]
    assert journal.list_candidates(trade_date="2026-11-10") == []


def test_trade_events(journal):
    c = f.awaiting_human()
    journal.save_candidate(c)
    journal.log_event("stop_moved", candidate_id=c.id, ticker=c.ticker, old=4.05, new=4.25, reason="breakeven")
    journal.log_event("close", candidate_id=c.id, ticker=c.ticker, reason="wind-down")
    ev = journal.events(candidate_id=c.id)
    assert [e["event"] for e in ev] == ["stop_moved", "close"]
    assert ev[1]["detail"] == {"reason": "wind-down"}


def test_cost_estimate():
    u = LLMUsage(agent="skeptic", model="claude-opus-5-5", input_tokens=2000, output_tokens=500,
                 cache_read_tokens=1500, cache_write_tokens=0)
    assert estimate_cost(u, PRICING) == pytest.approx((2000 * 4 + 500 * 20 + 1500 * 0.2) / 1e6)


def test_local_calls_are_free():
    u = LLMUsage(agent="newsie", model="qwen3:14b", input_tokens=10_000, output_tokens=5_000, local=True)
    assert estimate_cost(u, PRICING) == 0.0


def test_unknown_model_costed_conservatively():
    u = LLMUsage(agent="newsie", model="claude-mystery", input_tokens=1_000_000)
    assert estimate_cost(u, PRICING) == 4.0  # most expensive input rate in the table


def test_llm_spend_per_day_and_agent(journal):
    day = et(2026, 11, 9, 7, 0)
    for agent, model, tin, tout in [("newsie", "claude-sonnet-5", 1000, 200), ("skeptic", "claude-opus-5-5", 3000, 800),
                                    ("skeptic", "claude-opus-5-5", 2500, 700)]:
        journal.log_llm_usage(with_cost(LLMUsage(agent=agent, model=model, input_tokens=tin, output_tokens=tout, at=day),
                                        PRICING))
    journal.log_llm_usage(with_cost(LLMUsage(agent="newsie", model="qwen3:14b", input_tokens=9000, local=True, at=day),
                                    PRICING))
    journal.log_llm_usage(with_cost(LLMUsage(agent="newsie", model="claude-sonnet-5", input_tokens=1000,
                                             at=et(2026, 11, 10, 7, 0)), PRICING))
    by_agent = journal.llm_spend_by_agent("2026-11-09")
    assert by_agent["newsie"] == pytest.approx((1000 * 2 + 200 * 10) / 1e6)
    assert by_agent["skeptic"] == pytest.approx((5500 * 4 + 1500 * 20) / 1e6)
    assert journal.llm_spend("2026-11-09") == pytest.approx(sum(by_agent.values()))
    assert journal.llm_spend("2026-11-09", agent="skeptic") == pytest.approx(by_agent["skeptic"])
    assert journal.recent_token_average("skeptic", window=2) == pytest.approx((3800 + 3200) / 2)
    assert journal.recent_token_average("sentiment", window=5) is None


def test_utc_timestamp_uses_eastern_trade_date(journal):
    from datetime import datetime, timezone
    # 02:00 UTC Nov 10 = 21:00 ET Nov 9
    journal.log_llm_usage(LLMUsage(agent="newsie", model="claude-sonnet-5", cost_usd=0.5,
                                   at=datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc)))
    assert journal.llm_spend("2026-11-09") == 0.5
