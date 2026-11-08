"""Sentiment: StockTwits MCP client + source, promo checks, tone, regime, buzz trend. No network."""
from __future__ import annotations

import json
import time
from datetime import timedelta

import pytest

from tests.conftest import et
from tradingdesk.health import Health
from tradingdesk.news.local_llm import LocalLLMError, LocalReply
from tradingdesk.research_cache import ResearchCache
from tradingdesk.sessions import SessionClock
from tradingdesk.social import spam
from tradingdesk.social import stocktwits_mcp as mcp
from tradingdesk.social.base import SocialPost, SocialReading, SourcePaused
from tradingdesk.social.regime import IndexReading, VixReading, compute_regime, summary
from tradingdesk.social.sentiment import Sentiment, trend
from tradingdesk.social.stocktwits import StockTwitsSource, parse_pulse
from tradingdesk.social.tone import ToneScorer

NOW = et(2026, 11, 9, 8, 30)

PULSE = {
    "symbol": "ABCD", "change": 31.5, "watchers": {"count": 4200},
    "message_volume": {"label": "HIGH", "score": 70},
    "sentiment": {"score": 78, "label": "EXTREMELY_BULLISH", "bull_pct": 89.0},
    "volume": [{"timeframe": "now", "normalized_value": 70, "label": "HIGH"},
               {"timeframe": "1D", "normalized_value": 52, "label": "NORMAL"}],
    "posts": [
        {"id": 3, "body": "$ABCD FDA fast track is real news, holding", "created_at": "2026-11-09T13:20:00Z",
         "sentiment": "Bullish", "likes": 4, "user": {"username": "alice", "id": 1}},
        {"id": 2, "body": "$ABCD offering incoming, careful", "created_at": "2026-11-09T13:10:00Z",
         "sentiment": "Bearish", "user": {"username": "bob", "id": 2}},
        {"id": 1, "body": "", "created_at": "2026-11-09T13:00:00Z", "user": {"username": "x"}},
    ],
}


def post(i, text, author="u", mins=5, joined=None):
    return SocialPost("stocktwits", "ABCD", str(i), text, author, NOW - timedelta(minutes=mins), author_joined=joined)


# ---------------------------------------------------------------------------
# StockTwits pulse parsing + source politeness
# ---------------------------------------------------------------------------

def test_parse_pulse():
    r = parse_pulse("ABCD", PULSE, NOW)
    assert (r.volume_now, r.volume_day, r.crowd_score, r.crowd_label, r.watchers) == \
        (70.0, 52.0, 78.0, "EXTREMELY_BULLISH", 4200)
    assert [p.id for p in r.posts] == ["3", "2"]                  # empty body dropped
    assert r.posts[0].tag == "bullish" and r.posts[1].tag == "bearish" and r.posts[0].likes == 4
    assert r.posts[0].created_at.utcoffset() is not None


class FakeMCP:
    def __init__(self, errors=()):
        self.errors, self.calls = list(errors), []

    def call(self, name, args):
        self.calls.append((name, args))
        if self.errors:
            raise self.errors.pop(0)
        return {"structuredContent": PULSE}


def test_source_uses_one_pulse_call(tmp_path):
    fake = FakeMCP()
    r = StockTwitsSource(tmp_path / "t.json", client=fake).read("ABCD", NOW)
    assert fake.calls == [("get_symbol_pulse", {"symbol": "ABCD"})] and r.crowd_score == 78


def test_source_rests_after_rate_limit(tmp_path):
    fake = FakeMCP(errors=[mcp.StockTwitsError("rate limited (429), retry-after 120")])
    src = StockTwitsSource(tmp_path / "t.json", client=fake)
    with pytest.raises(SourcePaused) as e:
        src.read("ABCD", NOW)
    assert e.value.until == NOW + timedelta(seconds=120)
    with pytest.raises(SourcePaused):
        src.read("ABCD", NOW + timedelta(seconds=60))          # still resting: no call made
    assert len(fake.calls) == 1
    assert src.read("ABCD", NOW + timedelta(seconds=121)).crowd_score == 78


