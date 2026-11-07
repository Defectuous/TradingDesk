"""Builders for test candidates at each stage of the chain."""
from __future__ import annotations

from datetime import timedelta

from tests.conftest import et
from tradingdesk.models import (
    Agent,
    Candidate,
    CandidateStatus,
    FilingFlags,
    GradedHeadline,
    Headline,
    HumanDecision,
    NewsData,
    PillarCheck,
    ProposedTrade,
    RiskSizing,
    ScoutData,
    SentimentData,
)

T0 = et(2026, 11, 9, 9, 45)


def scout_data(**kw) -> ScoutData:
    base = dict(
        price=4.20, change_pct=32.5, gap_pct=18.0, relative_volume=9.1, volume=2_500_000,
        float_shares=6_000_000, spread_pct=0.4, vwap=3.95,
        pillars={
            "rvol": PillarCheck(passed=True, value=9.1, threshold=2.0),
            "momentum": PillarCheck(passed=True, value=32.5, threshold=10.0),
            "catalyst": PillarCheck(passed=True, value="A"),
            "price": PillarCheck(passed=True, value=4.20),
            "float": PillarCheck(passed=True, value=6_000_000, threshold=20_000_000),
        },
    )
    base.update(kw)
    return ScoutData(**base)


def news_data() -> NewsData:
    h = Headline(source="google_news", tickers=("ABCD",), title="ABCD gets FDA Fast Track",
                 url="https://example.com/a", published_at=T0 - timedelta(hours=2))
    return NewsData(catalyst_grade="A", catalyst_type="FDA", grade_reason="Fresh FDA Fast Track today.",
                    headlines=(GradedHeadline(headline=h, grade="A"),), filing_flags=FilingFlags(),
                    graded_at=T0)


def sentiment_data() -> SentimentData:
    return SentimentData(buzz_score=7.5, buzz_trend="rising", market_regime="supportive", as_of=T0)


def trade() -> ProposedTrade:
    return ProposedTrade(entry=4.25, stop=4.05, target=4.75, strategy="gap_and_go")


def sizing(shares: int = 47) -> RiskSizing:
    return RiskSizing(shares=shares, dollars_at_risk=round(shares * 0.20, 2), position_value=round(shares * 4.25, 2),
                      session="open")


def awaiting_human() -> Candidate:
    """A candidate that has cleared every gate and sits on the Trade Desk."""
    c = Candidate(ticker="abcd", session="open", created_at=T0)
    c.attach("scout", scout_data())
    c.record_pass(Agent.SCOUT, ["5/5 pillars"])
    c.attach("news", news_data())
    c.attach("sentiment", sentiment_data())
    c.attach("proposed_trade", trade())
    c.transition(CandidateStatus.RESEARCHED, by=Agent.MANAGER)
    c.record_pass(Agent.SKEPTIC, ["extension is moderate"])
    c.transition(CandidateStatus.CHALLENGED, by=Agent.SKEPTIC)
    c.attach("risk_sizing", sizing())
    c.record_pass(Agent.RISK_OFFICER, ["within limits"])
    c.transition(CandidateStatus.RISK_CLEARED, by=Agent.RISK_OFFICER)
    c.transition(CandidateStatus.AWAITING_HUMAN, by=Agent.MANAGER)
    return c


def approval(shares: int = 47, seconds: float = 12.0) -> HumanDecision:
    return HumanDecision(decision="approved", shown_at=T0, decided_at=T0 + timedelta(seconds=seconds),
                         approved_shares=shares)
