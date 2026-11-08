"""
Pluggable social sources, same pattern as news sources: each returns a
normalized reading for one ticker and fails on its own.

Adding a platform = a class with `name` and `read()` plus a config.yaml entry
under sentiment.sources.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol


@dataclass(frozen=True)
class SocialPost:
    platform: str
    ticker: str
    id: str
    text: str
    author: str
    created_at: datetime
    author_followers: int | None = None        # None when the platform doesn't say (StockTwits MCP doesn't)
    author_joined: datetime | None = None
    likes: int = 0
    replies: int = 0
    tag: Literal["bullish", "bearish"] | None = None   # the author's own tag, when the platform has one


@dataclass(frozen=True)
class SocialReading:
    """One platform's view of one ticker right now."""

    platform: str
    ticker: str
    as_of: datetime
    posts: list[SocialPost] = field(default_factory=list)
    volume_now: float | None = None             # 0-100 normalized chatter level, if the platform provides it
    volume_day: float | None = None             # the same scale for the day so far (trend baseline)
    crowd_score: float | None = None            # platform's bull/bear score, 0-100
    crowd_label: str | None = None
    watchers: int | None = None
    extra: dict = field(default_factory=dict)


class SocialSource(Protocol):
    name: str

    def read(self, ticker: str, now: datetime) -> SocialReading: ...


class SourcePaused(RuntimeError):
    """The platform asked us to slow down; the source is resting until `until`."""

    def __init__(self, msg: str, until: datetime) -> None:
        super().__init__(msg)
        self.until = until
