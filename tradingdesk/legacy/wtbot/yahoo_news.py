"""
Yahoo Finance news and float-share source.

Uses the yfinance library to fetch recent news articles and float share
counts for a symbol. News articles use the standard shape:
{title, description, publishedAt, source, url}
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger

from tradingdesk.legacy.wtbot import config

# yfinance logs every failed request straight to stderr via the stdlib
# logging module ("HTTP Error 404: ..."), bypassing loguru. Warrants,
# rights, units, and preferred shares (symbols with a class suffix like
# ".WS"/".PRC") always 404 on Yahoo's quoteSummary endpoint, so silence
# yfinance's own logger and let get_float()'s debug log cover it instead.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

# Symbol class suffixes (warrants, rights, units, preferred shares) that
# Yahoo Finance never has quoteSummary data for under Alpaca's "SYM.XX"
# naming — skip the network round-trip entirely instead of burning a
# throttled request on a guaranteed 404.
_NON_COMMON_SUFFIXES = (".WS", ".WT", ".RT", ".U", ".PR", ".PRA", ".PRB", ".PRC", ".PRD")

# Brief in-process cache — avoids hammering Yahoo for the same symbol
# within a single scan session.  TTL matches a typical scan interval.
_cache: dict[str, tuple[list[dict], float]] = {}
_CACHE_TTL = 300  # 5 minutes

# Symbol → (float_shares, fetched_at_epoch)
_float_cache: dict[str, tuple[Optional[int], float]] = {}
_FLOAT_CACHE_TTL = 86_400  # 24 hours — float data doesn't change intraday

# Throttle real (cache-miss) float lookups so a scan with many new symbols
# doesn't fire requests at Yahoo back-to-back and trip its rate limiting.
_FLOAT_MIN_INTERVAL_SEC = config.FLOAT_LOOKUP_MIN_INTERVAL_SEC
_last_float_fetch_at: float = 0.0


def get_float(symbol: str) -> Optional[int]:
    """
    Return float share count for a symbol via Yahoo Finance, or None on failure.

    Results are cached for 24 hours since float data is static.
    Returns None (never raises) so a float lookup failure cannot crash a scan.
    """
    global _last_float_fetch_at

    if symbol.endswith(_NON_COMMON_SUFFIXES):
        logger.debug(f"{symbol}: warrant/rights/unit/preferred class — float unavailable")
        return None

    now = time.monotonic()
    if symbol in _float_cache:
        cached_value, fetched_at = _float_cache[symbol]
        if now - fetched_at < _FLOAT_CACHE_TTL:
            logger.debug(f"{symbol}: float from cache → {cached_value}")
            return cached_value

    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance not installed — run: pip install yfinance")
        return None

    elapsed = time.monotonic() - _last_float_fetch_at
    if elapsed < _FLOAT_MIN_INTERVAL_SEC:
        time.sleep(_FLOAT_MIN_INTERVAL_SEC - elapsed)
    _last_float_fetch_at = time.monotonic()

    try:
        info = yf.Ticker(symbol).get_info()
        float_shares = info.get("floatShares")
    except Exception as exc:
        logger.warning(f"{symbol}: Yahoo Finance float fetch error — {exc}")
        _float_cache[symbol] = (None, now)
        return None

    float_shares = int(float_shares) if float_shares else None
    logger.debug(f"{symbol}: Yahoo float → {float_shares}")
    _float_cache[symbol] = (float_shares, now)
    return float_shares


def get_news_for_symbol(
    symbol: str,
    hours: int = config.NEWS_LOOKBACK_HOURS,
) -> list[dict]:
    """
    Return recent news articles for `symbol` from Yahoo Finance.

    Filters to articles published within the last `hours`.
    Returns [] on any error so a news failure cannot crash a scan.
    """
    now = time.monotonic()
    if symbol in _cache:
        cached, fetched_at = _cache[symbol]
        if now - fetched_at < _CACHE_TTL:
            logger.debug(f"{symbol}: Yahoo news from cache ({len(cached)} articles)")
            return _filter_by_hours(cached, hours)

    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance not installed — run: pip install yfinance")
        return []

    try:
        ticker   = yf.Ticker(symbol)
        raw_news = ticker.news or []
    except Exception as exc:
        logger.warning(f"{symbol}: Yahoo Finance fetch error — {exc}")
        return []

    articles = []
    for item in raw_news:
        parsed = _normalise(item)
        if parsed:
            articles.append(parsed)

    _cache[symbol] = (articles, now)
    logger.debug(f"{symbol}: Yahoo Finance returned {len(articles)} articles")
    return _filter_by_hours(articles, hours)


def _normalise(item: dict) -> Optional[dict]:
    """
    Convert a raw yfinance news item to the standard article shape.

    yfinance wraps article data inside a 'content' key:
        item['content']['title']
        item['content']['summary']        ← description
        item['content']['pubDate']        ← ISO timestamp
        item['content']['provider']['displayName']
        item['content']['canonicalUrl']['url']
    """
    content = item.get("content") or item  # older yfinance versions are flat

    title = (content.get("title") or "").strip()
    if not title:
        return None

    description = (content.get("summary") or content.get("description") or "").strip()

    pub_raw = content.get("pubDate") or content.get("providerPublishTime") or ""
    published_at = _parse_date(pub_raw)

    provider = content.get("provider") or {}
    source = (
        provider.get("displayName")
        or content.get("source")
        or "Yahoo Finance"
    ).strip()

    canonical = content.get("canonicalUrl") or {}
    url = canonical.get("url") or content.get("link") or ""

    return {
        "title":       title,
        "description": description[:300],
        "publishedAt": published_at,
        "source":      source,
        "url":         url,
    }


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


def _parse_date(raw) -> str:
    if not raw:
        return datetime.now(timezone.utc).isoformat()

    # Unix timestamp (older yfinance versions)
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, tz=timezone.utc).isoformat()

    # ISO string
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(str(raw)[:26], fmt)
            return dt.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass

    return datetime.now(timezone.utc).isoformat()
