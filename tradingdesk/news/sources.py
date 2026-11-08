"""
Pluggable news sources. Each returns normalized Headlines for one ticker and
fails on its own: an error in one source never stops the others (Newsie
catches it and marks that source unhealthy).

Adding a source = a class with `name` and `fetch()` plus a config.yaml entry
under news.sources, registered in SOURCE_TYPES.

  yahoo        WTBot's yfinance news (unofficial, best effort)
  google_news  WTBot's Google News RSS search
  edgar        SEC filings as headlines (see edgar.py)
  alpaca_news  Alpaca News API (off by default)
  newsfilter   Newsfilter.io official API (off until API access exists)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Protocol

from loguru import logger

from tradingdesk.models import Headline
from tradingdesk.sessions import ET


class NewsSource(Protocol):
    name: str

    def fetch(self, ticker: str, since: datetime) -> list[Headline]: ...


class SourceUnavailable(RuntimeError):
    """The source can't be used at all (missing key, no legitimate API access)."""


def _iso_to_et(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET)


def _from_wtbot(ticker: str, source: str, articles: list[dict], since: datetime) -> list[Headline]:
    """WTBot article dicts {title, description, publishedAt, source, url} -> Headlines."""
    out = []
    for a in articles:
        published = _iso_to_et(a.get("publishedAt", ""))
        title = (a.get("title") or "").strip()
        if not title or published is None or published < since:
            continue
        out.append(Headline(source=source, tickers=(ticker,), title=title[:300],
                            summary=(a.get("description") or "")[:300], url=a.get("url") or "",
                            published_at=published, outlet=(a.get("source") or "").strip()[:80]))
    return out


class YahooSource:
    name = "yahoo"

    def fetch(self, ticker: str, since: datetime) -> list[Headline]:
        from tradingdesk.legacy.wtbot import yahoo_news

        hours = max(1, int((datetime.now(ET) - since).total_seconds() // 3600) + 1)
        return _from_wtbot(ticker, self.name, yahoo_news.get_news_for_symbol(ticker, hours=hours), since)


class GoogleNewsSource:
    name = "google_news"

    def fetch(self, ticker: str, since: datetime) -> list[Headline]:
        from tradingdesk.legacy.wtbot import google_news

        hours = max(1, int((datetime.now(ET) - since).total_seconds() // 3600) + 1)
        return _from_wtbot(ticker, self.name, google_news.get_news_for_symbol(ticker, hours=hours), since)


class AlpacaNewsSource:
    """Alpaca News API. Off by default (it was unreliable in WTBot).

    WTBot asked with exclude_contentless=True, which drops headline-only items;
    most small-cap wire news is headline-only, which likely explains its empty
    results. This source keeps them.
    """

    name = "alpaca_news"

    def __init__(self, api_key: str, secret_key: str) -> None:
        if not api_key or not secret_key:
            raise SourceUnavailable("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
        from alpaca.data.historical.news import NewsClient

        self._client = NewsClient(api_key=api_key, secret_key=secret_key)

    def fetch(self, ticker: str, since: datetime) -> list[Headline]:
        from alpaca.data.requests import NewsRequest

        req = NewsRequest(symbols=ticker, start=since.astimezone(timezone.utc), limit=20, sort="desc",
                          exclude_contentless=False)
        items = self._client.get_news(req).data.get("news", [])
        out = []
        for it in items:
            if not it.headline or not it.created_at:
                continue
            out.append(Headline(source=self.name, tickers=(ticker,), title=it.headline.strip()[:300],
                                summary=(it.summary or "")[:300], url=it.url or "",
                                published_at=it.created_at.astimezone(ET), outlet=(it.source or "")[:80]))
        return out


class NewsfilterSource:
    """Placeholder until Newsfilter.io grants API access (ROADMAP). Official key-based API only;
    WTBot's old login-and-scrape approach is not coming back."""

    name = "newsfilter"

    def __init__(self, api_key: str) -> None:
        raise SourceUnavailable("Newsfilter.io API access isn't set up yet (see ROADMAP)")

    def fetch(self, ticker: str, since: datetime) -> list[Headline]:  # pragma: no cover
        return []


def build_sources(settings, edgar=None) -> tuple[list[NewsSource], dict[str, str]]:
    """Enabled sources, plus {name: why it's off} for anything enabled that couldn't start."""
    s = settings.secrets
    makers: dict[str, Callable[[], NewsSource]] = {
        "yahoo": YahooSource,
        "google_news": GoogleNewsSource,
        "alpaca_news": lambda: AlpacaNewsSource(s.alpaca_api_key, s.alpaca_secret_key),
        "newsfilter": lambda: NewsfilterSource(s.newsfilter_api_key),
    }
    if edgar is not None:
        makers["edgar"] = lambda: edgar
    sources: list[NewsSource] = []
    off: dict[str, str] = {}
    for name in settings.config.news.sources:
        if not settings.source_enabled("news", name):
            continue
        maker = makers.get(name)
        if maker is None:
            off[name] = "enabled in config but not available (EDGAR needs SEC_USER_AGENT)" if name == "edgar" \
                else "unknown source"
            continue
        try:
            sources.append(maker())
        except SourceUnavailable as exc:
            off[name] = str(exc)
        except Exception as exc:
            off[name] = f"failed to start: {exc}"
    for name, why in off.items():
        logger.warning("news source {} is off: {}", name, why)
    return sources, off
