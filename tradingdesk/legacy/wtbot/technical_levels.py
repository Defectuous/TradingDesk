"""
Technical level detection from intraday bar data.

Pre-market (4:00-9:30 AM ET):
  get_premarket_levels()  ->  pm_high, pm_low, pm_vwap, consolidation zones, bull flag

Session (9:30 AM+ ET):
  get_session_levels()    ->  hod, lod, vwap, ema9, consolidation zones, ABCD pivots

All pattern-detection functions are pure (bars in, dict out, no I/O).
"""
import asyncio
from datetime import date, time as dtime
from typing import Optional

from loguru import logger

from tradingdesk.legacy.wtbot import alpaca_client

# ---------------------------------------------------------------------------
# Detection thresholds
# ---------------------------------------------------------------------------
_CONSOLIDATION_MIN_BARS  = 5    # consecutive bars required to call it a zone
_CONSOLIDATION_MAX_PCT   = 1.5  # max high-to-low range as % of avg price
_BULL_FLAG_MIN_POLE_PCT  = 5.0  # pole must gain at least this %
_BULL_FLAG_MAX_FLAG_PCT  = 2.0  # flag range must stay inside this %
_BULL_FLAG_MIN_POLE_BARS = 3
_BULL_FLAG_MIN_FLAG_BARS = 3
_PIVOT_WINDOW            = 3    # bars each side to confirm a swing pivot
_RECLAIM_LOOKBACK_BARS   = 10   # how far back to look for a genuine dip-then-reclaim


# ---------------------------------------------------------------------------
# Pre-market levels
# ---------------------------------------------------------------------------

def _summarize_premarket(symbol: str, bars: list) -> dict:
    pm_high   = max(float(b.high) for b in bars)
    pm_low    = min(float(b.low)  for b in bars)
    pm_open   = float(bars[0].open)
    pm_close  = float(bars[-1].close)
    pm_vwap   = alpaca_client.calculate_vwap(bars)
    pm_volume = int(sum(b.volume for b in bars))

    zones     = detect_consolidation(bars)
    bull_flag = detect_bull_flag(bars)

    logger.debug(
        f"{symbol}: PM levels  high={pm_high:.2f}  low={pm_low:.2f}  "
        f"vol={pm_volume:,}  vwap={f'{pm_vwap:.2f}' if pm_vwap else 'n/a'}  "
        f"zones={len(zones)}  bull_flag={bull_flag['detected']}"
    )

    return {
        "pm_high":             pm_high,
        "pm_low":              pm_low,
        "pm_open":             pm_open,
        "pm_close":            pm_close,
        "pm_vwap":             pm_vwap,
        "pm_volume":           pm_volume,
        "consolidation_zones": zones,
        "bull_flag":           bull_flag,
        "bars":                bars,
    }


def get_premarket_levels(symbol: str) -> Optional[dict]:
    """
    Fetch and analyse pre-market bars (4:00–9:30 AM ET) for a symbol.

    Returns:
        pm_high, pm_low, pm_open, pm_close, pm_vwap,
        consolidation_zones, bull_flag, bars
    Returns None before 4:00 AM ET or on data failure.
    """
    bars = alpaca_client.get_premarket_bars(symbol)
    if not bars:
        return None
    return _summarize_premarket(symbol, bars)


def get_premarket_levels_for_date(symbol: str, trade_date: date) -> Optional[dict]:
    """
    Same as get_premarket_levels() but for a completed past trading day —
    used by diagnose.py to reconstruct what the pre-market looked like.
    Returns None if Alpaca has no pre-market bars for that date.
    """
    bars = alpaca_client.get_bars_for_date(symbol, trade_date, dtime(4, 0), dtime(9, 30))
    if not bars:
        return None
    return _summarize_premarket(symbol, bars)


