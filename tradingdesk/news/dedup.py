"""
Cross-source dedup, before any LLM sees a headline.

Two headlines are the same story when their URLs match or their normalized
titles are at least `similarity` alike. The merged headline keeps the earliest
timestamp and lists the other sources in `also_seen_in`.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from urllib.parse import urlsplit

from tradingdesk.models import Headline

_NON_WORD = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")


def norm_title(title: str) -> str:
    t = title.lower()
    t = re.sub(r"\s+-\s+[^-]{2,40}$", "", t)          # " - Publisher" suffix some feeds append
    return _SPACES.sub(" ", _NON_WORD.sub(" ", t)).strip()


def norm_url(url: str) -> str:
    if not url:
        return ""
    u = urlsplit(url)
    return (u.netloc.lower().removeprefix("www.") + u.path.rstrip("/")).lower()


def story_key(h: Headline) -> str:
    """Stable identity for 'have we already graded/filtered this story'."""
    return norm_title(h.title)


def dedupe(headlines: list[Headline], similarity: float = 0.90) -> list[Headline]:
    groups: list[list[Headline]] = []
    keys: list[tuple[str, str]] = []
    for h in sorted(headlines, key=lambda x: x.published_at):
        t, u = norm_title(h.title), norm_url(h.url)
        for i, (gt, gu) in enumerate(keys):
            if (u and u == gu) or t == gt or (t and gt and SequenceMatcher(None, t, gt).ratio() >= similarity):
                groups[i].append(h)
                break
        else:
            groups.append([h])
            keys.append((t, u))
    out = []
    for g in groups:
        first = g[0]                                   # earliest
        others = tuple(dict.fromkeys(x.source for x in g[1:] if x.source != first.source))
        summary = first.summary or next((x.summary for x in g if x.summary), "")
        out.append(first.model_copy(update={"also_seen_in": others, "summary": summary}))
    out.sort(key=lambda x: x.published_at, reverse=True)
    return out
