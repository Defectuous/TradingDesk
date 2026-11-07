from __future__ import annotations

from datetime import timedelta

import pytest

from tests.conftest import et
from tests.fakes import FakeMarket, snapshot
from tradingdesk.agents.scout import Scout, catalyst_from_news
from tradingdesk.legacy.wtbot import config as wtbot_config
from tradingdesk.models import Agent, CandidateStatus, NewsData
from tradingdesk.research_cache import ResearchCache
from tradingdesk.sessions import SessionClock

PM_NOW = et(2026, 11, 9, 7, 15)


@pytest.fixture
def setup(make_settings):
    s = make_settings()
    wtbot_config.apply_settings(s)
    market, cache = FakeMarket(), ResearchCache()
    return Scout(s, market, cache, SessionClock(s.config.sessions)), market, cache


def test_qualifying_ticker_becomes_candidate(setup):
    scout, market, _ = setup
    market.add("ABCD")
    hits = scout.scan(PM_NOW)
    assert len(hits) == 1
    c = hits[0].candidate
    assert c.ticker == "ABCD" and c.session == "premarket" and c.status is CandidateStatus.SPOTTED
    assert c.scout.pillars_passed == 5            # no grade yet -> WTBot treats catalyst as neutral pass
    assert c.scout.pillars["catalyst"].note == "no grade yet (neutral)"
    assert c.scout.key_levels["pm_high"] == 4.24
    assert c.verdicts[0].agent is Agent.SCOUT


def test_failing_pillars_filtered(setup):
    scout, market, _ = setup
    market.add("BIGF", float_shares=80_000_000)           # pillar 5 fails
    market.add("PRCY", price=35.0)                        # pillar 4 fails
    market.add("SLOW", rvol=1.1)                          # pillar 1 fails
    assert scout.scan(PM_NOW) == []


def test_cooldown(setup):
    scout, market, _ = setup
    market.add("ABCD")
    assert len(scout.scan(PM_NOW)) == 1
    assert scout.scan(PM_NOW + timedelta(minutes=5)) == []
    assert len(scout.scan(PM_NOW + timedelta(minutes=11))) == 1


def test_no_scan_outside_sessions(setup):
    scout, market, _ = setup
    market.add("ABCD")
    assert scout.scan(et(2026, 11, 9, 11, 5)) == []
    assert market.snapshot_calls == []


def test_one_bad_ticker_does_not_stop_scan(setup):
    scout, market, _ = setup
    market.add("ABCD")
    market.add("BOOM", change=40.0)
    orig = market.snapshot
    market.snapshot = lambda t: (_ for _ in ()).throw(RuntimeError("feed down")) if t == "BOOM" else orig(t)
    assert [h.candidate.ticker for h in scout.scan(PM_NOW)] == ["ABCD"]


def _news(grade: str, ctype: str) -> NewsData:
    return NewsData(catalyst_grade=grade, catalyst_type=ctype, grade_reason="x", graded_at=PM_NOW)


@pytest.mark.parametrize("grade, ctype, expect_pass, expect_type", [
    ("A", "FDA", True, "fda"),
    ("B", "Partnership", True, "partnership"),
    ("C", "Conference", False, "unclear"),
    ("F", "Offering", False, "negative"),
    ("F", "None", False, "none"),
])
def test_grade_mapping_into_wtbot_pillar(grade, ctype, expect_pass, expect_type):
    from tradingdesk.legacy.wtbot import pillar_checks

    cat = catalyst_from_news(_news(grade, ctype))
    assert cat["catalyst_type"] == expect_type
    r = pillar_checks.check_all_pillars(snapshot(), 6_000_000, cat["has_catalyst"], cat)
    assert r["pillar_3_catalyst"]["pass"] is expect_pass


def test_negative_catalyst_blocks_even_reduced_path(setup):
    scout, market, cache = setup
    market.add("ABCD", change=40.0)                        # strong momentum would allow reduced size
    cache.put_news("ABCD", _news("F", "Offering"))
    assert scout.scan(PM_NOW) == []


def test_vague_catalyst_uses_reduced_path(setup):
    scout, market, cache = setup
    market.add("ABCD", change=40.0)
    cache.put_news("ABCD", _news("C", "Conference"))
    hits = scout.scan(PM_NOW)
    assert len(hits) == 1 and hits[0].candidate.scout.qualifies_reduced
