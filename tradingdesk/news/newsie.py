"""
Newsie (LLM lane, background). Never in the hot path: the Scout and Manager
only read what Newsie has already put in the ResearchCache.

Watchlist: every Scout pass reports its top % movers (and during Startup the
runner peeks at movers before tickets are allowed). Newsie researches the
biggest movers first, drops tickers that haven't been movers for
news.watch_minutes, and polls only during Startup / Premarket / Open.

Per ticker, each pass:
  1. fetch every enabled source in parallel (each fails on its own)
  2. dedupe across sources (URL / title similarity, earliest time kept)
  3. drop junk stubs and roundups that never name the ticker (WTBot rules, no model)
  4. local qwen filter on headlines it hasn't seen: about this ticker and material?
  5. fingerprint the relevant set; unchanged since the last grade -> no LLM call
  6. grade: Claude, or local when the spend cap is hit / Claude fails, or rules
  7. code-enforced rubric rules: older-than-session news drops one grade; an
     offering filed today caps at D
  8. cache + journal
"""
from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta
from typing import Callable

from loguru import logger

from tradingdesk.health import Health
from tradingdesk.models import GradedHeadline, Headline, NewsData
from tradingdesk.news import flags as kw
from tradingdesk.news.dedup import dedupe, story_key
from tradingdesk.news.edgar import EdgarClient, EdgarResult
from tradingdesk.news.grader import (GRADES, ClaudeGrader, GradeError, GradeInput, GradeResult, LocalGrader,
                                     RelevanceFilter, RulesGrader)
from tradingdesk.news.sources import NewsSource
from tradingdesk.research_cache import ResearchCache
from tradingdesk.sessions import ET, Phase, SessionClock, now_et
from tradingdesk.watchlist import Watchlist

_ACTIVE = (Phase.STARTUP, Phase.PREMARKET, Phase.OPEN)
_OFFERING_FLAGS = {"offering", "private_placement", "share_issuance", "warrants", "convertible", "financing"}


def session_news_start(now: datetime, close: time = time(16, 0)) -> datetime:
    """News after the previous trading day's close counts as this session's news (weekends skipped)."""
    d = now.astimezone(ET).date()
    today_close = datetime.combine(d, close, ET)
    if now >= today_close and d.weekday() < 5:
        return today_close
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return datetime.combine(d, close, ET)


def drop_one(grade: str) -> str:
    return GRADES[min(GRADES.index(grade) + 1, len(GRADES) - 1)]


