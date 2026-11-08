"""
Deterministic headline checks, copied from WTBot (no logic changes):

  roundup / ticker-named      screener/ai_catalyst.py  _is_roundup_article, _ticker_named
  keyword flags               screener/news_flags.py   news_flags (hard / soft / context)
  junk stubs                  llm_tweaks/prompts.py    _JUNK_RE (quote, option-chain, bare filing pages)

They work on WTBot's article dicts {title, description}; `as_articles()` converts Headlines.
"""
from __future__ import annotations

import re

from tradingdesk.models import Headline


def as_articles(headlines: list[Headline]) -> list[dict]:
    return [{"title": h.title, "description": h.summary} for h in headlines]


# Quote / option-chain / bare filing pages: Claude says none on 88/92 such sets (WTBot LLM_tweaks.md).
_JUNK_RE = re.compile(r"\b(call|put) \(|Interactive Stock Chart|Stock Price, News, Quote|Stock Forecasts|"
                      r"SEC Filing \(|Form 8\.3", re.I)


def is_junk(title: str) -> bool:
    return bool(_JUNK_RE.search(title))


_ROUNDUP_TITLE_RE = re.compile(
    r"\d+\s+\S.*\bStocks\b|\bOther\b.*\bStocks Moving\b|\bStocks (?:Moving|to Watch)\b",
    re.IGNORECASE,
)
_TICKER_CITATION_RE = re.compile(r"\((?:NASDAQ|NYSE|NYSEAMERICAN|AMEX):[A-Z.]{1,6}\)")


def _is_roundup_article(article: dict) -> bool:
    """
    Multi-stock aggregator listicle ("12 X Stocks Moving In ... Session",
    "... Joins Y, Z And Other Big Stocks Moving Higher") whose description
    is frequently truncated mid-sentence before it reaches this specific
    ticker's line.
    """
    text = f"{article.get('title', '')} {article.get('description', '')}"
    return bool(_ROUNDUP_TITLE_RE.search(article.get("title", ""))) \
        or len(set(_TICKER_CITATION_RE.findall(text))) >= 2


def _ticker_named(symbol: str, article: dict) -> bool:
    text = f"{article.get('title', '')} {article.get('description', '')}"
    return bool(re.search(rf"\b{re.escape(symbol)}\b", text))


def _g(n: int) -> str:
    """Gap of up to n chars within a sentence; a '.' is allowed only as a decimal point."""
    return rf"(?:[^.\n]|\.(?=\d)){{0,{n}}}"


