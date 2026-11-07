"""
Scout (plain Python). Wraps WTBot's 5 Pillars screener unchanged.

Each pass: price-filtered universe -> top % gainers (one batched request) ->
full WTBot snapshot (RVOL) + float for each -> WTBot check_all_pillars() ->
a Candidate for each ticker that qualifies, with a per-ticker cooldown.

Pillar 3 (catalyst) reads Newsie's cached grade. It never calls an LLM. With no
grade yet, it uses WTBot's existing "no news = neutral" path.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from loguru import logger

from tradingdesk.legacy.wtbot import pillar_checks
from tradingdesk.market import MarketData, Quote, UniverseCache
from tradingdesk.models import Agent, Candidate, NewsData, PillarCheck, ScoutData
from tradingdesk.research_cache import ResearchCache
from tradingdesk.sessions import SessionClock, TradingSession
from tradingdesk.settings import Settings

# Newsie grade -> the catalyst dict shape WTBot's pillar 3 expects.
# Pillar 3 passes only for a positive, non-"unclear" type with confidence >= 0.6,
# so A/B pass, C/D fall to WTBot's reduced-size path, F blocks.
_GRADE_TO_CATALYST: dict[str, tuple[float, str]] = {
    "A": (0.90, "positive"),
    "B": (0.75, "positive"),
    "C": (0.50, "neutral"),
    "D": (0.30, "neutral"),
    "F": (0.00, "negative"),
}
_NEGATIVE_TYPES = {"offering", "reverse split", "delisting", "failed trial", "dilution"}


def catalyst_from_news(news: NewsData | None) -> dict[str, Any]:
    """Convert a cached Newsie grade into WTBot's catalyst_result dict."""
    if news is None:
        return {"has_catalyst": False, "catalyst_type": "none", "sentiment": "neutral",
                "confidence": 0.0, "summary": "No Newsie grade yet.", "no_news": True}
    conf, sentiment = _GRADE_TO_CATALYST[news.catalyst_grade]
    ctype = news.catalyst_type.strip().lower()
    if news.catalyst_grade == "F":
        ctype = "negative" if ctype in _NEGATIVE_TYPES else "none"
        sentiment = "negative" if ctype == "negative" else "neutral"
    elif news.catalyst_grade in ("C", "D"):
        ctype = "unclear"
    return {"has_catalyst": news.catalyst_grade in ("A", "B"), "catalyst_type": ctype or "none",
            "sentiment": sentiment, "confidence": conf, "summary": news.grade_reason, "no_news": False}


@dataclass
class ScoutHit:
    """A qualifying candidate plus the raw WTBot dicts the Manager plans from (not persisted)."""

    candidate: Candidate
    snapshot: dict[str, Any]
    premarket_levels: dict[str, Any] | None
    session_levels: dict[str, Any] | None
    quote: Quote | None
    pillar_result: dict[str, Any] = field(default_factory=dict)


def _pillars(result: dict[str, Any]) -> dict[str, PillarCheck]:
    p1, p2, p3, p4, p5 = (result[k] for k in (
        "pillar_1_rvol", "pillar_2_momentum", "pillar_3_catalyst", "pillar_4_price", "pillar_5_float"))
    return {
        "rvol": PillarCheck(passed=p1["pass"], value=p1["value"], threshold=p1["threshold"]),
        "momentum": PillarCheck(passed=p2["pass"], value=p2["value"], threshold=p2["threshold"]),
        "catalyst": PillarCheck(passed=p3["pass"], value=p3["catalyst_type"],
                                note="no grade yet (neutral)" if p3["no_news"] else p3.get("summary") or None),
        "price": PillarCheck(passed=p4["pass"], value=p4["value"], threshold=f"{p4['min']}-{p4['max']}"),
        "float": PillarCheck(passed=p5["pass"], value=p5["value"], threshold=p5["threshold"], note=p5.get("note")),
    }


def _num(d: dict[str, Any] | None, key: str) -> float | None:
    v = (d or {}).get(key)
    return float(v) if isinstance(v, (int, float)) and v else None


