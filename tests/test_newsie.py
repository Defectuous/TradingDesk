"""Newsie: sources, dedup, filters, grading choice, rubric rules, caching. No network, no LLM spend."""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.conftest import et
from tradingdesk.health import Health
from tradingdesk.journal import Journal
from tradingdesk.llm.costs import LLMUsage
from tradingdesk.models import FilingFlags, Headline
from tradingdesk.news import grader as G
from tradingdesk.news.dedup import dedupe, norm_title
from tradingdesk.news.edgar import EdgarClient, EdgarResult
from tradingdesk.news.local_llm import LocalLLMError, LocalReply, OllamaClient
from tradingdesk.news.newsie import Newsie, drop_one, session_news_start
from tradingdesk.research_cache import ResearchCache
from tradingdesk.sessions import SessionClock

NOW = et(2026, 11, 9, 8, 30)            # Monday premarket


def hl(title, source="yahoo", mins_ago=30, url="", summary="", outlet="", ticker="ABCD", at=None):
    return Headline(source=source, tickers=(ticker,), title=title, summary=summary, url=url, outlet=outlet,
                    published_at=at or NOW - timedelta(minutes=mins_ago))


class FakeSource:
    def __init__(self, name, items=None, fail=False):
        self.name, self.items, self.fail, self.calls = name, items or [], fail, 0

    def fetch(self, ticker, since):
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"{self.name} down")
        return [h for h in self.items if h.published_at >= since]


class FakeGrader:
    def __init__(self, result=None, error=None, by="claude"):
        self.result = result or G.GradeResult("A", "FDA", "FDA approval today.", by, key_index=0)
        self.error, self.calls = error, []

    def grade(self, g):
        self.calls.append(g)
        if self.error:
            raise G.GradeError(self.error)
        return self.result


class FakeFilter:
    def __init__(self, drop=()):
        self.drop, self.calls = set(drop), []

    def keep(self, ticker, headlines, now):
        self.calls.append([h.title for h in headlines])
        return [h.title not in self.drop for h in headlines]


class FakeEdgar:
    name = "edgar"

    def __init__(self, result=None):
        self.result = result or EdgarResult()

    def research(self, ticker, now):
        return self.result


@pytest.fixture
def rig(make_settings, tmp_path):
    s = make_settings()
    j = Journal(tmp_path / "j.db")

    def make(sources=(), claude=None, local=None, rules_only=False, edgar=None, relevance=None, cap=2.0,
             journal=j):
        return Newsie(s.config.news, journal, ResearchCache(), SessionClock(s.config.sessions), Health(),
                      list(sources), edgar, relevance, None if rules_only else claude, None if rules_only else local,
                      cap, now_fn=lambda: NOW)
    make.journal = j
    make.settings = s
    yield make
    j.close()


# ---------------------------------------------------------------------------
# dedup and filters
# ---------------------------------------------------------------------------

def test_dedup_merges_same_story_across_sources_keeping_earliest():
    a = hl("ABCD Receives FDA Fast Track Designation", "yahoo", mins_ago=20, outlet="GlobeNewswire")
    b = hl("ABCD receives FDA fast-track designation - GlobeNewswire", "google_news", mins_ago=50)
    c = hl("ABCD prices $5M offering", "google_news", mins_ago=10)
    out = dedupe([a, b, c])
    assert len(out) == 2
    fda = next(h for h in out if "FDA" in h.title)
    assert fda.source == "google_news" and fda.published_at == b.published_at and fda.also_seen_in == ("yahoo",)
    assert out[0].title.startswith("ABCD prices")          # newest first


def test_dedup_by_url():
    a = hl("Totally different wording", "yahoo", url="https://www.example.com/story/1/")
    b = hl("Another headline", "google_news", url="https://example.com/story/1")
    assert len(dedupe([a, b])) == 1


def test_norm_title():
    assert norm_title("ABCD Soars 40%! - Benzinga") == "abcd soars 40"


def test_prefilter_drops_junk_and_unnamed_roundups(rig):
    items = [hl("ABCD Dec 2026 4.00 call (ABCD) - Interactive Stock Chart"),
             hl("12 Health Care Stocks Moving In Monday's Session"),
             hl("12 Stocks Moving Premarket: ABCD, XYZ"),
             hl("ABCD wins $40M Army contract")]
    kept = Newsie._prefilter("ABCD", items)
    assert [h.title for h in kept] == ["12 Stocks Moving Premarket: ABCD, XYZ", "ABCD wins $40M Army contract"]


# ---------------------------------------------------------------------------
# research pipeline
# ---------------------------------------------------------------------------

