from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from tests import factories as f
from tradingdesk.models import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    Agent,
    Candidate,
    CandidateStatus,
    Fill,
    HumanDecision,
    IllegalTransition,
    ProposedTrade,
    StageAlreadySet,
    Verdict,
)

S = CandidateStatus


# -- basics -------------------------------------------------------------------

def test_ticker_normalised_and_validated():
    assert Candidate(ticker=" abcd ", session="open").ticker == "ABCD"
    with pytest.raises(ValidationError):
        Candidate(ticker="not a ticker", session="open")


def test_naive_timestamp_rejected():
    with pytest.raises(ValidationError):
        Candidate(ticker="ABCD", session="open", created_at=datetime(2026, 11, 9, 9, 45))


def test_full_chain_reaches_awaiting_human():
    c = f.awaiting_human()
    assert c.status is S.AWAITING_HUMAN
    assert [h.status for h in c.status_history] == [S.RESEARCHED, S.CHALLENGED, S.RISK_CLEARED, S.AWAITING_HUMAN]
    assert [v.agent for v in c.verdicts] == [Agent.SCOUT, Agent.SKEPTIC, Agent.RISK_OFFICER]
    assert c.scout.pillars_passed == 5


# -- stages never overwrite -------------------------------------------------------

@pytest.mark.parametrize("stage", ["scout", "news", "sentiment", "proposed_trade", "risk_sizing"])
def test_stage_set_once(stage):
    c = f.awaiting_human()
    replacement = {"scout": f.scout_data(price=9.9), "news": f.news_data(), "sentiment": f.sentiment_data(),
                   "proposed_trade": f.trade(), "risk_sizing": f.sizing(10)}[stage]
    with pytest.raises(StageAlreadySet):
        c.attach(stage, replacement)
    with pytest.raises(StageAlreadySet):
        setattr(c, stage, replacement)


def test_stage_data_is_frozen():
    c = f.awaiting_human()
    with pytest.raises(ValidationError):
        c.proposed_trade.stop = 1.0  # type: ignore[misc]


def test_verdicts_append_only():
    c = f.awaiting_human()
    with pytest.raises(StageAlreadySet):
        c.verdicts = c.verdicts[1:]
    with pytest.raises(StageAlreadySet):
        c.verdicts = ()
    with pytest.raises(AttributeError):
        c.verdicts.append(Verdict(agent=Agent.SKEPTIC, decision="pass"))  # tuples have no append


def test_status_cannot_be_assigned_directly():
    c = f.awaiting_human()
    with pytest.raises(StageAlreadySet):
        c.status = S.HUMAN_APPROVED


@pytest.mark.parametrize("field", ["id", "ticker", "created_at", "session"])
def test_identity_fields_locked(field):
    c = f.awaiting_human()
    with pytest.raises(StageAlreadySet):
        setattr(c, field, {"id": "x", "ticker": "ZZZ", "created_at": f.T0, "session": "premarket"}[field])


# -- gates can only say no -------------------------------------------------------------

def test_verdict_has_no_approve_decision():
    for bad in ("approve", "modify", "force"):
        with pytest.raises(ValidationError):
            Verdict(agent=Agent.SKEPTIC, decision=bad)  # type: ignore[arg-type]


def test_reject_ends_chain():
    c = Candidate(ticker="ABCD", session="open")
    c.attach("scout", f.scout_data())
    c.transition(S.RESEARCHED, by=Agent.MANAGER)
    c.reject(Agent.SKEPTIC, ["S-3 shelf filed yesterday"])
    assert c.status is S.REJECTED and c.is_terminal
    assert c.rejected_by is Agent.SKEPTIC
    assert c.rejection_reasons == ("S-3 shelf filed yesterday",)
    with pytest.raises(IllegalTransition):
        c.transition(S.CHALLENGED, by=Agent.MANAGER)
    with pytest.raises(IllegalTransition):
        c.reject(Agent.RISK_OFFICER, ["again"])


def test_refused_reject_changes_nothing():
    c = f.awaiting_human()
    c.record_human(f.approval())
    c.add_fill(Fill(side="buy", qty=47, price=4.25, at=f.T0))
    c.transition(S.FILLED, by=Agent.EXECUTOR)
    before = c.model_dump()
    with pytest.raises(IllegalTransition):
        c.reject(Agent.RISK_OFFICER, ["too late"])
    assert c.model_dump() == before


def test_reject_requires_reason():
    c = Candidate(ticker="ABCD", session="open")
    with pytest.raises(ValueError):
        c.reject(Agent.RISK_OFFICER, [])


def test_rejected_by_cannot_be_rewritten():
    c = Candidate(ticker="ABCD", session="open")
    c.reject(Agent.RISK_OFFICER, ["daily loss limit hit"])
    with pytest.raises(StageAlreadySet):
        c.rejected_by = Agent.SKEPTIC


# -- transitions -------------------------------------------------------------------------

