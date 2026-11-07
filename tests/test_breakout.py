from __future__ import annotations

from tests.fakes import bars
from tradingdesk.agents.breakout import plan_breakout
from tradingdesk.models import ProposedTrade
from tradingdesk.settings import ManagerConfig

CFG = ManagerConfig()


def pm(high: float = 4.24, lows: list[float] | None = None) -> dict:
    return {"pm_high": high, "bars": bars(lows or [4.10, 4.12, 4.15, 4.13, 4.16])}


def test_premarket_breakout_levels():
    t = plan_breakout("premarket", 4.20, pm(), None, CFG)
    assert isinstance(t, ProposedTrade)
    assert t.entry == 4.24                       # 4.24 * 1.001 = 4.2442 -> 4.24
    assert t.stop == round(4.10 * 0.998, 2)      # under the lowest of the last 5 bars
    assert t.target == round(t.entry + 2 * (t.entry - t.stop), 2)
    assert t.target_2 == round(t.entry + 3 * (t.entry - t.stop), 2)
    assert t.entry_type == "stop_limit"
    assert "premarket high" in t.strategy


def test_open_session_uses_high_of_day():
    sess = {"hod": 5.00, "bars": bars([4.80, 4.85, 4.90, 4.88, 4.92])}
    t = plan_breakout("open", 4.95, pm(), sess, CFG)
    assert isinstance(t, ProposedTrade) and "high of day" in t.strategy
    assert t.entry == 5.00 and t.stop == round(4.80 * 0.998, 2)


def test_early_open_stop_spans_premarket_bars():
    """With only 2 session bars, the last 5 bars reach back into premarket."""
    sess = {"hod": 4.30, "bars": bars([4.20, 4.22])}
    t = plan_breakout("open", 4.28, pm(), sess, CFG)
    assert isinstance(t, ProposedTrade) and t.stop == round(4.13 * 0.998, 2)


def test_open_falls_back_to_premarket_high():
    t = plan_breakout("open", 4.20, pm(), None, CFG)
    assert isinstance(t, ProposedTrade) and "premarket high" in t.strategy


def test_minimum_stop_distance():
    t = plan_breakout("premarket", 4.20, pm(lows=[4.23] * 5), None, CFG)
    assert isinstance(t, ProposedTrade)
    assert t.stop == round(t.entry * 0.99, 2)    # clamped to 1% below entry


def test_stop_too_wide():
    reason = plan_breakout("premarket", 4.20, pm(lows=[3.50] * 5), None, CFG)
    assert isinstance(reason, str) and "below entry" in reason


def test_chasing_rejected():
    reason = plan_breakout("premarket", 4.40, pm(), None, CFG)
    assert isinstance(reason, str) and "past the entry" in reason


def test_not_enough_bars():
    reason = plan_breakout("premarket", 4.20, {"pm_high": 4.24, "bars": bars([4.1, 4.2])}, None, CFG)
    assert isinstance(reason, str) and "bars" in reason


def test_no_levels():
    assert isinstance(plan_breakout("open", 4.20, None, None, CFG), str)


def test_sub_dollar_prices_keep_precision():
    t = plan_breakout("premarket", 0.80, pm(high=0.8123, lows=[0.79] * 5), None, CFG)
    assert isinstance(t, ProposedTrade) and t.entry == round(0.8123 * 1.001, 4)
