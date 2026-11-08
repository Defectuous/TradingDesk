"""
Catalyst grading: Claude first, the local model when the spend cap is hit or
Claude fails, keyword rules as the last resort. Plus the local relevance filter
that decides which headlines are worth sending to Claude.

  ClaudeGrader    A-F rubric (prompts/catalyst_rubric.md) + WTBot's type guide,
                  structured JSON output, system prompt cached, effort low
  LocalGrader     WTBot's best local setup (LLM_tweaks.md "hints2"): its exact
                  system prompt + addendum and article format, qwen3:14b thinking,
                  hard keyword flags override to negative; WTBot labels -> A-F
  RulesGrader     WTBot's keyword fallback (no model)
  RelevanceFilter qwen yes/no per headline: about this ticker and material?

Retries are transport-only (SDK max_retries); a parsed reply is never re-requested.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from loguru import logger

from tradingdesk.llm.costs import LLMUsage, with_cost
from tradingdesk.models import FilingFlags, Headline
from tradingdesk.news import flags as kw
from tradingdesk.news.local_llm import LocalLLMError, OllamaClient
from tradingdesk.sessions import ET

GRADES = ("A", "B", "C", "D", "F")
TYPES = ("FDA", "Contract", "Buyout", "Earnings", "Partnership", "Trial data", "Upgrade", "Product", "Patent",
         "Offering", "Reverse split", "Delisting", "Other", "None")


class GradeError(RuntimeError):
    pass


@dataclass
class GradeResult:
    grade: str
    catalyst_type: str
    reason: str
    graded_by: Literal["claude", "local", "rules"]
    key_index: int | None = None                       # 0-based index of the headline that drove the grade
    headline_grades: dict[int, str] = field(default_factory=dict)


@dataclass
class GradeInput:
    ticker: str
    now: datetime
    session_start: datetime                            # headlines before this are "older than the session"
    headlines: list[Headline]                          # freshest first, already deduped and filtered
    filing_flags: FilingFlags
    filing_lines: list[str]
    keyword_flags: dict[str, dict]


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

_CLAUDE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "grade": {"type": "string", "enum": list(GRADES)},
        "catalyst_type": {"type": "string", "enum": list(TYPES)},
        "reason": {"type": "string"},
        "key_headline": {"type": "integer"},
        "headline_grades": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"n": {"type": "integer"}, "grade": {"type": "string", "enum": list(GRADES)}},
                "required": ["n", "grade"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["grade", "catalyst_type", "reason", "key_headline", "headline_grades"],
    "additionalProperties": False,
}

_OUTPUT_RULES = """
=== YOUR ANSWER ===

The guide above describes catalyst types and a confidence scale from an older version of this task. Use it to
judge the news, but answer with the A-F grade and the type label defined in the rubric, in this JSON shape:
- grade: A-F per the rubric
- catalyst_type: one label from the rubric's list
- reason: ONE sentence naming the specific catalyst (or why there is none) for this ticker
- key_headline: the number [n] of the headline that drove the grade, or 0 if none did
- headline_grades: a grade for every numbered headline
SEC filing lines are verified facts from EDGAR about this company.
"""


def load_claude_system(types_file: Path, rubric_file: Path) -> str:
    """The cached system prompt: WTBot's type guide + the A-F rubric + output rules. Static per run."""
    def strip_comment(t: str) -> str:
        return t.split("-->", 1)[1].strip() if t.lstrip().startswith("<!--") else t.strip()
    return "\n\n".join([strip_comment(types_file.read_text(encoding="utf-8")),
                        strip_comment(rubric_file.read_text(encoding="utf-8")), _OUTPUT_RULES.strip()])


def claude_user_message(g: GradeInput) -> str:
    lines = [f"Stock ticker: {g.ticker}",
             f"Now: {g.now.astimezone(ET):%a %Y-%m-%d %H:%M} ET. The current session's news starts "
             f"{g.session_start.astimezone(ET):%a %H:%M} ET; anything earlier is older than the current session.",
             "", "Headlines (newest first):", ""]
    for i, h in enumerate(g.headlines, 1):
        src = ", ".join((h.outlet or h.source, *h.also_seen_in))
        lines.append(f"[{i}] {h.published_at.astimezone(ET):%a %m/%d %H:%M} ET | {src}")
        lines.append(f"    Title: {h.title}")
        if h.summary and h.summary.strip() != h.title.strip():
            lines.append(f"    Summary: {h.summary}")
        lines.append("")
    if not g.headlines:
        lines += ["(no headlines found for this ticker)", ""]
    if g.filing_lines:
        lines.append("SEC filings (EDGAR, times ET):")
        lines += [f"- {x}" for x in g.filing_lines[:8]]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