def test_cannot_skip_to_approved():
    c = Candidate(ticker="ABCD", session="open")
    for target in (S.HUMAN_APPROVED, S.FILLED, S.AWAITING_HUMAN, S.RISK_CLEARED):
        with pytest.raises(IllegalTransition):
            c.transition(target, by=Agent.MANAGER)


def test_only_awaiting_human_leads_to_approval():
    sources = [s for s, nxt in ALLOWED_TRANSITIONS.items() if S.HUMAN_APPROVED in nxt]
    assert sources == [S.AWAITING_HUMAN]


def test_terminal_statuses():
    assert TERMINAL_STATUSES == {S.HUMAN_PASSED, S.EXPIRED, S.REJECTED, S.CLOSED}


# -- human decisions ------------------------------------------------------------------------

def test_approval_records_decision_time():
    c = f.awaiting_human()
    c.record_human(f.approval(shares=47, seconds=12.5))
    assert c.status is S.HUMAN_APPROVED
    assert c.human.decision_seconds == 12.5
    assert c.status_history[-1].by is Agent.HUMAN


def test_reduce_size_allowed_increase_refused():
    c = f.awaiting_human()
    with pytest.raises(ValueError, match="only be reduced"):
        c.record_human(f.approval(shares=48))
    assert c.status is S.AWAITING_HUMAN and c.human is None
    c.record_human(f.approval(shares=20))
    assert c.human.approved_shares == 20


def test_pass_with_note():
    c = f.awaiting_human()
    c.record_human(HumanDecision(decision="passed", shown_at=f.T0, decided_at=f.T0 + timedelta(seconds=4),
                                 note="too extended"))
    assert c.status is S.HUMAN_PASSED and c.human.note == "too extended"


def test_expired_ticket_cannot_then_be_approved():
    c = f.awaiting_human()
    c.record_human(HumanDecision(decision="expired", shown_at=f.T0, decided_at=f.T0 + timedelta(seconds=90)))
    assert c.status is S.EXPIRED and c.status_history[-1].by is Agent.DESK
    with pytest.raises(IllegalTransition):
        c.record_human(f.approval())


def test_double_approval_refused():
    c = f.awaiting_human()
    c.record_human(f.approval())
    with pytest.raises(IllegalTransition):
        c.record_human(f.approval())


def test_decision_validation():
    with pytest.raises(ValidationError):  # approval without shares
        HumanDecision(decision="approved", shown_at=f.T0, decided_at=f.T0)
    with pytest.raises(ValidationError):  # decided before shown
        HumanDecision(decision="passed", shown_at=f.T0, decided_at=f.T0 - timedelta(seconds=1))
    with pytest.raises(ValidationError):
        HumanDecision(decision="passed", shown_at=f.T0, decided_at=f.T0, note="x" * 201)


# -- trade levels and P&L ------------------------------------------------------------------------

def test_proposed_trade_math():
    t = ProposedTrade(entry=4.25, stop=4.05, target=4.75)
    assert t.risk_per_share == 0.20
    assert t.risk_reward == 2.5


@pytest.mark.parametrize("entry, stop, target", [(4.0, 4.1, 5.0), (4.0, 3.9, 3.95), (4.0, 4.0, 5.0)])
def test_bad_long_levels_rejected(entry, stop, target):
    with pytest.raises(ValidationError):
        ProposedTrade(entry=entry, stop=stop, target=target)


def test_fills_and_pnl():
    c = f.awaiting_human()
    c.record_human(f.approval(shares=40))
    c.add_fill(Fill(side="buy", qty=30, price=4.25, at=f.T0, reason="entry"))
    c.add_fill(Fill(side="buy", qty=10, price=4.27, at=f.T0, reason="entry"))  # partial fills
    c.transition(S.FILLED, by=Agent.EXECUTOR)
    c.add_fill(Fill(side="sell", qty=20, price=4.75, at=f.T0, reason="target"))
    c.add_fill(Fill(side="sell", qty=20, price=4.30, at=f.T0, reason="wind-down"))
    assert c.position_qty == 0
    # avg buy = (30*4.25 + 10*4.27)/40 = 4.255
    assert c.realized_pnl == round(20 * (4.75 - 4.255) + 20 * (4.30 - 4.255), 2)
    assert c.r_multiple == round(c.realized_pnl / c.risk_sizing.dollars_at_risk, 2)
    c.transition(S.CLOSED, by=Agent.TRADE_MANAGER)
    assert c.is_terminal


# -- serialization --------------------------------------------------------------------------------

def test_json_round_trip_keeps_guards():
    c = f.awaiting_human()
    c.record_human(f.approval())
    restored = Candidate.model_validate_json(c.model_dump_json())
    assert restored == c
    assert restored.scout.pillars_passed == 5
    with pytest.raises(StageAlreadySet):
        restored.attach("scout", f.scout_data())
    with pytest.raises(IllegalTransition):
        restored.record_human(f.approval())