def _summarize_session(symbol: str, bars: list) -> dict:
    hod        = max(float(b.high) for b in bars)
    lod        = min(float(b.low)  for b in bars)
    open_price = float(bars[0].open)
    vwap       = alpaca_client.calculate_vwap(bars)
    ema9       = alpaca_client.calculate_ema(bars, period=9)

    zones = detect_consolidation(bars)
    abcd  = detect_abcd(bars)

    logger.debug(
        f"{symbol}: session  HOD={hod:.2f}  LOD={lod:.2f}  "
        f"VWAP={f'{vwap:.2f}' if vwap else 'n/a'}  "
        f"EMA9={f'{ema9:.2f}' if ema9 else 'n/a'}  "
        f"ABCD={'yes' if abcd['detected'] else 'no'}"
    )

    return {
        "hod":                 hod,
        "lod":                 lod,
        "open_price":          open_price,
        "vwap":                vwap,
        "ema9":                ema9,
        "consolidation_zones": zones,
        "abcd":                abcd,
        "bars":                bars,
    }


def get_session_levels(symbol: str) -> Optional[dict]:
    """
    Fetch and analyse the current regular session bars (9:30 AM ET – now).

    Returns:
        hod, lod, open_price, vwap,
        consolidation_zones, abcd, bars
    Returns None before 9:30 AM ET or on data failure.
    """
    bars = alpaca_client.get_session_bars(symbol)
    if not bars:
        return None
    return _summarize_session(symbol, bars)


def get_session_levels_for_date(symbol: str, trade_date: date) -> Optional[dict]:
    """
    Same as get_session_levels() but for a completed past trading day
    (9:30 AM–4:00 PM ET) — used by diagnose.py.
    Returns None if Alpaca has no session bars for that date.
    """
    bars = alpaca_client.get_bars_for_date(symbol, trade_date, dtime(9, 30), dtime(16, 0))
    if not bars:
        return None
    return _summarize_session(symbol, bars)


# ---------------------------------------------------------------------------
# Pure detection functions
# ---------------------------------------------------------------------------

def detect_consolidation(
    bars: list,
    min_bars: int   = _CONSOLIDATION_MIN_BARS,
    max_range_pct: float = _CONSOLIDATION_MAX_PCT,
) -> list[dict]:
    """
    Find tight consolidation zones in a bar list.

    A zone is min_bars+ consecutive bars where the total high-to-low span
    is <= max_range_pct% of the zone's average price.  Zones are extended
    as far as they remain tight, then we skip past them.

    Returns list of:
        {high, low, midpoint, bar_count, start_idx, end_idx}
    """
    if len(bars) < min_bars:
        return []

    zones = []
    i = 0

    while i <= len(bars) - min_bars:
        zone_high = max(float(b.high) for b in bars[i : i + min_bars])
        zone_low  = min(float(b.low)  for b in bars[i : i + min_bars])
        avg_price = (zone_high + zone_low) / 2

        if avg_price == 0:
            i += 1
            continue

        if (zone_high - zone_low) / avg_price * 100 <= max_range_pct:
            # Zone qualifies — extend it as far as it stays tight
            j = i + min_bars
            while j < len(bars):
                ext_high = max(zone_high, float(bars[j].high))
                ext_low  = min(zone_low,  float(bars[j].low))
                ext_avg  = (ext_high + ext_low) / 2
                if ext_avg > 0 and (ext_high - ext_low) / ext_avg * 100 <= max_range_pct:
                    zone_high, zone_low = ext_high, ext_low
                    j += 1
                else:
                    break

            zones.append({
                "high":      round(zone_high, 2),
                "low":       round(zone_low,  2),
                "midpoint":  round((zone_high + zone_low) / 2, 2),
                "bar_count": j - i,
                "start_idx": i,
                "end_idx":   j - 1,
            })
            i = j  # jump past the zone
        else:
            i += 1

    return zones


