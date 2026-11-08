"""
Sentiment (background). Writes SentimentData to the ResearchCache; the
Manager only reads it. Polls during Startup / Premarket / Open, only for
tickers on the Scout's radar.

Each poll:
  1. market regime (plain Python, refreshed every regime.refresh_sec), shared by every ticker
  2. per watched ticker, each enabled social source (fails on its own; StockTwits = 1 call)
  3. promotion checks (spam.py): duplicates, floods, pump language -> promo warning for the Skeptic
  4. local tone of recent posts (qwen, free, optional)
  5. buzz = chatter level (0-100) discounted by the promo share; trend vs our reading
     trend_lookback_min ago (or the platform's day level before we have one)
"""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from loguru import logger

from tradingdesk.health import Health
from tradingdesk.models import SentimentData
from tradingdesk.research_cache import ResearchCache
from tradingdesk.sessions import Phase, SessionClock, now_et
from tradingdesk.social import spam
from tradingdesk.social.base import SocialReading, SocialSource, SourcePaused
from tradingdesk.social.regime import RegimeData, RegimeResult, compute_regime, summary
from tradingdesk.social.tone import ToneScorer
from tradingdesk.watchlist import Watchlist

_ACTIVE = (Phase.STARTUP, Phase.PREMARKET, Phase.OPEN)


def trend(now_level: float | None, history: list[tuple[datetime, float]], day_level: float | None,
          now: datetime, lookback_min: float, points: float) -> str:
    if now_level is None:
        return "unknown"
    cutoff = now - timedelta(minutes=lookback_min)
    past = [v for t, v in history if t <= cutoff]
    base = past[-1] if past else day_level
    if base is None:
        return "flat"
    if now_level - base >= points:
        return "rising"
    if base - now_level >= points:
        return "fading"
    return "flat"


