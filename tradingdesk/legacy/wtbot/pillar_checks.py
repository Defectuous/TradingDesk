"""
Ross Cameron's 5-Pillar evaluation logic.

All thresholds come from config.py and can be tuned via .env.
Pillar logic is pure (no I/O) — easy to unit-test in isolation.
"""
from typing import Any, Optional
from tradingdesk.legacy.wtbot import config


def check_all_pillars(
    snapshot: dict,
    float_shares: Optional[int],
    has_catalyst: bool,
    catalyst_result: Optional[dict],
) -> dict[str, Any]:
    """
    Evaluate all 5 pillars and return a structured result dict.

    Args:
        snapshot:        Output of alpaca_client.get_snapshot()
        float_shares:    Float share count (None = unknown)
        has_catalyst:    Whether a news catalyst was detected
        catalyst_result: Full output of ai_catalyst.classify_catalyst()

    Returns:
        Dict with per-pillar results, all_pass bool, strong_momentum flag,
        and an integer score (0–5).
    """
    rvol: float = snapshot.get("rvol", 0.0)
    daily_change_pct: float = snapshot.get("daily_change_pct", 0.0)
    current_price: float = snapshot.get("current_price", 0.0)

    # ------------------------------------------------------------------
    # Pillar 1 — Relative Volume ≥ 5× (unusual buying interest)
    # ------------------------------------------------------------------
    p1 = {
        "pass": rvol >= config.MIN_RVOL,
        "value": round(rvol, 2),
        "threshold": config.MIN_RVOL,
    }

    # ------------------------------------------------------------------
    # Pillar 2 — Intraday Momentum ≥ 10% daily change
    # ------------------------------------------------------------------
    p2 = {
        "pass": daily_change_pct >= config.MIN_DAILY_CHANGE_PCT,
        "value": round(daily_change_pct, 2),
        "threshold": config.MIN_DAILY_CHANGE_PCT,
    }

    # ------------------------------------------------------------------
    # Pillar 3 — News Catalyst present and positive
    # ------------------------------------------------------------------
    catalyst_type = "none"
    catalyst_confidence = 0.0
    catalyst_sentiment = "neutral"
    catalyst_summary = ""
    no_news = False

    if catalyst_result:
        catalyst_type = catalyst_result.get("catalyst_type", "none")
        catalyst_confidence = catalyst_result.get("confidence", 0.0)
        catalyst_sentiment = catalyst_result.get("sentiment", "neutral")
        catalyst_summary = catalyst_result.get("summary", "")
        no_news = catalyst_result.get("no_news", False)

    # Catalyst passes if there is one, it's positive, and it's not "none"/"negative".
    # If there's simply no news to evaluate (as opposed to news that fails to
    # justify the move), don't let the catalyst pillar block qualification —
    # treat it as neutral rather than penalizing the stock for missing data.
    p3_pass = no_news or (
        has_catalyst
        and catalyst_type not in ("none", "negative", "unclear")
        and catalyst_sentiment == "positive"
        and catalyst_confidence >= 0.6
    )

    p3 = {
        "pass": p3_pass,
        "catalyst_type": catalyst_type,
        "sentiment": catalyst_sentiment,
        "confidence": round(catalyst_confidence, 2),
        "summary": catalyst_summary,
        "no_news": no_news,
    }

    # ------------------------------------------------------------------
    # Pillar 4 — Price in the sweet spot ($1–$20)
    # ------------------------------------------------------------------
    p4 = {
        "pass": config.MIN_PRICE <= current_price <= config.MAX_PRICE,
        "value": round(current_price, 2),
        "min": config.MIN_PRICE,
        "max": config.MAX_PRICE,
    }

    # ------------------------------------------------------------------
    # Pillar 5 — Low float (≤ 10 million shares)
    # ------------------------------------------------------------------
    if float_shares is None:
        p5 = {
            "pass": False,
            "value": None,
            "threshold": config.MAX_FLOAT_SHARES,
            "note": "N/A — float data unavailable",
        }
    else:
        p5 = {
            "pass": float_shares <= config.MAX_FLOAT_SHARES,
            "value": float_shares,
            "threshold": config.MAX_FLOAT_SHARES,
            "note": None,
        }

    # ------------------------------------------------------------------
    # Aggregate
    # ------------------------------------------------------------------
    pillars = [p1["pass"], p2["pass"], p3["pass"], p4["pass"], p5["pass"]]
    score = sum(pillars)
    all_pass = all(pillars)
    strong_momentum = daily_change_pct >= config.STRONG_MOMENTUM_PCT

    # ------------------------------------------------------------------
    # Reduced-size path — RVOL/momentum/price/float all check out but the
    # catalyst is missing, vague, or low-confidence (common for low-float
    # runners on generic PR rather than a textbook catalyst type). Mirrors
    # the no-news half-size handling agent.py's surge scanner already uses,
    # instead of silently discarding the setup for failing Pillar 3 alone.
    # Explicitly excludes a *confirmed bad* catalyst (negative type or
    # sentiment) — that's a disqualified setup, not a missing-data one.
    # ------------------------------------------------------------------
    catalyst_is_bad = catalyst_type == "negative" or catalyst_sentiment == "negative"
    non_catalyst_pass = p1["pass"] and p2["pass"] and p4["pass"] and p5["pass"]
    qualifies_reduced = non_catalyst_pass and not p3["pass"] and not catalyst_is_bad and strong_momentum
    qualifies = all_pass or qualifies_reduced

    return {
        "pillar_1_rvol": p1,
        "pillar_2_momentum": p2,
        "pillar_3_catalyst": p3,
        "pillar_4_price": p4,
        "pillar_5_float": p5,
        "all_pass": all_pass,
        "strong_momentum": strong_momentum,
        "qualifies_reduced": qualifies_reduced,
        "qualifies": qualifies,
        "score": score,
    }