def test_research_grades_caches_and_journals(rig):
    src = FakeSource("yahoo", [hl("ABCD gets FDA approval for lead drug", outlet="GlobeNewswire")])
    claude = FakeGrader()
    n = rig([src], claude=claude)
    assert n.research("ABCD", NOW)
    d = n.cache.news("ABCD", NOW)
    assert d.catalyst_grade == "A" and d.catalyst_type == "FDA" and d.graded_by == "claude"
    assert d.headlines[0].headline.outlet == "GlobeNewswire"
    assert rig.journal.news_grades(NOW.date().isoformat())["ABCD"][1].catalyst_grade == "A"
    assert n.health.get("news:yahoo").ok


def test_unchanged_headlines_are_not_graded_twice(rig):
    src = FakeSource("yahoo", [hl("ABCD gets FDA approval for lead drug")])
    claude = FakeGrader()
    n = rig([src], claude=claude)
    assert n.research("ABCD", NOW)
    assert not n.research("ABCD", NOW + timedelta(minutes=1))
    assert len(claude.calls) == 1
    src.items.append(hl("ABCD also announces partnership with Pfizer", mins_ago=1))
    assert n.research("ABCD", NOW + timedelta(minutes=2))     # a new story -> regrade
    assert len(claude.calls) == 2


def test_restart_reuses_todays_grades(rig):
    src = FakeSource("yahoo", [hl("ABCD gets FDA approval for lead drug")])
    claude = FakeGrader()
    rig([src], claude=claude).research("ABCD", NOW)
    again = rig([src], claude=claude)                           # new Newsie, same journal
    assert again.cache.news("ABCD", NOW).catalyst_grade == "A"
    assert not again.research("ABCD", NOW + timedelta(minutes=1))
    assert len(claude.calls) == 1


def test_one_source_failing_doesnt_stop_others(rig):
    good = FakeSource("google_news", [hl("ABCD gets FDA approval for lead drug", "google_news")])
    bad = FakeSource("yahoo", fail=True)
    n = rig([bad, good], claude=FakeGrader())
    assert n.research("ABCD", NOW)
    assert not n.health.get("news:yahoo").ok and n.health.get("news:google_news").ok


def test_no_news_is_F_without_any_model_call(rig):
    claude, local = FakeGrader(), FakeGrader(by="local")
    n = rig([FakeSource("yahoo")], claude=claude, local=local)
    n.research("ABCD", NOW)
    d = n.cache.news("ABCD", NOW)
    assert (d.catalyst_grade, d.catalyst_type, d.graded_by) == ("F", "None", "rules")
    assert not claude.calls and not local.calls


def test_relevance_filter_runs_once_per_story(rig):
    keep, drop = hl("ABCD gets FDA approval for lead drug"), hl("Why the market is up today")
    src = FakeSource("yahoo", [keep, drop])
    f = FakeFilter(drop={drop.title})
    claude = FakeGrader()
    n = rig([src], claude=claude, relevance=f)
    n.research("ABCD", NOW)
    assert [h.title for h in claude.calls[0].headlines] == [keep.title]
    n.research("ABCD", NOW + timedelta(minutes=1))
    assert len(f.calls) == 1                                   # verdicts remembered


def test_max_headlines_sent_to_claude(rig):
    topics = ["wins Army contract", "files patent on battery", "names new CFO", "expands Texas plant",
              "reports record Q3 revenue", "signs Pfizer licensing deal", "gets FDA clearance for device",
              "launches AI platform", "receives Nasdaq notice"]
    src = FakeSource("yahoo", [hl(f"ABCD {t}", mins_ago=i + 1) for i, t in enumerate(topics)])
    claude = FakeGrader()
    rig([src], claude=claude).research("ABCD", NOW)
    assert len(claude.calls[0].headlines) == 5


# ---------------------------------------------------------------------------
# grader choice and cost control
# ---------------------------------------------------------------------------

def test_spend_cap_switches_to_local(rig):
    rig.journal.log_llm_usage(LLMUsage(agent="newsie", model="claude-sonnet-5", cost_usd=2.5, at=NOW))
    claude, local = FakeGrader(), FakeGrader(G.GradeResult("B", "Contract", "Contract win.", "local"), by="local")
    n = rig([FakeSource("yahoo", [hl("ABCD wins $40M Army contract")])], claude=claude, local=local)
    n.research("ABCD", NOW)
    assert not claude.calls and len(local.calls) == 1
    assert n.cache.news("ABCD", NOW).graded_by == "local"


