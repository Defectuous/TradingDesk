"""
Background research cache. Newsie (phase 4) and Sentiment (phase 5) write here;
the Scout and Manager only read. Reads never trigger an LLM call: that's what
keeps LLMs out of the hot path.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta

from tradingdesk.models import NewsData, SentimentData
from tradingdesk.sessions import now_et


class ResearchCache:
    def __init__(self, max_age: timedelta = timedelta(hours=12)) -> None:
        self._lock = threading.Lock()
        self._news: dict[str, NewsData] = {}
        self._sentiment: dict[str, SentimentData] = {}
        self._regime: tuple[str, dict, datetime] | None = None
        self._max_age = max_age

    def _fresh(self, ts: datetime, now: datetime | None) -> bool:
        return (now or now_et()) - ts <= self._max_age

    def put_news(self, ticker: str, data: NewsData) -> None:
        with self._lock:
            self._news[ticker.upper()] = data

    def news(self, ticker: str, now: datetime | None = None) -> NewsData | None:
        with self._lock:
            d = self._news.get(ticker.upper())
        return d if d and self._fresh(d.graded_at, now) else None

    def put_sentiment(self, ticker: str, data: SentimentData) -> None:
        with self._lock:
            self._sentiment[ticker.upper()] = data

    def sentiment(self, ticker: str, now: datetime | None = None) -> SentimentData | None:
        with self._lock:
            d = self._sentiment.get(ticker.upper())
        return d if d and self._fresh(d.as_of, now) else None

    # market-wide regime, so a ticker without its own social reading still gets it
    def put_regime(self, regime: str, detail: dict, as_of: datetime) -> None:
        with self._lock:
            self._regime = (regime, detail, as_of)

    def regime(self, now: datetime | None = None) -> SentimentData | None:
        """Regime-only SentimentData (no buzz) if a fresh regime reading exists."""
        with self._lock:
            r = self._regime
        if r is None or (now or now_et()) - r[2] > timedelta(minutes=30):
            return None
        return SentimentData(market_regime=r[0], regime_detail=r[1], as_of=r[2])