class Newsie:
    def __init__(self, cfg, journal, cache: ResearchCache, clock: SessionClock, health: Health,
                 sources: list[NewsSource], edgar: EdgarClient | None, relevance: RelevanceFilter | None,
                 claude: ClaudeGrader | None, local: LocalGrader | None, spend_cap_usd: float,
                 off_sources: dict[str, str] | None = None, now_fn: Callable[[], datetime] = now_et) -> None:
        self.cfg = cfg
        self.journal = journal
        self.cache = cache
        self.clock = clock
        self.health = health
        self.sources = sources
        self.edgar = edgar
        self.relevance = relevance
        self.claude = claude
        self.local = local
        self.rules = RulesGrader()
        self.spend_cap = spend_cap_usd
        self.off_sources = off_sources or {}
        self.now = now_fn
        self._watch = Watchlist(cfg.watch_minutes, cfg.max_watch)
        self._fingerprints: dict[str, str] = {}
        self._keep: dict[tuple[str, str], bool] = {}    # (ticker, story) -> relevance verdict
        self._wake = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=cfg.workers, thread_name_prefix="newsie-fetch")
        self._restore()

    # -- watchlist -----------------------------------------------------------------------------
    def watch(self, tickers: list[str], now: datetime | None = None) -> None:
        if self._watch.add(tickers, now or self.now()):
            self._wake.set()

    def watchlist(self, now: datetime | None = None) -> list[str]:
        return self._watch.current(now or self.now())

    # -- loop ----------------------------------------------------------------------------------------
    def start(self, stop: threading.Event) -> threading.Thread:
        def loop() -> None:
            while not stop.is_set():
                now = self.now()
                if self.clock.phase_at(now) in _ACTIVE:
                    try:
                        self.run_pass(now, stop)
                    except Exception as exc:
                        logger.exception("newsie pass failed")
                        self.health.error("newsie", f"pass failed: {exc}")
                self._wake.wait(self.cfg.poll_interval_sec)
                self._wake.clear()

        th = threading.Thread(target=loop, name="newsie", daemon=True)
        th.start()
        return th

    def run_pass(self, now: datetime | None = None, stop: threading.Event | None = None) -> int:
        tickers = self.watchlist(now)
        graded = 0
        for t in tickers:
            if stop is not None and stop.is_set():
                break
            try:
                if self.research(t, self.now() if now is None else now):
                    graded += 1
            except Exception as exc:
                logger.exception("newsie: {} failed", t)
                self.health.error("newsie", f"{t}: {exc}")
        self.health.ok("newsie", f"{len(tickers)} watched, {graded} graded this pass")
        return graded

    # -- one ticker ----------------------------------------------------------------------------------
    def research(self, ticker: str, now: datetime) -> bool:
        """Refresh one ticker. Returns True if it was (re)graded."""
        since = now - timedelta(hours=self.cfg.lookback_hours)
        raw = self._fetch_all(ticker, since)
        edgar = self._edgar(ticker, now)
        news = [h for h in dedupe([h for h in raw if h.source != "edgar"], self.cfg.dedup_title_similarity)]
        news = self._prefilter(ticker, news)
        news = self._relevant(ticker, news, now)
        graded = news[: self.cfg.max_headlines_to_grade]
        filing_lines = [h.title for h in edgar.headlines]

        fp = hashlib.sha1("\n".join(sorted(story_key(h) for h in graded) + sorted(filing_lines)).encode()).hexdigest()
        if self._fingerprints.get(ticker) == fp and self.cache.news(ticker, now) is not None:
            return False

        inp = GradeInput(ticker=ticker, now=now, session_start=session_news_start(now), headlines=graded,
                         filing_flags=edgar.flags, filing_lines=filing_lines,
                         keyword_flags=kw.news_flags(ticker, kw.as_articles(graded)))
        result = self._grade(inp)
        result = self._enforce_rules(inp, result, edgar)
        data = NewsData(
            catalyst_grade=result.grade, catalyst_type=result.catalyst_type, grade_reason=result.reason,
            headlines=tuple(GradedHeadline(headline=h, grade=result.headline_grades.get(i))
                            for i, h in enumerate(graded)) + tuple(GradedHeadline(headline=h) for h in edgar.headlines),
            filing_flags=edgar.flags, graded_at=now, graded_by=result.graded_by,
        )
        self.cache.put_news(ticker, data)
        self.journal.save_news_grade(ticker, fp, data)
        self._fingerprints[ticker] = fp
        logger.info("newsie: {} Catalyst {} · {} ({}) — {}", ticker, data.catalyst_grade, data.catalyst_type,
                    data.graded_by, data.grade_reason)
        return True

    def _fetch_all(self, ticker: str, since: datetime) -> list[Headline]:
        def one(src: NewsSource) -> list[Headline]:
            try:
                got = src.fetch(ticker, since)
                self.health.ok(f"news:{src.name}", f"{len(got)} for {ticker}")
                return got
            except Exception as exc:                    # one source failing never stops the others
                logger.warning("newsie: {} failed for {}: {}", src.name, ticker, exc)
                self.health.error(f"news:{src.name}", str(exc))
                return []
        out: list[Headline] = []
        for got in self._pool.map(one, self.sources):
            out.extend(got)
        return out

    def _edgar(self, ticker: str, now: datetime) -> EdgarResult:
        if self.edgar is None:
            return EdgarResult()
        try:
            return self.edgar.research(ticker, now)
        except Exception as exc:
            logger.warning("newsie: EDGAR failed for {}: {}", ticker, exc)
            self.health.error("news:edgar", str(exc))
            return EdgarResult()

    @staticmethod
    def _prefilter(ticker: str, headlines: list[Headline]) -> list[Headline]:
        out = []
        for h in headlines:
            a = {"title": h.title, "description": h.summary}
            if kw.is_junk(h.title):
                continue
            if kw._is_roundup_article(a) and not kw._ticker_named(ticker, a):
                continue
            out.append(h)
        return out

    def _relevant(self, ticker: str, headlines: list[Headline], now: datetime) -> list[Headline]:
        if self.relevance is None or not self.cfg.local_filter.enabled:
            return headlines
        unknown = [h for h in headlines if (ticker, story_key(h)) not in self._keep]
        if unknown:
            for h, keep in zip(unknown, self.relevance.keep(ticker, unknown, now)):
                self._keep[(ticker, story_key(h))] = keep
        return [h for h in headlines if self._keep.get((ticker, story_key(h)), True)]

    def _over_cap(self, now: datetime) -> bool:
        try:
            return self.journal.llm_spend(now.astimezone(ET).date().isoformat()) >= self.spend_cap
        except Exception:
            return True                                 # unknown spend: don't risk Claude

    def _grade(self, g: GradeInput) -> GradeResult:
        if not g.headlines and not g.filing_lines:
            return GradeResult("F", "None", "No company-specific news found for this ticker.", "rules")
        tried: list[str] = []
        if self.claude is not None and not self._over_cap(g.now):
            try:
                return self.claude.grade(g)
            except GradeError as exc:
                tried.append(f"Claude: {exc}")
                logger.warning("newsie: Claude grading failed for {}: {}", g.ticker, exc)
        elif self.claude is not None:
            tried.append("daily LLM spend cap reached")
        if self.local is not None:
            try:
                return self.local.grade(g)
            except GradeError as exc:
                tried.append(f"local: {exc}")
                logger.warning("newsie: local grading failed for {}: {}", g.ticker, exc)
        r = self.rules.grade(g)
        if tried:
            logger.info("newsie: {} graded by rules ({})", g.ticker, "; ".join(tried))
        return r

    @staticmethod
    def _enforce_rules(g: GradeInput, r: GradeResult, edgar: EdgarResult) -> GradeResult:
        grade, ctype, notes = r.grade, r.catalyst_type, []
        # freshness: the headline that drove the grade is older than the current session -> one letter down
        key = g.headlines[r.key_index] if r.key_index is not None and r.key_index < len(g.headlines) else None
        if key is not None and grade != "F" and key.published_at < g.session_start:
            grade = drop_one(grade)
            notes.append("older than this session: dropped one grade")
        # offering filed today caps at D
        today = g.now.astimezone(ET).date()
        kw_offering_today = any(
            h.published_at.astimezone(ET).date() == today and set(kw.news_flags(g.ticker, kw.as_articles([h])))
            & _OFFERING_FLAGS for h in g.headlines)
        if (edgar.offering_today or kw_offering_today) and GRADES.index(grade) < GRADES.index("D"):
            grade, ctype = "D", "Offering"
            notes.append("offering filed today: capped at D")
        if not notes:
            return r
        reason = f"{r.reason} ({'; '.join(notes)})"[:300]
        return GradeResult(grade, ctype, reason, r.graded_by, r.key_index, r.headline_grades)

    # -- restart -----------------------------------------------------------------------------------
    def _restore(self) -> None:
        """Reload today's grades so a restart doesn't re-grade unchanged stories."""
        try:
            saved = self.journal.news_grades(self.now().astimezone(ET).date().isoformat())
        except Exception as exc:
            logger.warning("newsie: could not reload today's grades: {}", exc)
            return
        for t, (fp, data) in saved.items():
            self.cache.put_news(t, data)
            self._fingerprints[t] = fp
        if saved:
            logger.info("newsie: reloaded {} grade(s) from today", len(saved))

    def status(self) -> dict[str, str]:
        """Source name -> why it's off, for the desk."""
        return dict(self.off_sources)