def detect_bull_flag(bars: list) -> dict:
    """
    Detect a bull flag: sharp upward pole followed by tight consolidation.

    Scans all pole windows (3–30 bars) and flag windows (3–20 bars).
    Keeps the best (largest pole gain that still has a valid flag).

    Returns:
        {detected, pole_gain_pct, pole_start_price, pole_end_price,
         flag_range_pct, flag_high, flag_low, flag_start_idx}
    """
    _no_flag = {
        "detected": False,
        "pole_gain_pct": 0.0, "pole_start_price": 0.0, "pole_end_price": 0.0,
        "flag_range_pct": 0.0, "flag_high": 0.0, "flag_low": 0.0, "flag_start_idx": 0,
    }

    min_total = _BULL_FLAG_MIN_POLE_BARS + _BULL_FLAG_MIN_FLAG_BARS
    if len(bars) < min_total:
        return _no_flag

    best: Optional[dict] = None
    best_pole_pct = 0.0

    for pole_end in range(_BULL_FLAG_MIN_POLE_BARS, len(bars) - _BULL_FLAG_MIN_FLAG_BARS + 1):
        for pole_start in range(max(0, pole_end - 30), pole_end):
            pole_low  = min(float(b.low)  for b in bars[pole_start:pole_end])
            pole_high = max(float(b.high) for b in bars[pole_start:pole_end])
            if pole_low == 0:
                continue
            pole_pct = (pole_high - pole_low) / pole_low * 100

            if pole_pct < _BULL_FLAG_MIN_POLE_PCT or pole_pct <= best_pole_pct:
                continue

            # Measure the flag immediately after the pole
            flag_bars = bars[pole_end : pole_end + 20]
            if len(flag_bars) < _BULL_FLAG_MIN_FLAG_BARS:
                continue

            flag_high = max(float(b.high) for b in flag_bars)
            flag_low  = min(float(b.low)  for b in flag_bars)
            flag_avg  = (flag_high + flag_low) / 2
            flag_pct  = (flag_high - flag_low) / flag_avg * 100 if flag_avg > 0 else 999.0

            if flag_pct <= _BULL_FLAG_MAX_FLAG_PCT:
                best_pole_pct = pole_pct
                best = {
                    "detected":         True,
                    "pole_gain_pct":    round(pole_pct, 2),
                    "pole_start_price": round(float(bars[pole_start].open), 2),
                    "pole_end_price":   round(pole_high, 2),
                    "flag_range_pct":   round(flag_pct, 2),
                    "flag_high":        round(flag_high, 2),
                    "flag_low":         round(flag_low, 2),
                    "flag_start_idx":   pole_end,
                }

    return best if best else _no_flag


def detect_abcd(bars: list) -> dict:
    """
    Detect an approximate bullish ABCD harmonic pattern in session bars.

    ABCD structure:
      A = swing high
      B = pullback low  (retraces 50–90% of prior move)
      C = secondary high (lower than A)
      D = projected entry (C minus AB length  —  equal-leg target)

    Returns:
        {detected, A, B, C, D_target, bc_ratio, quality, current_price}
    or  {detected: False}
    """
    _no_abcd: dict = {"detected": False}

    if len(bars) < 20:
        return _no_abcd

    pivots = _find_pivots(bars)
    if len(pivots) < 3:
        return _no_abcd

    # Walk through pivot triplets looking for A(high) → B(low) → C(high)
    for i in range(len(pivots) - 2):
        pa, pb, pc = pivots[i], pivots[i + 1], pivots[i + 2]

        if pa["type"] != "high" or pb["type"] != "low" or pc["type"] != "high":
            continue

        A, B, C = pa["price"], pb["price"], pc["price"]

        if B >= A or C >= A or C <= B:
            continue

        AB = A - B
        BC = C - B
        if AB == 0:
            continue

        bc_ratio = BC / AB

        # Standard harmonic range: B retraces 50–90% and C bounces 50–90% of AB
        if not (0.50 <= bc_ratio <= 0.90):
            continue

        D_target = round(C - AB, 2)  # equal-leg projection
        if D_target <= 0:
            continue

        quality = "strong" if 0.618 <= bc_ratio <= 0.786 else "moderate"

        return {
            "detected":      True,
            "A":             round(A, 2),
            "B":             round(B, 2),
            "C":             round(C, 2),
            "D_target":      D_target,
            "bc_ratio":      round(bc_ratio, 2),
            "quality":       quality,
            "current_price": round(float(bars[-1].close), 2),
        }

    return _no_abcd


