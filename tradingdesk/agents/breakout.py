"""
The one setup the Manager proposes: a momentum breakout (pure function, no I/O).

  breakout level  premarket high in premarket; high of day in the open session
                  (falls back to the premarket high before any session bars exist)
  entry           just above the level (stop-limit)
  stop            under the low of the last N one-minute bars, clamped to
                  [min_stop_pct, max_stop_pct] below entry (too wide = no trade)
  target          entry + target_r x risk (partial profit); target_2 at target_2_r
"""
from __future__ import annotations

from typing import Any

from tradingdesk.models import ProposedTrade
from tradingdesk.sessions import TradingSession
from tradingdesk.settings import ManagerConfig


def _round(px: float) -> float:
    return round(px, 2) if px >= 1 else round(px, 4)


def plan_breakout(
    session: TradingSession,
    price: float,
    premarket_levels: dict[str, Any] | None,
    session_levels: dict[str, Any] | None,
    cfg: ManagerConfig,
) -> ProposedTrade | str:
    """Return a ProposedTrade, or a one-line reason why there is no trade."""
    pm, sess = premarket_levels or {}, session_levels or {}
    if session == "open" and sess.get("hod"):
        level, level_name = float(sess["hod"]), "high of day"
        bars = list(pm.get("bars") or []) + list(sess.get("bars") or [])
    elif pm.get("pm_high"):
        level, level_name = float(pm["pm_high"]), "premarket high"
        bars = list(pm.get("bars") or []) + list(sess.get("bars") or [])
    else:
        return "no breakout level (no premarket or session bars)"
    if len(bars) < cfg.stop_lookback_bars:
        return f"only {len(bars)} one-minute bars; need {cfg.stop_lookback_bars} to place a stop"

    entry = _round(level * (1 + cfg.entry_buffer_pct / 100))
    if price > entry * (1 + cfg.max_chase_pct / 100):
        return f"price {price} already {((price / entry) - 1) * 100:.1f}% past the entry {entry}"

    recent_low = min(float(b.low) for b in bars[-cfg.stop_lookback_bars:])
    stop = _round(recent_low * (1 - cfg.stop_buffer_pct / 100))
    stop = min(stop, _round(entry * (1 - cfg.min_stop_pct / 100)))
    stop_pct = (entry - stop) / entry * 100
    if stop_pct > cfg.max_stop_pct:
        return f"stop {stop} is {stop_pct:.1f}% below entry (max {cfg.max_stop_pct}%)"
    if stop <= 0:
        return "stop would be at or below zero"

    risk = entry - stop
    return ProposedTrade(
        entry=entry,
        stop=stop,
        target=_round(entry + cfg.target_r * risk),
        target_2=_round(entry + cfg.target_2_r * risk),
        entry_type="stop_limit",
        strategy=f"breakout of {level_name} {level:g}",
    )
