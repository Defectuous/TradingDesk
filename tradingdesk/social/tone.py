"""
Local tone scoring (qwen on defectserver, free): bullish / bearish / neutral per
post, remembered per post id so each post is scored once. Fails quietly: if
the local model is down, tone is None and nothing else changes.
"""
from __future__ import annotations

from datetime import datetime

from loguru import logger

from tradingdesk.llm.costs import LLMUsage
from tradingdesk.news.local_llm import LocalLLMError, OllamaClient
from tradingdesk.social.base import SocialPost

_SYSTEM = """\
You read short social-media posts about one stock ticker and label each post's tone toward THAT stock:
bullish (expects or wants the price to go up), bearish (expects it to go down, warns, mocks longs),
or neutral (questions, news links with no opinion, off-topic). Sarcasm counts by its real meaning.\
"""
_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"n": {"type": "integer"},
                       "tone": {"type": "string", "enum": ["bullish", "bearish", "neutral"]}},
        "required": ["n", "tone"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False,
}


class ToneScorer:
    def __init__(self, client: OllamaClient, thinking: bool, log_usage, max_posts: int = 20) -> None:
        self.client = client
        self.thinking = thinking
        self.log_usage = log_usage
        self.max_posts = max_posts
        self._seen: dict[str, str] = {}

    def score(self, ticker: str, posts: list[SocialPost], now: datetime) -> float | None:
        """(bullish - bearish) / (bullish + bearish) over the recent posts; None if nothing opinionated."""
        recent = posts[: self.max_posts]
        todo = [p for p in recent if p.id not in self._seen]
        if todo:
            user = f"Ticker: {ticker}\n\n" + "\n".join(f"[{i}] {p.text[:300]}" for i, p in enumerate(todo, 1))
            try:
                r = self.client.chat_json(_SYSTEM, user, _SCHEMA, think=self.thinking, max_tokens=1024)
            except LocalLLMError as exc:
                logger.debug("tone: local model unavailable for {}: {}", ticker, exc)
                return None
            self.log_usage(LLMUsage(agent="sentiment_tone", model=r.model, input_tokens=r.input_tokens,
                                    output_tokens=r.output_tokens, local=True, ticker=ticker, at=now))
            got = {int(x["n"]): x["tone"] for x in r.data.get("items", []) if isinstance(x, dict)}
            for i, p in enumerate(todo, 1):
                self._seen[p.id] = got.get(i, "neutral")
        tones = [self._seen.get(p.id, "neutral") for p in recent]
        bull, bear = tones.count("bullish"), tones.count("bearish")
        return round((bull - bear) / (bull + bear), 2) if bull + bear else None