class Sentiment:
    def __init__(self, cfg, cache: ResearchCache, clock: SessionClock, health: Health,
                 sources: list[SocialSource], regime_data: RegimeData | None, tone: ToneScorer | None,
                 off_sources: dict[str, str] | None = None, now_fn: Callable[[], datetime] = now_et) -> None:
        self.cfg = cfg
        self.cache = cache
        self.clock = clock
        self.health = health
        self.sources = sources
        self.regime_data = regime_data
        self.tone = tone
        self.off_sources = off_sources or {}
        self.now = now_fn
        self._watch = Watchlist(cfg.watch_minutes, cfg.max_watch)
        self._history: dict[str, deque[tuple[datetime, float]]] = {}
        self._regime: RegimeResult | None = None
        self._regime_at: datetime | None = None
        self._wake = threading.Event()

    # -- watchlist / loop ----------------------------------------------------------------------
    def watch(self, tickers: list[str], now: datetime | None = None) -> None:
        if self._watch.add(tickers, now or self.now()):
            self._wake.set()

    def watchlist(self, now: datetime | None = None) -> list[str]:
        return self._watch.current(now or self.now())

    def start(self, stop: threading.Event) -> threading.Thread:
        def loop() -> None:
            while not stop.is_set():
                now = self.now()
                if self.clock.phase_at(now) in _ACTIVE:
                    try:
                        self.run_pass(now, stop)
                    except Exception as exc:
                        logger.exception("sentiment pass failed")
                        self.health.error("sentiment", f"pass failed: {exc}")
                self._wake.wait(self.cfg.poll_interval_sec)
                self._wake.clear()

        th = threading.Thread(target=loop, name="sentiment", daemon=True)
        th.start()
        return th

    def run_pass(self, now: datetime | None = None, stop: threading.Event | None = None) -> int:
        now = now or self.now()
        regime = self.regime(now)
        tickers = self.watchlist(now)
        done = 0
        for t in tickers:
            if stop is not None and stop.is_set():
                break
            try:
                self.research(t, now, regime)
                done += 1
            except Exception as exc:
                logger.exception("sentiment: {} failed", t)
                self.health.error("sentiment", f"{t}: {exc}")
        self.health.ok("sentiment", f"{done}/{len(tickers)} tickers; regime {regime.regime if regime else 'n/a'}")
        return done

    # -- regime ----------------------------------------------------------------------------------
    def regime(self, now: datetime) -> RegimeResult | None:
        rc = self.cfg.regime
        if self._regime_at and (now - self._regime_at).total_seconds() < rc.refresh_sec:
            return self._regime
        if self.regime_data is None:
            return self._regime
        try:
            idx = self.regime_data.indexes(rc.symbols, rc.sma_days)
            if not idx:
                raise RuntimeError("no index data")
            self._regime = compute_regime(idx, self.regime_data.vix(rc.vix_symbol), rc)
            self._regime_at = now
            self.cache.put_regime(self._regime.regime, {**self._regime.detail, "summary": summary(self._regime)}, now)
            self.health.ok("regime", f"{self._regime.regime} ({summary(self._regime)})")
        except Exception as exc:
            logger.warning("regime refresh failed: {}", exc)
            self.health.error("regime", str(exc))
        return self._regime

    # -- one ticker -------------------------------------------------------------------------------
    def research(self, ticker: str, now: datetime, regime: RegimeResult | None = None) -> SentimentData:
        readings: list[SocialReading] = []
        for src in self.sources:
            try:
                readings.append(src.read(ticker, now))
                self.health.ok(f"social:{src.name}", ticker)
            except SourcePaused as exc:
                self.health.error(f"social:{src.name}", str(exc))
            except Exception as exc:                      # one source failing never stops the others
                logger.warning("sentiment: {} failed for {}: {}", src.name, ticker, exc)
                self.health.error(f"social:{src.name}", str(exc))
        posts = sorted((p for r in readings for p in r.posts), key=lambda p: p.created_at, reverse=True)
        rep = spam.check(posts, now, self.cfg.promo_share)
        levels = [r.volume_now for r in readings if r.volume_now is not None]
        level = max(levels) if levels else None
        buzz = round(level * (1 - 0.5 * rep.share), 1) if level is not None else None
        hist = self._history.setdefault(ticker, deque(maxlen=120))
        day = next((r.volume_day for r in readings if r.volume_day is not None), None)
        tr = trend(buzz, list(hist), day, now, self.cfg.trend_lookback_min, self.cfg.trend_points)
        if buzz is not None:
            hist.append((now, buzz))
        clean = [p for p in posts if p.id not in rep.flagged]
        tone = None
        if self.tone is not None and self.cfg.local_tone.enabled and clean:
            tone = self.tone.score(ticker, clean, now)
        crowd = next((r for r in readings if r.crowd_score is not None), None)
        reg = regime or self._regime
        data = SentimentData(
            buzz_score=buzz, buzz_trend=tr, promo_warning=rep.warning, promo_reasons=tuple(rep.reasons),
            crowd_score=crowd.crowd_score if crowd else None, crowd_label=crowd.crowd_label if crowd else None,
            tone=tone, sources=tuple(r.platform for r in readings),
            detail={"posts": len(posts), "promo_share": rep.share, "chatter_level": level,
                    "watchers": next((r.watchers for r in readings if r.watchers), None),
                    "volume_label": next((r.extra.get("volume_label") for r in readings
                                          if r.extra.get("volume_label")), None)},
            market_regime=reg.regime if reg else "neutral",
            regime_detail=({**reg.detail, "summary": summary(reg)} if reg else {"note": "regime unavailable"}),
            as_of=now,
        )
        self.cache.put_sentiment(ticker, data)
        return data

    def status(self) -> dict[str, str]:
        return dict(self.off_sources)


def build_sentiment(settings, journal, cache: ResearchCache, clock: SessionClock, health: Health,
                    now_fn: Callable[[], datetime] = now_et) -> Sentiment:
    from tradingdesk.news.local_llm import OllamaClient
    from tradingdesk.social.regime import LiveRegimeData

    cfg, llm = settings.config.sentiment, settings.config.llm
    sources: list[SocialSource] = []
    off: dict[str, str] = {}
    if settings.source_enabled("sentiment", "stocktwits"):
        extra = cfg.sources["stocktwits"].model_extra or {}
        token = settings.path(Path(extra.get("token_file", "data/stocktwits_token.json")))
        if token.exists():
            from tradingdesk.social.stocktwits import StockTwitsSource
            sources.append(StockTwitsSource(token))
        else:
            off["stocktwits"] = "not logged in: run `python -m tradingdesk stocktwits-login`"
    for name, why in (("x", "waiting on the X API cost decision"), ("reddit", "waiting on Reddit API approval")):
        if settings.source_enabled("sentiment", name):
            off[name] = why
    for name, why in off.items():
        logger.warning("social source {} is off: {}", name, why)
    tone = None
    if cfg.local_tone.enabled:
        ollama = OllamaClient(llm.local.base_url, llm.local.model, llm.local.fallback_model, llm.local.timeout_sec,
                              max_retries=min(llm.max_retries, 1))
        tone = ToneScorer(ollama, cfg.local_tone.thinking, journal.log_llm_usage)
    logger.info("sentiment: sources {} | regime {} + {}", ", ".join(s.name for s in sources) or "none",
                "/".join(cfg.regime.symbols), cfg.regime.vix_symbol)
    return Sentiment(cfg, cache, clock, health, sources, LiveRegimeData(), tone, off, now_fn)
