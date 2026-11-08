"""
StockTwits source, through StockTwits' official MCP server (stocktwits_mcp.py).

One call per ticker per poll: get_symbol_pulse returns the canonical sentiment
score, message volume (now and per timeframe), watchers and the ten newest
posts. Reading through the approved API with your own account is what
StockTwits' Terms (section 5) allow; we never scrape and never post.

Politeness: if StockTwits rate-limits us (429) the source rests for the
retry-after time (default 5 minutes); after three 429s in a row it rests for
an hour. A 401 means the login expired: run `python -m tradingdesk stocktwits-login`.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from tradingdesk.sessions import ET
from tradingdesk.social.base import SocialPost, SocialReading, SourcePaused
from tradingdesk.social.stocktwits_mcp import StockTwitsError, StockTwitsMCP, TokenStore


def _ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(ET)
    except ValueError:
        return None


def parse_pulse(ticker: str, d: dict[str, Any], now: datetime) -> SocialReading:
    posts = []
    for p in d.get("posts") or []:
        created = _ts(p.get("created_at"))
        if created is None or not p.get("body"):
            continue
        tag = (p.get("sentiment") or "").lower()
        user = p.get("user") or {}
        posts.append(SocialPost(
            platform="stocktwits", ticker=ticker, id=str(p.get("id")), text=p["body"][:600],
            author=user.get("username") or str(user.get("id") or "?"), created_at=created,
            author_followers=user.get("followers"), author_joined=_ts(user.get("join_date")),
            likes=int(p.get("likes") or 0), replies=int(p.get("replies") or 0),
            tag=tag if tag in ("bullish", "bearish") else None,
        ))
    vol = {v.get("timeframe"): v for v in d.get("volume") or [] if isinstance(v, dict)}
    mv = d.get("message_volume") or {}
    sent = d.get("sentiment") or {}

    def norm(tf: str) -> float | None:
        v = vol.get(tf, {}).get("normalized_value")
        return float(v) if isinstance(v, (int, float)) else None

    now_level = mv.get("score") if isinstance(mv.get("score"), (int, float)) else norm("now")
    w = d.get("watchers")
    watchers = w.get("count") if isinstance(w, dict) else w          # {"count": N} on the MCP server
    return SocialReading(
        platform="stocktwits", ticker=ticker, as_of=now, posts=posts,
        volume_now=float(now_level) if now_level is not None else None, volume_day=norm("1D"),
        crowd_score=float(sent["score"]) if isinstance(sent.get("score"), (int, float)) else None,
        crowd_label=sent.get("label"), watchers=int(watchers) if isinstance(watchers, (int, float)) else None,
        extra={"bull_pct": sent.get("bull_pct"), "volume_label": mv.get("label") or vol.get("now", {}).get("label"),
               "price_change_pct": d.get("change")},
    )


class StockTwitsSource:
    name = "stocktwits"

    def __init__(self, token_file: Path, client: StockTwitsMCP | None = None) -> None:
        self.client = client or StockTwitsMCP(TokenStore(token_file))
        self._lock = threading.Lock()
        self._paused_until: datetime | None = None
        self._strikes = 0

    def read(self, ticker: str, now: datetime) -> SocialReading:
        with self._lock:
            if self._paused_until and now < self._paused_until:
                raise SourcePaused(f"resting after rate limits until {self._paused_until:%H:%M}", self._paused_until)
        try:
            res = self.client.call("get_symbol_pulse", {"symbol": ticker})
        except StockTwitsError as exc:
            if "429" in str(exc):
                with self._lock:
                    self._strikes += 1
                    rest = timedelta(hours=1) if self._strikes >= 3 else timedelta(seconds=_retry_after(str(exc)))
                    self._paused_until = now + rest
                raise SourcePaused(f"rate limited by StockTwits; resting {int(rest.total_seconds() // 60)} min",
                                   self._paused_until) from exc
            raise
        with self._lock:
            self._strikes = 0
        data = res.get("structuredContent")
        if not isinstance(data, dict):
            raise StockTwitsError("get_symbol_pulse returned no structured data")
        return parse_pulse(ticker, data, now)


def _retry_after(msg: str) -> float:
    try:
        return max(60.0, float(msg.rsplit("retry-after", 1)[1].strip()))
    except (IndexError, ValueError):
        return 300.0