def build_newsie(settings, journal, cache: ResearchCache, clock: SessionClock, health: Health,
                 now_fn: Callable[[], datetime] = now_et) -> Newsie:
    from tradingdesk.news.edgar import make_edgar
    from tradingdesk.news.grader import load_claude_system
    from tradingdesk.news.local_llm import OllamaClient
    from tradingdesk.news.sources import build_sources

    cfg, llm = settings.config.news, settings.config.llm
    edgar = make_edgar(settings)
    sources, off = build_sources(settings, edgar=edgar)
    if settings.source_enabled("news", "edgar") and edgar is None:
        off["edgar"] = "set SEC_USER_AGENT in .env"
    log = journal.log_llm_usage
    ollama = OllamaClient(llm.local.base_url, llm.local.model, llm.local.fallback_model, llm.local.timeout_sec,
                          max_retries=min(llm.max_retries, 1))
    claude = None
    if settings.secrets.anthropic_api_key:
        system = load_claude_system(settings.path(cfg.types_file), settings.path(cfg.rubric_file))
        claude = ClaudeGrader(settings.secrets.anthropic_api_key, llm.claude.newsie_model, llm.claude.newsie_max_tokens,
                              llm.claude.timeout_sec, llm.max_retries, system, llm.pricing, log)
    else:
        logger.warning("newsie: ANTHROPIC_API_KEY not set; grading with the local model only")
    local_prompt = settings.path(cfg.types_file).parent / "local_catalyst_hints2.md"
    local = LocalGrader(ollama, local_prompt.read_text(encoding="utf-8"), llm.local.thinking, log) \
        if local_prompt.exists() else None
    relevance = RelevanceFilter(ollama, cfg.local_filter.thinking, log) if cfg.local_filter.enabled else None
    logger.info("newsie: sources {} | Claude {} | local {}", ", ".join(s.name for s in sources) or "none",
                llm.claude.newsie_model if claude else "off", llm.local.model)
    return Newsie(cfg, journal, cache, clock, health, sources, edgar, relevance, claude, local,
                  llm.daily_spend_cap_usd, off, now_fn)