def test_claude_error_falls_back_to_local_then_rules(rig):
    claude = FakeGrader(error="connection error")
    local = FakeGrader(error="local model unavailable", by="local")
    n = rig([FakeSource("yahoo", [hl("ABCD wins $40M Army contract")])], claude=claude, local=local)
    n.research("ABCD", NOW)
    d = n.cache.news("ABCD", NOW)
    assert d.graded_by == "rules" and d.catalyst_type == "Contract" and d.catalyst_grade == "C"
    assert len(claude.calls) == 1 and len(local.calls) == 1   # each tried once, never retried


def test_rules_grader_negative_keywords():
    g = G.GradeInput("ABCD", NOW, session_news_start(NOW), [hl("ABCD prices $8 million registered direct offering")],
                     FilingFlags(), [], {"offering": {"severity": "hard", "match": "registered direct offering"}})
    r = G.RulesGrader().grade(g)
    assert (r.grade, r.catalyst_type) == ("F", "Offering")


# ---------------------------------------------------------------------------
# rubric rules enforced in code
# ---------------------------------------------------------------------------

def test_session_news_start():
    assert session_news_start(et(2026, 11, 9, 8, 0)) == et(2026, 11, 6, 16, 0)     # Monday -> Friday close
    assert session_news_start(et(2026, 11, 10, 9, 45)) == et(2026, 11, 9, 16, 0)
    assert session_news_start(et(2026, 11, 10, 17, 0)) == et(2026, 11, 10, 16, 0)
    assert drop_one("A") == "B" and drop_one("F") == "F"


def test_old_news_drops_one_grade(rig):
    old = hl("ABCD gets FDA approval for lead drug", at=et(2026, 11, 6, 9, 0))     # Friday morning
    n = rig([FakeSource("yahoo", [old])], claude=FakeGrader())
    n.cfg = n.cfg.model_copy(update={"lookback_hours": 96})
    n.research("ABCD", NOW)
    d = n.cache.news("ABCD", NOW)
    assert d.catalyst_grade == "B" and "dropped one grade" in d.grade_reason


def test_offering_filed_today_caps_at_D(rig):
    edgar = FakeEdgar(EdgarResult(
        headlines=[hl("SEC filing 424B5: offering / registration (potential DILUTION)", "edgar", mins_ago=5)],
        flags=FilingFlags(offering=True, dilution=True, forms=("424B5",)), offering_today=True))
    n = rig([FakeSource("yahoo", [hl("ABCD gets FDA approval for lead drug")])], claude=FakeGrader(), edgar=edgar)
    n.research("ABCD", NOW)
    d = n.cache.news("ABCD", NOW)
    assert (d.catalyst_grade, d.catalyst_type) == ("D", "Offering") and "capped at D" in d.grade_reason
    assert d.filing_flags.offering and d.filing_flags.forms == ("424B5",)
    assert any(h.headline.source == "edgar" for h in d.headlines)


def test_offering_headline_today_caps_at_D_without_edgar(rig):
    items = [hl("ABCD gets FDA approval for lead drug"), hl("ABCD announces pricing of $10 million public offering")]
    n = rig([FakeSource("yahoo", items)], claude=FakeGrader())
    n.research("ABCD", NOW)
    assert n.cache.news("ABCD", NOW).catalyst_grade == "D"


# ---------------------------------------------------------------------------
# watchlist
# ---------------------------------------------------------------------------

def test_watchlist_priority_and_expiry(rig):
    n = rig()
    n.watch(["AAA", "BBB"], NOW - timedelta(minutes=100))
    n.watch(["CCC", "BBB"], NOW)
    assert n.watchlist(NOW) == ["CCC", "BBB"]                 # AAA expired (watch_minutes 90)


def test_scout_movers_feed_newsie(make_settings, tmp_path):
    from tests.fakes import FakeMarket
    from tradingdesk.runner import build_app

    m = FakeMarket()
    m.add("ABCD")
    j = Journal(tmp_path / "j.db")
    app = build_app(make_settings(), j, market=m, threaded_closes=False, sleep=lambda s: None)
    app.pipeline.scout.scan(NOW)
    assert app.newsie.watchlist() == ["ABCD"]
    j.close()


# ---------------------------------------------------------------------------
# Claude grader request shape
# ---------------------------------------------------------------------------