# (flag, severity, pattern) -- checked against title + description of each
# non-roundup article.
_RULES = [
    # --- dilution / financing ---
    ("offering",          "hard", r"\b(public|registered direct|underwritten|secondary|follow-on|equity|best[- ]efforts|notes?|convertible|share|stock|ATM|at[- ]the[- ]market|rights)\s+offering\b"),
    ("offering",          "hard", rf"\b(prices?|priced|pricing|announces?|launch(es)?|closes?|closed|completes?|upsized?)\b{_g(60)}\boffering\b"),
    ("private_placement", "hard", r"\bprivate (placement|sale)\b|\bPIPE\b|\bregistered direct\b"),
    ("share_issuance",    "hard", rf"\b(sell|sold|sells|selling|issue|issues|issued)\b{_g(40)}\b(shares|stock)\b{_g(40)}\b(investors?|directly|accredited|per share|at \$)"),
    ("share_issuance",    "hard", r"\bcapital raise\b|\braises? (more than |over )?\$?[\d.,]+\s*(million|m\b)|\bstock-buying rights\b|\bsubscription rights\b"),
    ("warrants",          "hard", rf"\bwarrants? (exercise|inducement|repric)|\b(exercises?|exercised|lowers?|reduces?|amend\w*)\b{_g(30)}\bwarrants?\b"),
    ("shelf",             "hard", rf"\b(shelf|S-1|S-3|F-1|F-3)\b{_g(40)}\b(registration|filing|files?|lets)\b|\bnew shelf\b|\bresale registration\b"),
    ("convertible",       "hard", r"\bconvertible (notes?|debentures?|preferred)\b|\bdebentures?\b|\bloan into shares\b|\bdebt[- ]to[- ]equity\b"),
    ("financing",         "hard", rf"\$?[\d.,]+\s*(million|m)\b{_g(20)}\bfinancing\b|\bfinancing (agreement|round|deal)\b"),
    # --- reverse split ---
    ("reverse_split",     "hard", rf"\breverse[- ](stock[- ])?splits?\b|\b1[- ]for[- ]\d+\b|\bshares{_g(25)}\bbecome (one|1)\b|\bshare consolidation\b|\bsplit plan\b"),
    # --- listing / solvency ---
    ("delisting",         "hard", r"\bdelist|\bdeficiency\b|\bminimum bid\b|\$1 (share|bid) rule|\bnon-?compliance\b|\bequity (bar|shortfall|requirement)\b|\blisting rule\b|\bcompliance (period|extension)\b|more time to (clear|regain)"),
    ("going_concern",     "hard", r"\bgoing concern\b|\bchapter 11\b|\binsolven|\bneeds? (more |additional )?funding\b|\bfinancial difficult"),
    # --- bad events / context ---
    ("trial_fail",        "soft", rf"\b(missed|miss|fails?|failed|did not meet|falls short)\b{_g(40)}\b(primary|main|endpoint|goal|trials?|study|studies)\b|\bclinical hold\b|\bcomplete response letter\b|\bCRL\b|\bFDA (rejects?|declines?|hold)\b|\bsetback\b"),
    ("legal",             "soft", r"\bclass action\b|\bsecurities (fraud|litigation)\b|\bshareholder investigation\b|\bSEC (investigation|probe|subpoena|charges)\b|\binvestigation (launched|into)\b|\bopens investigation\b|\bdeadline alert\b|\blawsuit\b"),
    ("exec_exit",         "soft", rf"\b(CEO|CFO|chief executive)\b{_g(30)}\b(resigns?|steps? down|departs?|ousted|terminated)\b|\bsteps down as CEO\b"),
    ("guidance_cut",      "soft", rf"\b(cuts?|lowers?|lowered|slashes?|reduces?|reduced)\b{_g(20)}\b(guidance|outlook|forecast|price target|estimates?|views)\b|\bprice target lowered\b|\bdowngrade[sd]?\b|\bmiss(es|ed)? (estimates|expectations)\b|\bloss(es)? widen"),
    ("deal_off",          "soft", rf"\b(abandons?|terminat\w+|scraps?|walks away|ends?)\b{_g(40)}\b(takeover|merger|acquisition|deal|partnership|agreement)\b|\bpartnership ends\b"),
    ("insider_sell",      "soft", rf"\binsider\b{_g(30)}\b(sale|sells?|sold)\b|\b(CEO|CFO|director)\b{_g(20)}\bsells?\b|\b10b5-1\b|\boffload\b|\b(share|stock) sale\b"),
    ("distress",          "soft", r"\bcash (burn|risk)\b|\bbankruptcy\b|\bweak (balance sheet|financials)\b"),
]
_RX = [(f, s, re.compile(p, re.I)) for f, s, p in _RULES]

# A headline reporting the stock itself going down.
_STOCK_FELL_RX = re.compile(
    r"\b(falls?|fell|slides?|slid|tumbles?|drops?|plunges?|sinks?|craters?|down \d+%|lower|selloff|sell-off)\b", re.I)

# Routine, non-catalyst news: governance, insider buys, conferences, listing
# housekeeping, quote/metrics pages.
_ROUTINE_RX = re.compile(
    r"\b(board of directors|joins .{0,40}board|to (its|the) board|appoints?|names .{0,30}(CEO|CFO|director)"
    r"|presentation|conference|summit|webinar|fireside|Form 4|buys [\d,]+ shares|boosts stake|lifts stake"
    r"|stake to|gift|annual meeting|general meeting|votes?|minimum bid|\$1 minimum|compliance|dividend"
    r"|Stock Forecast|Price Target|Stock Price|Interactive Stock Chart|Quote & History|award)\b", re.I)


def news_flags(symbol: str, articles: list[dict]) -> dict[str, dict]:
    """
    Return {flag: {"severity": ..., "match": ...}} for the first 5 articles
    (the same slice the classifier sees). Empty dict when nothing fires.
    """
    arts = articles[:5]
    hits: dict[str, dict] = {}

    for a in arts:
        if _is_roundup_article(a):
            continue
        text = f"{a.get('title', '')} {a.get('description', '')}"
        for flag, sev, rx in _RX:
            if flag in hits:
                continue
            m = rx.search(text)
            if m:
                hits[flag] = {"severity": sev, "match": m.group(0)[:60]}

    titles = [a.get("title", "") for a in arts if a.get("title")]
    for t in titles:
        m = _STOCK_FELL_RX.search(t)
        if m:
            hits["stock_fell"] = {"severity": "context", "match": t[:60]}
            break
    if titles and all(_ROUTINE_RX.search(t) for t in titles):
        hits["routine_only"] = {"severity": "context", "match": titles[0][:60]}
    if arts and not any(_ticker_named(symbol, a) for a in arts):
        hits["ticker_not_named"] = {"severity": "context", "match": ""}

    return hits
