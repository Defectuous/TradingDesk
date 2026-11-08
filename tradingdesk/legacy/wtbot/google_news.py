"""
Google News RSS source.

Queries news.google.com/rss/search — no API key required. Merged into
news_client alongside Yahoo/Alpaca (not a fallback): it aggregates across
far more outlets than either, which matters for catalysts that break on a
wire service or small outlet before Yahoo/Alpaca have indexed them (e.g.
CRE's "record 550 customers" headline, 2026-08-26 — Yahoo+Alpaca combined
had only a generic multi-stock roundup by scan time, so a stop-on-first-hit
fallback would never have reached this source).

Returns articles in the standard shape: {title, description, publishedAt, source, url}
"""
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

import requests
from loguru import logger

from tradingdesk.legacy.wtbot import config

_RSS_URL = "https://news.google.com/rss/search"

# Brief in-process cache — avoids hammering Google for the same symbol
# within a single scan session. TTL matches a typical scan interval.
_cache: dict[str, tuple[list[dict], float]] = {}
_CACHE_TTL = 300  # 5 minutes

_REQUEST_TIMEOUT_SEC = 10


def get_news_for_symbol(
    symbol: str,
    hours: int = config.NEWS_LOOKBACK_HOURS,
) -> list[dict]:
    """
    Return recent news articles for `symbol` from Google News RSS.

    Filters to articles published within the last `hours` — Google News RSS
    has no date-range query param, so staleness is enforced client-side the
    same way yahoo_news.py does.
    Returns [] on any error so a news failure cannot crash a scan.
    """
    now = time.monotonic()
    if symbol in _cache:
        cached, fetched_at = _cache[symbol]
        if now - fetched_at < _CACHE_TTL:
            logger.debug(f"{symbol}: Google News from cache ({len(cached)} articles)")
            return _filter_by_hours(cached, hours)

    # Quoted ticker + "stock" cuts down on false hits for short/common-word
    # tickers (e.g. bare "SMR" or "RUM" pull in a lot of unrelated results).
    query = f'"{symbol}" stock'
    params = {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}

    try:
        resp = requests.get(_RSS_URL, params=params, timeout=_REQUEST_TIMEOUT_SEC)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as exc:
        logger.warning(f"{symbol}: Google News fetch error — {exc}")
        return []

    articles = []
    for item in root.findall("./channel/item"):
        parsed = _normalise(item)
        if parsed:
            articles.append(parsed)

    _cache[symbol] = (articles, now)
    logger.debug(f"{symbol}: Google News returned {len(articles)} articles")
    return _filter_by_hours(articles, hours)


def _normalise(item: ET.Element) -> Optional[dict]:
    """
    Convert a raw Google News RSS <item> to the standard article shape.

    Google renders <title> as "Headline - Source Name" and duplicates the
    source in a <source> element — prefer the <source> tag and strip the
    " - Source Name" suffix from the title when it matches.
    """
    title = (item.findtext("title") or "").strip()
    if not title:
        return None

    source_el = item.find("source")
    source = (source_el.text or "Google News").strip() if source_el is not None else "Google News"

    if source != "Google News" and title.endswith(f" - {source}"):
        title = title[: -(len(source) + 3)].strip()

    description = (item.findtext("description") or "").strip()
    # Google's <description> is an HTML blob (often just re-wraps the title
    # in an <a> tag with no extra text) — strip tags, and don't let that
    # noise stand in for a real summary.
    description = _strip_html(description)
    if description == title:
        description = ""

    pub_raw = item.findtext("pubDate") or ""
    published_at = _parse_date(pub_raw)

    link = (item.findtext("link") or "").strip()

    return {
        "title":       title,
        "description": description[:300],
        "publishedAt": published_at,
        "source":      source,
        "url":         link,
    }


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return _HTML_TAG_RE.sub("", text).strip()


def _filter_by_hours(articles: list[dict], hours: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    result = []
    for a in articles:
        try:
            dt = datetime.fromisoformat(a["publishedAt"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                result.append(a)
        except Exception:
            result.append(a)
    return result


def _parse_date(raw: str) -> str:
    if not raw:
        return datetime.now(timezone.utc).isoformat()
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()