class ClaudeGrader:
    def __init__(self, api_key: str, model: str, max_tokens: int, timeout_sec: float, max_retries: int,
                 system_prompt: str, pricing: dict, log_usage) -> None:
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key, timeout=timeout_sec, max_retries=max_retries)
        self.model = model
        self.max_tokens = max_tokens
        self.system = system_prompt
        self.pricing = pricing
        self.log_usage = log_usage

    def grade(self, g: GradeInput) -> GradeResult:
        a = self._anthropic
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": claude_user_message(g)}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": _CLAUDE_SCHEMA}},
            )
        except a.RateLimitError as exc:
            raise GradeError(f"rate limited: {exc}") from exc
        except a.APIStatusError as exc:
            raise GradeError(f"API error {exc.status_code}: {exc.message}") from exc
        except a.APIConnectionError as exc:
            raise GradeError(f"connection error: {exc}") from exc
        u = resp.usage
        self.log_usage(with_cost(LLMUsage(
            agent="newsie", model=self.model, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
            cache_read_tokens=u.cache_read_input_tokens or 0, cache_write_tokens=u.cache_creation_input_tokens or 0,
            ticker=g.ticker, at=g.now), self.pricing))
        if resp.stop_reason != "end_turn":
            raise GradeError(f"no answer (stop_reason={resp.stop_reason})")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            d = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GradeError(f"invalid JSON: {text[:120]!r}") from exc
        n = len(g.headlines)
        key = d.get("key_headline") or 0
        return GradeResult(
            grade=d["grade"], catalyst_type=d["catalyst_type"], reason=" ".join(str(d["reason"]).split())[:300],
            graded_by="claude", key_index=key - 1 if 1 <= key <= n else None,
            headline_grades={x["n"] - 1: x["grade"] for x in d.get("headline_grades", []) if 1 <= x["n"] <= n},
        )


# ---------------------------------------------------------------------------
# Local (WTBot hints2)
# ---------------------------------------------------------------------------

WTBOT_TYPES = ("earnings_beat", "fda_approval", "merger_acquisition", "partnership", "contract", "upgrade",
               "short_squeeze", "product_launch", "none", "negative", "unclear")
_LOCAL_SCHEMA = {
    "type": "object",
    "properties": {
        "has_catalyst": {"type": "boolean"},
        "catalyst_type": {"type": "string", "enum": list(WTBOT_TYPES)},
        "sentiment": {"type": "string", "enum": ["positive", "negative", "neutral"]},
        "confidence": {"type": "number"},
        "summary": {"type": "string"},
    },
    "required": ["has_catalyst", "catalyst_type", "sentiment", "confidence", "summary"],
    "additionalProperties": False,
}
_WTBOT_TO_LABEL = {"earnings_beat": "Earnings", "fda_approval": "FDA", "merger_acquisition": "Buyout",
                   "partnership": "Partnership", "contract": "Contract", "upgrade": "Upgrade",
                   "product_launch": "Product", "short_squeeze": "Other", "unclear": "Other", "none": "None"}
_FLAG_TO_LABEL = {"offering": "Offering", "private_placement": "Offering", "share_issuance": "Offering",
                  "warrants": "Offering", "shelf": "Offering", "convertible": "Offering", "financing": "Offering",
                  "reverse_split": "Reverse split", "delisting": "Delisting", "trial_fail": "Trial data"}
LOCAL_FULL_CONFIDENCE = 0.70      # WTBot LLM_tweaks.md: local full-size cutoff, confirmed on 1,696 sets


def wtbot_user_hints(ticker: str, articles: list[dict]) -> str:
    """WTBot llm_tweaks/prompts.py user_hints(), byte for byte (format changes hurt qwen: LLM_tweaks.md)."""
    msg = f"Stock ticker: {ticker}\n\nRecent news:\n\n"
    for i, a in enumerate(articles[:5], 1):
        msg += f"[{i}] Title: {a.get('title', '')}\n     Description: {a.get('description', 'N/A')}\n\n"
    h = {f: v for f, v in kw.news_flags(ticker, articles).items() if v["severity"] in ("hard", "soft")}
    if h:
        msg += ("Automated keyword scan of these articles (verify against the text; "
                "ignore if it refers to a different company):\n")
        for f, v in h.items():
            msg += f"- {f} ({v['severity']}): \"{v['match']}\"\n"
    return msg


def wtbot_label_to_grade(ctype: str, conf: float, flags: dict[str, dict]) -> tuple[str, str]:
    """WTBot's (catalyst_type, confidence) -> (A-F grade, type label)."""
    if ctype == "negative":
        label = next((_FLAG_TO_LABEL[f] for f in flags if f in _FLAG_TO_LABEL), "Other")
        return "F", label
    if ctype == "none":
        return "F", "None"
    label = _WTBOT_TO_LABEL.get(ctype, "Other")
    if ctype in ("unclear", "short_squeeze"):
        return ("C" if conf >= 0.40 else "D"), label
    if conf >= 0.85 and ctype in ("fda_approval", "merger_acquisition", "earnings_beat"):
        return "A", label
    if conf >= LOCAL_FULL_CONFIDENCE:
        return "B", label
    return ("C" if conf >= 0.50 else "D"), label