def test_three_rate_limits_rest_an_hour(tmp_path):
    err = lambda: mcp.StockTwitsError("rate limited (429), retry-after None")   # noqa: E731
    fake = FakeMCP(errors=[err(), err(), err()])
    src = StockTwitsSource(tmp_path / "t.json", client=fake)
    t = NOW
    rests = []
    for _ in range(3):
        with pytest.raises(SourcePaused) as e:
            src.read("ABCD", t)
        rests.append(e.value.until - t)
        t = e.value.until + timedelta(seconds=1)
    assert rests[:2] == [timedelta(minutes=5)] * 2 and rests[2] == timedelta(hours=1)


# ---------------------------------------------------------------------------
# MCP client: OAuth refresh + JSON-RPC over streamable HTTP
# ---------------------------------------------------------------------------

class _R:
    def __init__(self, status=200, body=None, headers=None, text=None):
        self.status_code, self._body = status, body
        self.headers = headers or {"Content-Type": "application/json"}
        self.text = text if text is not None else json.dumps(body)
        self.content = self.text.encode()

    def json(self):
        return self._body


def test_mcp_client_refreshes_token_and_parses_sse(tmp_path, monkeypatch):
    store = mcp.TokenStore(tmp_path / "tok.json")
    store.save({"client_id": "c1", "access_token": "old", "refresh_token": "r1", "expires_at": time.time() - 5})
    seen = []

    def post(url, json=None, data=None, headers=None, timeout=None):
        seen.append((url, (json or {}).get("method") or (data or {}).get("grant_type"),
                     (headers or {}).get("Authorization")))
        if url.endswith("/token"):
            return _R(body={"access_token": "new", "expires_in": 3600})
        if json["method"] == "initialize":
            return _R(body={"jsonrpc": "2.0", "id": json["id"], "result": {}}, headers={
                "Content-Type": "application/json", "Mcp-Session-Id": "s1"})
        if json["method"] == "notifications/initialized":
            return _R(status=202, text="")
        reply = {"jsonrpc": "2.0", "id": json["id"], "result": {"structuredContent": {"ok": 1}}}
        return _R(headers={"Content-Type": "text/event-stream"}, text=f"event: message\ndata: {json_dumps(reply)}\n\n")
    json_dumps = json.dumps
    monkeypatch.setattr(mcp.requests, "post", post)
    res = mcp.StockTwitsMCP(store).call("get_symbol_pulse", {"symbol": "ABCD"})
    assert res["structuredContent"] == {"ok": 1}
    assert seen[0][1] == "refresh_token"                        # expired token refreshed first
    assert all(a == "Bearer new" for u, m, a in seen if u.endswith("/mcp"))
    saved = store.load()
    assert saved["access_token"] == "new" and saved["refresh_token"] == "r1" and saved["client_id"] == "c1"


def test_mcp_client_requires_login(tmp_path):
    with pytest.raises(mcp.StockTwitsAuthError):
        mcp.StockTwitsMCP(mcp.TokenStore(tmp_path / "none.json")).tools()


def test_mcp_tool_error_raises(tmp_path, monkeypatch):
    store = mcp.TokenStore(tmp_path / "tok.json")
    store.save({"client_id": "c", "access_token": "a", "refresh_token": "r", "expires_at": time.time() + 999})

    def post(url, json=None, data=None, headers=None, timeout=None):
        if json.get("method") == "tools/call":
            return _R(body={"jsonrpc": "2.0", "id": json["id"],
                            "result": {"isError": True, "content": [{"type": "text", "text": "unknown symbol"}]}})
        return _R(body={"jsonrpc": "2.0", "id": json.get("id"), "result": {}})
    monkeypatch.setattr(mcp.requests, "post", post)
    with pytest.raises(mcp.StockTwitsError, match="unknown symbol"):
        mcp.StockTwitsMCP(store).call("get_symbol_pulse", {"symbol": "ZZZZ"})


# ---------------------------------------------------------------------------
# promotion checks
# ---------------------------------------------------------------------------

def test_spam_flags_duplicates_floods_and_pump_language():
    posts = [post(1, "$ABCD huge news today, this is going to run hard into the open guys"),
             post(2, "$ABCD huge news today, this is going to run hard into the open guys!!", author="v"),
             post(3, "$ABCD huge news today this is going to run hard into the open guys", author="w"),
             post(4, "$ABCD next 10x, join my discord for alerts", author="p"),
             post(5, "$ABCD FDA fast track looks real, small position", author="q"),
             post(6, "$ABCD what time is the call?", author="r")]
    rep = spam.check(posts, NOW)
    assert rep.flagged["1"] == ["duplicate"] and "pump" in rep.flagged["4"]
    assert "5" not in rep.flagged and rep.warning and rep.share == 0.67
    assert any("duplicate" in r for r in rep.reasons)