def test_claude_grader_request_and_parse(make_settings, tmp_path):
    from tradingdesk.settings import PROJECT_ROOT

    s = make_settings()
    logged = []
    g = G.ClaudeGrader("sk-test", "claude-sonnet-5", 2048, 60, 2,
                       G.load_claude_system(PROJECT_ROOT / s.config.news.types_file,
                                            PROJECT_ROOT / s.config.news.rubric_file),
                       s.config.llm.pricing, logged.append)
    sent = {}

    def create(**kw):
        sent.update(kw)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps({
                "grade": "A", "catalyst_type": "FDA", "reason": "FDA approved its lead drug today.",
                "key_headline": 1, "headline_grades": [{"n": 1, "grade": "A"}, {"n": 2, "grade": "C"}]}))],
            usage=SimpleNamespace(input_tokens=300, output_tokens=120, cache_read_input_tokens=2500,
                                  cache_creation_input_tokens=0))
    g.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    inp = G.GradeInput("ABCD", NOW, session_news_start(NOW),
                       [hl("ABCD gets FDA approval", outlet="GlobeNewswire"), hl("ABCD to present at conference")],
                       FilingFlags(), ["SEC filing 8-K: 8.01 other events"], {})
    r = g.grade(inp)
    assert (r.grade, r.catalyst_type, r.key_index, r.headline_grades) == ("A", "FDA", 0, {0: "A", 1: "C"})
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "CATALYST GRADE (A-F)" in sent["system"][0]["text"] and "earnings_beat" in sent["system"][0]["text"]
    assert sent["output_config"]["effort"] == "low" and sent["output_config"]["format"]["type"] == "json_schema"
    user = sent["messages"][0]["content"]
    assert "[1]" in user and "GlobeNewswire" in user and "SEC filings" in user and "8-K" in user
    assert NOW.strftime("%H:%M") not in sent["system"][0]["text"]       # nothing volatile in the cached prefix
    assert logged[0].agent == "newsie" and logged[0].cache_read_tokens == 2500 and logged[0].cost_usd > 0


def test_claude_grader_refusal_is_an_error(make_settings):
    s = make_settings()
    g = G.ClaudeGrader("sk-test", "claude-sonnet-5", 2048, 60, 2, "sys", s.config.llm.pricing, lambda u: None)
    g.client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: SimpleNamespace(
        stop_reason="max_tokens", content=[],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0,
                              cache_creation_input_tokens=0))))
    with pytest.raises(G.GradeError):
        g.grade(G.GradeInput("ABCD", NOW, NOW, [hl("x")], FilingFlags(), [], {}))


# ---------------------------------------------------------------------------
# local grader (WTBot hints2)
# ---------------------------------------------------------------------------

def test_wtbot_user_hints_format_is_byte_exact():
    arts = [{"title": "ABCD prices $8 million registered direct offering at $1.10", "description": ""}]
    assert G.wtbot_user_hints("ABCD", arts) == (
        "Stock ticker: ABCD\n\nRecent news:\n\n"
        "[1] Title: ABCD prices $8 million registered direct offering at $1.10\n     Description: \n\n"
        "Automated keyword scan of these articles (verify against the text; "
        "ignore if it refers to a different company):\n"
        "- offering (hard): \"registered direct offering\"\n"
        "- private_placement (hard): \"registered direct\"\n")


@pytest.mark.parametrize("ctype,conf,expect", [
    ("fda_approval", 0.92, ("A", "FDA")), ("fda_approval", 0.75, ("B", "FDA")),
    ("contract", 0.80, ("B", "Contract")), ("partnership", 0.55, ("C", "Partnership")),
    ("upgrade", 0.30, ("D", "Upgrade")), ("unclear", 0.35, ("D", "Other")), ("unclear", 0.50, ("C", "Other")),
    ("none", 0.10, ("F", "None")),
])
def test_wtbot_label_to_grade(ctype, conf, expect):
    assert G.wtbot_label_to_grade(ctype, conf, {}) == expect


def test_local_grader_hard_flag_overrides_to_negative():
    class Client:
        def chat_json(self, system, user, schema, think, max_tokens=2048):
            return LocalReply({"has_catalyst": True, "catalyst_type": "contract", "sentiment": "positive",
                               "confidence": 0.9, "summary": "Contract win."}, "qwen3:14b", 900, 40, 3.0)
    logged = []
    g = G.LocalGrader(Client(), "sys", True, logged.append)
    inp = G.GradeInput("ABCD", NOW, NOW, [hl("ABCD wins contract; prices $8 million registered direct offering")],
                       FilingFlags(), [], {"offering": {"severity": "hard", "match": "registered direct offering"}})
    r = g.grade(inp)
    assert (r.grade, r.catalyst_type, r.graded_by) == ("F", "Offering", "local")
    assert logged[0].local and logged[0].agent == "newsie_local"