class LocalGrader:
    def __init__(self, client: OllamaClient, system_prompt: str, thinking: bool, log_usage) -> None:
        self.client = client
        self.system = system_prompt
        self.thinking = thinking
        self.log_usage = log_usage

    def grade(self, g: GradeInput) -> GradeResult:
        articles = kw.as_articles(g.headlines)
        try:
            r = self.client.chat_json(self.system, wtbot_user_hints(g.ticker, articles), _LOCAL_SCHEMA,
                                      think=self.thinking)
        except LocalLLMError as exc:
            raise GradeError(str(exc)) from exc
        self.log_usage(LLMUsage(agent="newsie_local", model=r.model, input_tokens=r.input_tokens,
                                output_tokens=r.output_tokens, local=True, ticker=g.ticker, at=g.now))
        ctype = r.data.get("catalyst_type") if r.data.get("catalyst_type") in WTBOT_TYPES else "unclear"
        conf = max(0.0, min(1.0, float(r.data.get("confidence") or 0.0)))
        hard = {f: v for f, v in g.keyword_flags.items() if v["severity"] == "hard"}
        if hard:                                       # WTBot's hard keyword override
            ctype = "negative"
        grade, label = wtbot_label_to_grade(ctype, conf, hard or g.keyword_flags)
        reason = " ".join(str(r.data.get("summary") or "").split())[:280] or f"local model: {ctype}"
        if hard:
            reason = f"{reason} (keyword flags: {', '.join(hard)})"[:300]
        return GradeResult(grade, label, reason, "local", key_index=0 if g.headlines else None)


# ---------------------------------------------------------------------------
# Rules (no model)
# ---------------------------------------------------------------------------

class RulesGrader:
    """WTBot fallback_classify keywords, plus the hard negative keyword flags."""

    _KEYWORDS = (("earnings", "Earnings"), ("fda", "FDA"), ("approved", "FDA"), ("acquisition", "Buyout"),
                 ("merger", "Buyout"), ("partnership", "Partnership"), ("contract", "Contract"))

    def grade(self, g: GradeInput) -> GradeResult:
        hard = [f for f, v in g.keyword_flags.items() if v["severity"] == "hard"]
        if hard:
            label = next((_FLAG_TO_LABEL[f] for f in hard if f in _FLAG_TO_LABEL), "Other")
            return GradeResult("F", label, f"Negative keywords in headlines: {', '.join(hard)} (no model available).",
                               "rules")
        titles = " ".join(h.title for h in g.headlines).lower()
        for word, label in self._KEYWORDS:
            if word in titles:
                return GradeResult("C", label, f"Keyword '{word}' in headlines; not graded by a model.", "rules",
                                   key_index=0)
        if not g.headlines:
            return GradeResult("F", "None", "No news found for this ticker.", "rules")
        return GradeResult("D", "Other", "Headlines found but no catalyst keywords; not graded by a model.", "rules")


# ---------------------------------------------------------------------------
# Local relevance filter
# ---------------------------------------------------------------------------

_FILTER_SYSTEM = """\
You screen news headlines for a small-cap momentum day trader before a more expensive model grades them.
For each numbered headline about the given stock ticker, answer keep=true or keep=false.

keep=true when the headline is specifically about THIS company (it names the ticker or the company, or is
clearly about it) AND reports a company-specific event or development that could move the stock, good or bad:
results, guidance, FDA/regulatory news, trials, contracts, deals, partnerships, products, analyst actions,
offerings, financings, warrants, reverse splits, delisting or compliance notices, lawsuits, insider trades,
SEC filings, executive changes.

keep=false for: articles mainly about a different company; market or sector roundups that give no
company-specific reason for this ticker; price-move-only reports ("shares jump 40%") that give no reason;
quote, chart, option-chain or forecast pages; generic market or macro commentary.

When unsure, keep=true. Never drop negative news about this company.\
"""
_FILTER_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"n": {"type": "integer"}, "keep": {"type": "boolean"}},
        "required": ["n", "keep"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False,
}


class RelevanceFilter:
    def __init__(self, client: OllamaClient, thinking: bool, log_usage) -> None:
        self.client = client
        self.thinking = thinking
        self.log_usage = log_usage

    def keep(self, ticker: str, headlines: list[Headline], now: datetime) -> list[bool]:
        """keep flags, same order. Fails open (keeps everything) if the local model is down."""
        if not headlines:
            return []
        user = f"Stock ticker: {ticker}\n\n" + "\n".join(
            f"[{i}] {h.title}" + (f" | {h.summary[:200]}" if h.summary and h.summary != h.title else "")
            for i, h in enumerate(headlines, 1))
        try:
            r = self.client.chat_json(_FILTER_SYSTEM, user, _FILTER_SCHEMA, think=self.thinking, max_tokens=1024)
        except LocalLLMError as exc:
            logger.warning("newsie: relevance filter unavailable for {} ({}); keeping all", ticker, exc)
            return [True] * len(headlines)
        self.log_usage(LLMUsage(agent="newsie_filter", model=r.model, input_tokens=r.input_tokens,
                                output_tokens=r.output_tokens, local=True, ticker=ticker, at=now))
        verdict = {int(x["n"]): bool(x["keep"]) for x in r.data.get("items", []) if isinstance(x, dict)}
        return [verdict.get(i, True) for i in range(1, len(headlines) + 1)]   # missing answer -> keep