def test_spam_quiet_stream_has_no_warning():
    posts = [post(1, "$ABCD holding the 4.20 level nicely on volume", author="a"),
             post(2, "$ABCD anyone know when the conference call is?", author="b"),
             post(9, "$ABCD earnings next week", author="z")]
    rep = spam.check(posts, NOW)
    assert not rep.warning and rep.flagged == {}


def test_spam_flood_and_new_accounts():
    posts = [post(i, f"$ABCD distinct message {i} about levels {i * 11}", author="same") for i in range(3)]
    posts.append(post(7, "$ABCD hello", author="fresh", joined=NOW - timedelta(days=3)))
    rep = spam.check(posts, NOW, min_posts=3)
    assert all("flood" in rep.flagged[str(i)] for i in range(3)) and rep.flagged["7"] == ["new_account"]


# ---------------------------------------------------------------------------
# tone
# ---------------------------------------------------------------------------

class FakeOllama:
    def __init__(self, tones=None, fail=False):
        self.tones, self.fail, self.calls = tones or {}, fail, 0

    def chat_json(self, system, user, schema, think, max_tokens=1024):
        self.calls += 1
        if self.fail:
            raise LocalLLMError("down")
        n = user.count("\n[")
        return LocalReply({"items": [{"n": i, "tone": self.tones.get(i, "neutral")} for i in range(1, n + 1)]},
                          "qwen3:14b", 100, 20, 1.0)


def test_tone_scores_once_per_post():
    llm = FakeOllama({1: "bullish", 2: "bullish", 3: "bearish"})
    logged = []
    t = ToneScorer(llm, False, logged.append)
    posts = [post(1, "a"), post(2, "b"), post(3, "c"), post(4, "d")]
    assert t.score("ABCD", posts, NOW) == round((2 - 1) / 3, 2)
    assert t.score("ABCD", posts, NOW) == round((2 - 1) / 3, 2) and llm.calls == 1
    assert logged[0].local and logged[0].agent == "sentiment_tone"


def test_tone_fails_quietly():
    assert ToneScorer(FakeOllama(fail=True), False, lambda u: None).score("ABCD", [post(1, "a")], NOW) is None


# ---------------------------------------------------------------------------
# regime
# ---------------------------------------------------------------------------

@pytest.fixture
def rcfg(make_settings):
    return make_settings().config.sentiment.regime


def test_regime_supportive_hostile_neutral(rcfg):
    up = [IndexReading(s, 101, 100, 99) for s in ("SPY", "QQQ", "IWM")]       # above avg, +1% each
    r = compute_regime(up, VixReading(15, 15.5), rcfg)
    assert r.regime == "supportive" and r.score == 7
    down = [IndexReading(s, 98, 100, 99.5) for s in ("SPY", "QQQ", "IWM")]
    r = compute_regime(down, VixReading(28, 24), rcfg)                         # VIX high and spiking
    assert r.regime == "hostile" and r.score == -8
    mixed = [IndexReading("SPY", 100.1, 100, 99), IndexReading("QQQ", 99.9, 100, 101),
             IndexReading("IWM", 100, 100, None)]
    r = compute_regime(mixed, None, rcfg)
    assert r.regime == "neutral" and r.detail["parts"]["VIX"]["note"] == "unavailable"
    assert summary(r).endswith("VIX n/a")


# ---------------------------------------------------------------------------
# buzz trend + the agent
# ---------------------------------------------------------------------------

def test_trend():
    assert trend(None, [], None, NOW, 10, 10) == "unknown"
    assert trend(70, [], 52, NOW, 10, 10) == "rising"                          # vs platform's day level
    assert trend(55, [], 52, NOW, 10, 10) == "flat"
    hist = [(NOW - timedelta(minutes=15), 80.0), (NOW - timedelta(minutes=2), 60.0)]
    assert trend(62, hist, 30, NOW, 10, 10) == "fading"                       # vs our reading 10+ min ago