# ---------------------------------------------------------------------------
# Ollama client + relevance filter
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        return self._body


def test_ollama_native_api_and_fallback_model(monkeypatch):
    calls = []

    def post(url, json=None, timeout=None):
        calls.append((url, json["model"], json["think"]))
        if json["model"] == "qwen3:14b":
            return _Resp(404, text="model not found")
        return _Resp(200, {"message": {"content": '{"ok": true}'}, "prompt_eval_count": 10, "eval_count": 3})
    monkeypatch.setattr("tradingdesk.news.local_llm.requests.post", post)
    c = OllamaClient("http://192.168.1.151:11434/v1", "qwen3:14b", "qwen3:8b")
    r = c.chat_json("s", "u", {"type": "object"}, think=True)
    assert r.data == {"ok": True} and r.model == "qwen3:8b"
    assert calls[0][0] == "http://192.168.1.151:11434/api/chat" and calls[0][2] is True


def test_ollama_bad_json_is_not_retried(monkeypatch):
    n = []

    def post(url, json=None, timeout=None):
        n.append(1)
        return _Resp(200, {"message": {"content": "not json"}})
    monkeypatch.setattr("tradingdesk.news.local_llm.requests.post", post)
    with pytest.raises(LocalLLMError):
        OllamaClient("http://x:11434", "qwen3:14b", None, max_retries=2).chat_json("s", "u", {}, think=False)
    assert len(n) == 1


def test_relevance_filter_fails_open():
    class Down:
        def chat_json(self, *a, **k):
            raise LocalLLMError("down")
    f = G.RelevanceFilter(Down(), False, lambda u: None)
    assert f.keep("ABCD", [hl("a"), hl("b")], NOW) == [True, True]


def test_relevance_filter_missing_answers_keep():
    class Partial:
        def chat_json(self, *a, **k):
            return LocalReply({"items": [{"n": 1, "keep": False}]}, "qwen3:14b", 50, 10, 1.0)
    f = G.RelevanceFilter(Partial(), False, lambda u: None)
    assert f.keep("ABCD", [hl("a"), hl("b")], NOW) == [False, True]


# ---------------------------------------------------------------------------
# EDGAR parsing (no network)
# ---------------------------------------------------------------------------

_FORM4 = """<?xml version="1.0"?><ownershipDocument><reportingOwner><reportingOwnerId>
<rptOwnerName>Jane Doe</rptOwnerName></reportingOwnerId></reportingOwner><nonDerivativeTable>
<nonDerivativeTransaction><transactionCoding><transactionCode>P</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>10000</value></transactionShares>
<transactionPricePerShare><value>2.15</value></transactionPricePerShare></transactionAmounts>
</nonDerivativeTransaction></nonDerivativeTable></ownershipDocument>"""


def test_edgar_flags(tmp_path, monkeypatch):
    e = EdgarClient("TradingDesk test@example.com", tmp_path, ["8-K", "S-1", "S-3", "424B*", "4"])
    sub = {"filings": {"recent": {
        "form": ["424B5", "8-K", "4", "10-Q"],
        "acceptanceDateTime": ["2026-11-09T12:05:00.000Z", "2026-11-08T21:00:00.000Z",
                               "2026-11-06T20:00:00.000Z", "2026-11-09T11:00:00.000Z"],
        "items": ["", "3.02,9.01", "", ""],
        "accessionNumber": ["0001-26-1", "0001-26-2", "0001-26-3", "0001-26-4"],
        "primaryDocument": ["a.htm", "b.htm", "xslF345X05/form4.xml", "d.htm"]}}}

    def get(url, kind="json", max_age_sec=None):
        if "company_tickers" in url:
            return {"0": {"ticker": "ABCD", "cik_str": 123}}
        if "submissions" in url:
            return sub
        return _FORM4
    monkeypatch.setattr(e, "_get", get)
    r = e.research("ABCD", NOW)
    assert r.flags.offering and r.flags.dilution and r.flags.insider_buy and not r.flags.insider_sell
    assert r.offering_today                                   # 424B5 accepted 07:05 ET today
    assert "10-Q" not in r.flags.forms                         # not a configured form
    titles = [h.title for h in r.headlines]
    assert any("424B5" in t for t in titles) and any("3.02" in t for t in titles)
    assert any("Jane Doe open-market BUY 10,000 sh @ $2.15" in t for t in titles)
    assert next(h for h in r.headlines if "Form 4" in h.title).url.endswith("/form4.xml")


def test_edgar_requires_user_agent(tmp_path):
    with pytest.raises(ValueError):
        EdgarClient("  ", tmp_path, ["8-K"])