class Scout:
    def __init__(self, settings: Settings, market: MarketData, cache: ResearchCache, clock: SessionClock) -> None:
        self.cfg = settings.config.scout
        self.market = market
        self.cache = cache
        self.clock = clock
        self.universe = UniverseCache(market, self.cfg.universe_refresh_min)
        self._last_emitted: dict[str, datetime] = {}

    def _cooling_down(self, ticker: str, now: datetime) -> bool:
        last = self._last_emitted.get(ticker)
        return last is not None and (now - last).total_seconds() < self.cfg.cooldown_sec

    def scan(self, now: datetime) -> list[ScoutHit]:
        session = self.clock.trading_session_at(now)
        if session is None:
            return []
        p = self.cfg.pillars
        movers = self.market.movers(
            self.universe.get(), min(p.min_gap_pct, p.min_daily_change_pct), self.cfg.max_movers_per_pass
        )
        todo = [m.ticker for m in movers if not self._cooling_down(m.ticker, now)]
        if not todo:
            return []
        with ThreadPoolExecutor(max_workers=self.cfg.workers) as pool:
            results = list(pool.map(lambda t: self._evaluate(t, session, now), todo))
        hits = [h for h in results if h is not None]
        for h in hits:
            self._last_emitted[h.candidate.ticker] = now
        logger.info("scout: {} movers, {} checked, {} qualified", len(movers), len(todo), len(hits))
        return hits

    def _evaluate(self, ticker: str, session: TradingSession, now: datetime) -> ScoutHit | None:
        try:
            snap = self.market.snapshot(ticker)
            if not snap:
                return None
            float_shares = self.market.float_shares(ticker)
            catalyst = catalyst_from_news(self.cache.news(ticker, now))
            result = pillar_checks.check_all_pillars(
                snapshot=snap, float_shares=float_shares,
                has_catalyst=catalyst["has_catalyst"], catalyst_result=catalyst,
            )
            if not result["qualifies"]:
                logger.debug("scout: {} score {}/5, not qualified", ticker, result["score"])
                return None
            pm = self.market.premarket_levels(ticker)
            sess = self.market.session_levels(ticker) if session == "open" else None
            quote = self.market.quote(ticker)
            return ScoutHit(
                candidate=self._candidate(ticker, session, now, snap, float_shares, result, pm, sess, quote),
                snapshot=snap, premarket_levels=pm, session_levels=sess, quote=quote, pillar_result=result,
            )
        except Exception as exc:  # one bad ticker must not stop the pass
            logger.warning("scout: {} failed: {}", ticker, exc)
            return None

    def _candidate(self, ticker: str, session: TradingSession, now: datetime, snap: dict[str, Any],
                   float_shares: int | None, result: dict[str, Any], pm: dict[str, Any] | None,
                   sess: dict[str, Any] | None, quote: Quote | None) -> Candidate:
        key_levels = {k: v for k, v in {
            "pm_high": _num(pm, "pm_high"), "pm_low": _num(pm, "pm_low"), "pm_vwap": _num(pm, "pm_vwap"),
            "hod": _num(sess, "hod"), "lod": _num(sess, "lod"), "vwap": _num(sess, "vwap"),
            "prev_close": _num(snap, "prev_close"),
        }.items() if v is not None}
        data = ScoutData(
            price=snap["current_price"],
            change_pct=round(snap.get("daily_change_pct", 0.0), 2),
            gap_pct=round(snap.get("gap_pct", 0.0), 2),
            relative_volume=round(snap.get("rvol", 0.0), 2),
            volume=int(snap.get("today_volume") or 0),
            float_shares=float_shares,
            spread_pct=quote.spread_pct if quote else None,
            vwap=key_levels.get("vwap") or key_levels.get("pm_vwap"),
            day_open=_num(snap, "open_price"),
            premarket_high=key_levels.get("pm_high"),
            high_of_day=key_levels.get("hod"),
            key_levels=key_levels,
            pillars=_pillars(result),
            qualifies_reduced=bool(result.get("qualifies_reduced")),
        )
        c = Candidate(ticker=ticker, session=session, created_at=now)
        c.attach("scout", data)
        path = "all 5 pillars" if result["all_pass"] else "reduced-size path (catalyst missing/vague)"
        c.record_pass(Agent.SCOUT, [f"{result['score']}/5 pillars, {path}"], score=result["score"])
        return c