class FakeSocial:
    def __init__(self, reading=None, fail=False, name="stocktwits"):
        self.name, self.reading, self.fail = name, reading, fail

    def read(self, ticker, now):
        if self.fail:
            raise RuntimeError("down")
        return self.reading


class FakeRegime:
    def __init__(self):
        self.calls = 0

    def indexes(self, symbols, sma_days):
        self.calls += 1
        return [IndexReading(s, 101, 100, 99) for s in symbols]

    def vix(self, symbol):
        return VixReading(15, 15)


@pytest.fixture
def agent(make_settings):
    s = make_settings()

    def make(sources, regime=None, tone=None):
        return Sentiment(s.config.sentiment, ResearchCache(), SessionClock(s.config.sessions), Health(),
                         sources, regime, tone, now_fn=lambda: NOW)
    return make


def test_research_builds_sentiment_with_regime(agent):
    reading = parse_pulse("ABCD", PULSE, NOW)
    a = agent([FakeSocial(reading)], FakeRegime(), ToneScorer(FakeOllama({1: "bullish"}), False, lambda u: None))
    a.watch(["ABCD"])
    assert a.run_pass(NOW) == 1
    d = a.cache.sentiment("ABCD", NOW)
    assert d.buzz_score == 70.0 and d.buzz_trend == "rising" and d.crowd_score == 78
    assert d.market_regime == "supportive" and "VIX 15.0" in d.regime_detail["summary"]
    assert d.sources == ("stocktwits",) and d.tone is not None and not d.promo_warning
    assert a.cache.regime(NOW).market_regime == "supportive"
    assert a.health.get("sentiment").ok and a.health.get("regime").ok


def test_promo_discounts_buzz_and_warns(agent):
    spammy = [post(i, "$ABCD next 10x squeeze incoming, join my discord", author=f"a{i}") for i in range(6)]
    reading = SocialReading("stocktwits", "ABCD", NOW, posts=spammy, volume_now=80, crowd_score=95)
    d = agent([FakeSocial(reading)]).research("ABCD", NOW)
    assert d.promo_warning and d.buzz_score == 40.0 and d.promo_reasons


def test_source_failure_still_gives_regime(agent):
    a = agent([FakeSocial(fail=True)], FakeRegime())
    a.watch(["ABCD"])
    a.run_pass(NOW)
    d = a.cache.sentiment("ABCD", NOW)
    assert d.buzz_score is None and d.buzz_trend == "unknown" and d.market_regime == "supportive"
    assert not a.health.get("social:stocktwits").ok


def test_regime_refreshes_on_its_own_schedule(agent):
    reg = FakeRegime()
    a = agent([], reg)
    a.regime(NOW)
    a.regime(NOW + timedelta(seconds=30))
    a.regime(NOW + timedelta(seconds=121))
    assert reg.calls == 2


def test_manager_attaches_regime_when_ticker_has_no_social_read(make_settings, tmp_path):
    from tests.test_execution import OPEN_NOW, Rig
    from tradingdesk.market import Quote

    r = Rig(make_settings(), tmp_path, regular=True)
    r.market.add("ABCD", price=4.95, pm_high=4.60, hod=5.00, quote=Quote(4.95, 4.94, 4.96))
    r.price("ABCD", 4.95)
    r.app.pipeline.cache.put_regime("hostile", {"summary": "SPY -1.2%, VIX 27.0"}, OPEN_NOW)
    c = r.ticket(OPEN_NOW)
    assert c.sentiment.market_regime == "hostile" and c.sentiment.buzz_score is None
    r.journal.close()


def test_scout_feeds_both_agents(make_settings, tmp_path):
    from tests.fakes import FakeMarket
    from tradingdesk.journal import Journal
    from tradingdesk.runner import build_app

    m = FakeMarket()
    m.add("ABCD")
    j = Journal(tmp_path / "j.db")
    app = build_app(make_settings(), j, market=m, threaded_closes=False, sleep=lambda s: None)
    app.pipeline.scout.scan(NOW)
    assert app.newsie.watchlist() == ["ABCD"] and app.sentiment.watchlist() == ["ABCD"]
    assert "stocktwits" in app.sentiment.status()       # test settings have no token file -> off with a reason
    j.close()