def detect_reclaim(
    bars: list,
    key_level: Optional[float],
    lookback: int = _RECLAIM_LOOKBACK_BARS,
) -> dict:
    """
    Check whether the most recent bars show a genuine dip-below-then-close-
    back-above `key_level` (the VWAP or 9 EMA the caller has already picked
    as the live support level) — a real shakeout-and-reclaim, not just price
    currently sitting near the level on any given tick.

    Without this check, a stock merely chopping near (or still fading below)
    VWAP scored identically to one that actually dipped and bounced — see
    todolist.md 2026-08-17: XPON's live vwap_bounce entry landed on a bar
    that had itself closed *below* VWAP, mid-fade, because "close to the
    level" and "just bounced off the level" were treated as the same thing.

    Also returns `shakeout_low` — the lowest `low` printed from the dip bar
    through the reclaim bar — the real structural low the stop should
    reference, instead of a fixed percentage below the support level. Three
    independent noise-stopped WTBot losses (CAPR 0.9%, DRMA 0.77%, IPST
    0.68% stops) all used a fixed-% stop that had no relationship to where
    price actually bottomed; see todolist.md 2026-08-18.

    Returns:
        {reclaimed: bool, bars_since_dip: Optional[int], shakeout_low: Optional[float]}
        reclaimed=True only if the last bar closed >= key_level AND at least
        one bar within `lookback` before it closed below key_level.
    """
    _none = {"reclaimed": False, "bars_since_dip": None, "shakeout_low": None}
    if not bars or len(bars) < 2 or not key_level:
        return _none

    recent = bars[-lookback:]
    if float(recent[-1].close) < key_level:
        return _none  # hasn't reclaimed yet

    dip_idx = None
    for i in range(len(recent) - 2, -1, -1):
        if float(recent[i].close) < key_level:
            dip_idx = i
            break

    if dip_idx is None:
        return _none  # never actually dipped

    shakeout_low = min(float(b.low) for b in recent[dip_idx:])
    return {
        "reclaimed":      True,
        "bars_since_dip": len(recent) - 1 - dip_idx,
        "shakeout_low":   shakeout_low,
    }


# ---------------------------------------------------------------------------
# Pivot helpers
# ---------------------------------------------------------------------------

def _find_pivots(bars: list, window: int = _PIVOT_WINDOW) -> list[dict]:
    """
    Identify alternating swing highs and lows.

    A swing high at index i: bars[i].high is the max over bars[i-window : i+window+1].
    A swing low  at index i: bars[i].low  is the min over bars[i-window : i+window+1].

    Consecutive pivots of the same type are collapsed to keep only the more extreme one.
    """
    n = len(bars)
    raw: list[dict] = []

    for i in range(window, n - window):
        surround = bars[i - window : i + window + 1]
        hi = float(bars[i].high)
        lo = float(bars[i].low)

        if hi == max(float(b.high) for b in surround):
            raw.append({"type": "high", "price": hi, "idx": i})
        elif lo == min(float(b.low) for b in surround):
            raw.append({"type": "low",  "price": lo, "idx": i})

    if not raw:
        return []

    # Collapse consecutive same-type pivots
    deduped = [raw[0]]
    for p in raw[1:]:
        last = deduped[-1]
        if p["type"] == last["type"]:
            keep = (p if (p["type"] == "high" and p["price"] > last["price"])
                      or (p["type"] == "low"  and p["price"] < last["price"])
                      else last)
            deduped[-1] = keep
        else:
            deduped.append(p)

    return deduped


# ---------------------------------------------------------------------------
# Async wrappers
# ---------------------------------------------------------------------------

async def get_premarket_levels_async(symbol: str) -> Optional[dict]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, get_premarket_levels, symbol)


async def get_session_levels_async(symbol: str) -> Optional[dict]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, get_session_levels, symbol)
