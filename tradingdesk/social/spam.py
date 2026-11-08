"""
Promotion / bot-pattern checks on recent posts (plain Python, no model).

A spike in coordinated promotion is a warning for the Skeptic, not a bullish
signal. Flags per post:
  duplicate   near-identical text to another recent post (copy-paste campaigns)
  flood       the same account posted 3+ of the recent posts
  pump        pump / promo language ("to the moon", "next 10x", "join my discord", rocket strings...)
  new_account account joined in the last 30 days (only when the platform reports join dates)
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from tradingdesk.social.base import SocialPost

_PUMP = re.compile(
    r"to the moon|\bmoon(ing)?\b|next \d+x|\b\d{2,}x\b|\b10\s?bagger|easy money|free money|load(ing)? up|"
    r"buy (now|before)|don'?t miss|last chance|squeeze (is )?(coming|incoming|imminent)|"
    r"(join|check) (my|our) (discord|telegram|chat|group|alerts?)|\bdm (me|for)\b|"
    r"guaranteed|can'?t lose|(\$\d+|\d{3,}%) (target|incoming|soon)|"
    r"(\U0001F680\s*){3,}|(\U0001F4B0\s*){3,}|(\U0001F48E\s*){3,}",
    re.I,
)
_CASHTAGS = re.compile(r"\$[A-Z]{1,6}\b")
_NON_WORD = re.compile(r"[^a-z0-9 ]+")


def _norm(text: str) -> str:
    t = _CASHTAGS.sub("", text.lower())
    t = re.sub(r"https?://\S+", "", t)
    return " ".join(_NON_WORD.sub(" ", t).split())


@dataclass
class SpamReport:
    flagged: dict[str, list[str]] = field(default_factory=dict)   # post id -> reasons
    share: float = 0.0
    warning: bool = False
    reasons: list[str] = field(default_factory=list)


def check(posts: list[SocialPost], now: datetime, warn_share: float = 0.3, min_posts: int = 5) -> SpamReport:
    rep = SpamReport()
    if not posts:
        return rep
    norms = [_norm(p.text) for p in posts]
    by_author = Counter(p.author for p in posts)
    for i, p in enumerate(posts):
        why: list[str] = []
        if norms[i] and len(norms[i]) >= 20 and any(
                j != i and norms[j] and SequenceMatcher(None, norms[i], norms[j]).ratio() >= 0.9
                for j in range(len(posts))):
            why.append("duplicate")
        if by_author[p.author] >= 3:
            why.append("flood")
        if _PUMP.search(p.text):
            why.append("pump")
        if p.author_joined is not None and now - p.author_joined < timedelta(days=30):
            why.append("new_account")
        if why:
            rep.flagged[p.id] = why
    rep.share = round(len(rep.flagged) / len(posts), 2)
    counts = Counter(r for rs in rep.flagged.values() for r in rs)
    dupes = counts.get("duplicate", 0)
    if (len(posts) >= min_posts and rep.share >= warn_share) or dupes >= 3:
        rep.warning = True
        rep.reasons = [f"{len(rep.flagged)} of {len(posts)} recent posts look like promotion"] + \
            [f"{n} {r.replace('_', ' ')}" for r, n in counts.most_common()]
    return rep
