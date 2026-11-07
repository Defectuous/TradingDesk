"""
Alpaca Markets data client.

Provides market snapshots and asset universe filtering.
Rate-limit protection uses exponential backoff on 429 responses.
"""
import asyncio
import time
from datetime import date, datetime, timedelta, time as dtime, timezone
from typing import Optional

import pytz
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import (
    StockBarsRequest,
    StockSnapshotRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from loguru import logger

from tradingdesk.legacy.wtbot import config

_ET = pytz.timezone("America/New_York")


# ---------------------------------------------------------------------------
# Client singleton — constructed once, reused across all calls.
# ---------------------------------------------------------------------------
_data_client: Optional[StockHistoricalDataClient] = None

# Pre-market baseline volume is the same for every snapshot taken on a given
# symbol/day (it only depends on prior days), but get_snapshot() can be
# called repeatedly during the pre-market scan — cache it to avoid re-pulling
# ~45 days of minute bars on every call.
_premarket_avg_volume_cache: dict[tuple[str, date], Optional[float]] = {}


def _get_data_client() -> StockHistoricalDataClient:
    global _data_client
    if _data_client is None:
        _data_client = StockHistoricalDataClient(
            api_key=config.ALPACA_API_KEY,
            secret_key=config.ALPACA_SECRET_KEY,
        )
    return _data_client


# ---------------------------------------------------------------------------
# Backoff helper
# ---------------------------------------------------------------------------
def _backoff_retry(fn, *args, retries: int = 4, **kwargs):
    """Call fn with exponential backoff on rate-limit errors."""
    delay = 1.0
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            msg = str(exc).lower()
            if "429" in msg or "too many requests" in msg or "rate limit" in msg:
                logger.warning(
                    f"Rate limited — retry {attempt + 1}/{retries} in {delay:.1f}s"
                )
                time.sleep(delay)
                delay = min(delay * 2, 30)
                last_exc = exc
            else:
                raise
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_snapshot(symbol: str) -> Optional[dict]:
    """
    Return a enriched snapshot dict for one symbol, or None on failure.

    Keys: current_price, prev_close, daily_change_pct,
          today_volume, avg_volume_30d, rvol
    """
    client = _get_data_client()
    try:
        snap_req = StockSnapshotRequest(symbol_or_symbols=[symbol])
        snaps = _backoff_retry(client.get_stock_snapshot, snap_req)
        snap = snaps.get(symbol)
        if snap is None:
            logger.debug(f"{symbol}: no snapshot data returned")
            return None

        current_price: float = float(snap.latest_trade.price)

        now_et = datetime.now(_ET)
        is_regular_session = now_et.time() >= dtime(9, 30)

        # Before today's regular session has printed, Alpaca's daily_bar is
        # still YESTERDAY's bar and previous_daily_bar is the day BEFORE
        # yesterday. Taking them at face value made every pre-market gap_pct
        # a two-day move and today_volume yesterday's full-session volume —
        # 125 of 132 pre-9:30 watchlist rows on 2026-09-17..25 were affected
        # (JAGX 9/23 logged +512% while actually -53%; APUS 9/25 RVOL 43,336x).
        # This is also the real cause of the STKH/JWEL negative-gap symptom on
        # 2026-08-10. Resolve by date: if daily_bar isn't today's, it IS the
        # previous close, and today's volume has to come from today's own bars.
        daily_bar = snap.daily_bar
        daily_bar_is_today = (
            daily_bar is not None
            and daily_bar.timestamp.astimezone(_ET).date() == now_et.date()
        )

        if daily_bar_is_today:
            prev_close: float = float(snap.previous_daily_bar.close)
            today_volume: int = int(daily_bar.volume)
            # Only trust daily_bar.open as the session open once the regular
            # session has opened — before that it's today's first pre-market
            # print, not an opening price.
            open_price: float = float(daily_bar.open) if is_regular_session else current_price
        else:
            prev_close = float(daily_bar.close) if daily_bar is not None else float(snap.previous_daily_bar.close)
            open_price = current_price
            day_start = _ET.localize(datetime.combine(now_et.date(), dtime(4, 0)))
            today_volume = (
                sum(int(b.volume) for b in get_intraday_bars(symbol, start=day_start, end=now_et))
                if now_et > day_start
                else 0
            )

        daily_change_pct: float = (
            ((current_price - prev_close) / prev_close) * 100
            if prev_close > 0
            else 0.0
        )
        # gap_pct: opening gap vs prev close.
        # Pre-market: open_price == current_price, so gap_pct == daily_change_pct.
        # Market hours: open_price is the actual session open.
        gap_pct: float = (
            ((open_price - prev_close) / prev_close) * 100
            if prev_close > 0
            else 0.0
        )

        # Pre-market: today_volume is only the cumulative pre-market print so
        # far, so comparing it against a
        # full 6.5-hour regular-session average understates RVOL for any
        # stock moving before 9:30 — e.g. AEMD read as 0.66x RVOL on
        # 2026-09-17 while up 483% pre-market, purely from this mismatch.
        # Compare like against like: pre-market volume so far vs. a
        # pre-market-only historical baseline. Falls back to the
        # regular-session average if there isn't enough pre-market history.
        if is_regular_session:
            avg_volume_30d = _get_avg_volume(symbol, client)
        else:
            avg_volume_30d = _get_premarket_avg_volume(symbol, client, now_et.date())
            if avg_volume_30d is None:
                avg_volume_30d = _get_avg_volume(symbol, client)
        rvol: float = (
            today_volume / avg_volume_30d
            if avg_volume_30d and avg_volume_30d > 0
            else 0.0
        )

        return {
            "symbol": symbol,
            "current_price": current_price,
            "prev_close": prev_close,
            "open_price": open_price,
            "daily_change_pct": daily_change_pct,
            "gap_pct": gap_pct,
            "today_volume": today_volume,
            "avg_volume_30d": avg_volume_30d,
            "rvol": rvol,
        }
    except Exception as exc:
        logger.debug(f"{symbol}: snapshot error — {exc}")
        return None


def _get_avg_volume(symbol: str, client: StockHistoricalDataClient) -> Optional[float]:
    """Fetch the last N calendar days of daily bars and return the outlier-trimmed mean volume."""
    end = datetime.now(_ET).date()
    # Fetch extra days to account for weekends/holidays
    start = end - timedelta(days=config.RVOL_LOOKBACK_DAYS + 14)
    try:
        bar_req = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
        )
        bars_resp = _backoff_retry(client.get_stock_bars, bar_req)
        bars = bars_resp.data.get(symbol, [])
        if not bars:
            return None
        # Use only the most recent RVOL_LOOKBACK_DAYS trading days
        recent = bars[-config.RVOL_LOOKBACK_DAYS :]
        volumes = [bar.volume for bar in recent]
        return _trimmed_avg_volume(volumes) if volumes else None
    except Exception as exc:
        logger.debug(f"{symbol}: avg-volume error — {exc}")
        return None


def _get_premarket_avg_volume(
    symbol: str, client: StockHistoricalDataClient, as_of: date
) -> Optional[float]:
    """
    Outlier-trimmed mean of pre-market-only (4:00-9:30 ET) volume over the
    last RVOL_LOOKBACK_DAYS trading days before `as_of`.

    Used so a pre-market RVOL reading compares today's partial pre-market
    volume against other days' pre-market volume, not a full regular-session
    average (see get_snapshot). Returns None if fewer than 5 days of
    pre-market bar history are available, so the caller can fall back to the
    regular-session baseline instead of a baseline built from a handful of
    days.
    """
    cache_key = (symbol, as_of)
    if cache_key in _premarket_avg_volume_cache:
        return _premarket_avg_volume_cache[cache_key]

    start_date = as_of - timedelta(days=config.RVOL_LOOKBACK_DAYS + 14)
    start = _ET.localize(datetime.combine(start_date, dtime(4, 0)))
    end = _ET.localize(datetime.combine(as_of, dtime(4, 0)))  # exclude today's own pre-market

    bars = get_intraday_bars(symbol, start=start, end=end, timeframe_min=1)

    daily_totals: dict[date, int] = {}
    for bar in bars:
        ts_et = bar.timestamp.astimezone(_ET)
        if dtime(4, 0) <= ts_et.time() < dtime(9, 30):
            d = ts_et.date()
            daily_totals[d] = daily_totals.get(d, 0) + int(bar.volume)

    volumes = [v for _, v in sorted(daily_totals.items())][-config.RVOL_LOOKBACK_DAYS :]
    result = _trimmed_avg_volume(volumes) if len(volumes) >= 5 else None
    _premarket_avg_volume_cache[cache_key] = result
    return result


def _trimmed_avg_volume(volumes: list[int]) -> float:
    """
    Mean volume with breakout/blow-off days trimmed out.

    A stock already in a multi-day runner has one or more anomalous volume
    days sitting inside its own lookback window. A plain mean lets those
    days inflate the baseline enough that a legitimate Day-2/Day-3
    continuation move no longer reads as high relative volume — exactly the
    setups Ross Cameron's approach depends on catching. Days more than
    RVOL_OUTLIER_MULTIPLE times the window's median are excluded from the
    baseline; if too many days are flagged as outliers (a stock that's been
    "in play" for most of the window), fall back to the plain mean.
    """
    n = len(volumes)
    sorted_vols = sorted(volumes)
    mid = n // 2
    median = (
        sorted_vols[mid]
        if n % 2
        else (sorted_vols[mid - 1] + sorted_vols[mid]) / 2
    )

    if median <= 0:
        return sum(volumes) / n

    threshold = median * config.RVOL_OUTLIER_MULTIPLE
    filtered = [v for v in volumes if v <= threshold]

    if len(filtered) < max(5, n // 3):
        return sum(volumes) / n

    return sum(filtered) / len(filtered)


async def get_snapshot_async(symbol: str) -> Optional[dict]:
    """Async wrapper so the scan loop can fan out concurrently."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, get_snapshot, symbol)


# ---------------------------------------------------------------------------
# Historical day reconstruction (diagnose.py) — completed past trading days,
# built from daily bars instead of the live snapshot endpoint.
# ---------------------------------------------------------------------------

def get_historical_snapshot(symbol: str, trade_date: date) -> Optional[dict]:
    """
    Reconstruct a snapshot-shaped dict for a COMPLETED past trading day.

    current_price is the day's HIGH — the peak the stock reached, i.e. the
    best moment the live scanner could have caught it — not the close.
    daily_change_pct and gap_pct are derived the same way, so a diagnostic
    run answers "could the pillars ever have passed that day?" rather than
    "where did it end up."

    Returns None if Alpaca has no daily bar for trade_date (holiday, no
    trades, or outside your data plan's history) or no prior bar to use
    as prev_close.
    """
    client = _get_data_client()
    lookback_start = trade_date - timedelta(days=config.RVOL_LOOKBACK_DAYS + 20)

    try:
        bar_req = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame.Day,
            start=lookback_start,
            end=trade_date + timedelta(days=1),
        )
        bars_resp = _backoff_retry(client.get_stock_bars, bar_req)
        bars = list(bars_resp.data.get(symbol, []))
    except Exception as exc:
        logger.debug(f"{symbol}: historical daily-bar error — {exc}")
        return None

    if not bars:
        return None

    idx = next(
        (i for i, b in enumerate(bars) if b.timestamp.astimezone(_ET).date() == trade_date),
        None,
    )
    if idx is None or idx == 0:
        logger.debug(f"{symbol}: no daily bar (or no prior bar) for {trade_date}")
        return None

    day_bar    = bars[idx]
    prev_close = float(bars[idx - 1].close)

    baseline = bars[max(0, idx - config.RVOL_LOOKBACK_DAYS) : idx]
    avg_volume_30d = _trimmed_avg_volume([b.volume for b in baseline]) if baseline else None

    current_price = float(day_bar.high)  # peak reached that day
    open_price    = float(day_bar.open)
    today_volume  = int(day_bar.volume)

    daily_change_pct = (
        ((current_price - prev_close) / prev_close) * 100 if prev_close > 0 else 0.0
    )
    gap_pct = (
        ((open_price - prev_close) / prev_close) * 100 if prev_close > 0 else 0.0
    )
    rvol = (
        today_volume / avg_volume_30d
        if avg_volume_30d and avg_volume_30d > 0
        else 0.0
    )

    return {
        "symbol":           symbol,
        "current_price":    current_price,
        "close_price":      float(day_bar.close),
        "prev_close":       prev_close,
        "open_price":       open_price,
        "daily_change_pct": daily_change_pct,
        "gap_pct":          gap_pct,
        "today_volume":     today_volume,
        "avg_volume_30d":   avg_volume_30d,
        "rvol":             rvol,
    }


def get_bars_for_date(
    symbol: str,
    trade_date: date,
    start_time: dtime,
    end_time: dtime,
) -> list:
    """
    Fetch 1-minute bars for `symbol` between start_time and end_time (ET) on
    trade_date. Thin wrapper around get_intraday_bars for reconstructing a
    historical pre-market or session window (see technical_levels.py).
    """
    start = _ET.localize(datetime.combine(trade_date, start_time))
    end   = _ET.localize(datetime.combine(trade_date, end_time))
    return get_intraday_bars(symbol, start=start, end=end, timeframe_min=1)


# ---------------------------------------------------------------------------
# Intraday bar data
# ---------------------------------------------------------------------------

def get_intraday_bars(
    symbol: str,
    start: datetime,
    end: datetime,
    timeframe_min: int = 1,
) -> list:
    """
    Fetch OHLCV bars for a symbol over an arbitrary intraday window.

    Args:
        symbol:        Ticker.
        start:         Window start (timezone-aware datetime).
        end:           Window end (timezone-aware datetime).
        timeframe_min: Bar size in minutes — 1 or 5 are the common values.

    Returns:
        List of bar objects (each has .open .high .low .close .volume .timestamp).
        Returns [] on any error so callers never need to handle exceptions.
    """
    client = _get_data_client()
    tf = TimeFrame.Minute if timeframe_min == 1 else TimeFrame(timeframe_min, TimeFrameUnit.Minute)

    try:
        bar_req = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=tf,
            start=start,
            end=end,
        )
        bars_resp = _backoff_retry(client.get_stock_bars, bar_req)
        bars = list(bars_resp.data.get(symbol, []))
        logger.debug(f"{symbol}: {len(bars)} {timeframe_min}m bars ({start:%H:%M}–{end:%H:%M})")
        return bars
    except Exception as exc:
        logger.debug(f"{symbol}: intraday bars error — {exc}")
        return []


def get_premarket_bars(symbol: str) -> list:
    """
    Fetch 1-minute bars for today's pre-market session (4:00 AM – 9:30 AM ET).
    Caps end at now so the call never requests future data.
    Returns [] before 4:00 AM ET.
    """
    now_et = datetime.now(_ET)
    today  = now_et.date()
    start  = _ET.localize(datetime.combine(today, dtime(4, 0)))
    end    = _ET.localize(datetime.combine(today, dtime(9, 30)))
    end    = min(end, now_et)

    if end <= start:
        logger.debug(f"{symbol}: pre-market not yet open ({now_et:%H:%M} ET < 04:00)")
        return []

    return get_intraday_bars(symbol, start=start, end=end, timeframe_min=1)


def get_latest_quotes(symbols: list[str]) -> dict[str, dict]:
    """
    Last trade + bid/ask for many symbols in ONE snapshot request — the
    premarket trader polls every few seconds, and a per-symbol get_snapshot()
    (which also pulls daily bars and average volume) would blow the data-API
    rate limit. Symbols with no data are simply missing from the result.

    Returns {symbol: {"price": float, "bid": float, "ask": float}}.
    """
    if not symbols:
        return {}
    client = _get_data_client()
    try:
        snaps = _backoff_retry(client.get_stock_snapshot, StockSnapshotRequest(symbol_or_symbols=symbols))
    except Exception as exc:
        logger.warning(f"get_latest_quotes failed for {len(symbols)} symbol(s) — {exc}")
        return {}
    out: dict[str, dict] = {}
    for sym, snap in snaps.items():
        try:
            q = snap.latest_quote
            out[sym] = {
                "price": float(snap.latest_trade.price),
                "bid":   float(q.bid_price) if q and q.bid_price else 0.0,
                "ask":   float(q.ask_price) if q and q.ask_price else 0.0,
            }
        except Exception:
            continue
    return out


def get_session_bars(symbol: str) -> list:
    """
    Fetch 1-minute bars for the current regular session (9:30 AM ET – now).
    Used for live VWAP and HOD tracking during the trading window.
    """
    today = datetime.now(_ET).date()
    start = _ET.localize(datetime.combine(today, dtime(9, 30)))
    end   = datetime.now(timezone.utc)
    return get_intraday_bars(symbol, start=start, end=end, timeframe_min=1)


def calculate_vwap(bars: list) -> Optional[float]:
    """
    Calculate VWAP from a list of bar objects.

    Formula: Σ(typical_price × volume) / Σ(volume)
    typical_price = (high + low + close) / 3

    Returns None if bars is empty or total volume is zero.
    """
    if not bars:
        return None

    cum_tp_vol = 0.0
    cum_vol    = 0.0

    for bar in bars:
        typical = (float(bar.high) + float(bar.low) + float(bar.close)) / 3
        vol     = float(bar.volume)
        cum_tp_vol += typical * vol
        cum_vol    += vol

    return cum_tp_vol / cum_vol if cum_vol > 0 else None


def calculate_ema(bars: list, period: int = 9) -> Optional[float]:
    """
    Calculate the exponential moving average of bar closes.

    Seeds with a simple average of the first `period` closes, then applies
    the standard EMA multiplier (2 / (period + 1)) to the rest. Returns None
    if there aren't at least `period` bars — an EMA over a short warm-up
    window doesn't mean anything yet.
    """
    if len(bars) < period:
        return None

    closes = [float(bar.close) for bar in bars]
    multiplier = 2 / (period + 1)

    ema = sum(closes[:period]) / period
    for close in closes[period:]:
        ema = (close - ema) * multiplier + ema

    return ema


# ---------------------------------------------------------------------------
# Async wrappers for the new intraday functions
# ---------------------------------------------------------------------------

async def get_premarket_bars_async(symbol: str) -> list:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, get_premarket_bars, symbol)


async def get_session_bars_async(symbol: str) -> list:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, get_session_bars, symbol)


async def get_intraday_bars_async(
    symbol: str,
    start: datetime,
    end: datetime,
    timeframe_min: int = 1,
) -> list:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: get_intraday_bars(symbol, start, end, timeframe_min),
    )


async def get_universe_async(
    max_symbols: Optional[int] = None,
) -> list[str]:
    """
    Fetch all active, tradable US equity symbols then filter to those
    currently priced $1–$20 using snapshot data.

    This is an expensive call — it fetches snapshots in batches.
    max_symbols caps the universe size (useful for testing).
    """
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetAssetsRequest
    from alpaca.trading.enums import AssetClass

    trading_client = TradingClient(
        api_key=config.ALPACA_API_KEY,
        secret_key=config.ALPACA_SECRET_KEY,
        paper=config.ALPACA_PAPER,
        # Raw dicts: alpaca-py's AssetExchange enum rejects newer venues
        # (e.g. TXSE, Oct 2026) and fails the whole list on one bad row.
        raw_data=True,
    )

    logger.info("Fetching active US equity universe from Alpaca…")
    try:
        asset_req = GetAssetsRequest(asset_class=AssetClass.US_EQUITY)
        all_assets = _backoff_retry(trading_client.get_all_assets, asset_req)
    except Exception as exc:
        logger.error(f"Failed to fetch asset list: {exc}")
        return []

    tradable = [
        a["symbol"]
        for a in all_assets
        if a.get("tradable") and a.get("status") == "active" and not a["symbol"].endswith(".")
    ]

    logger.info(f"Found {len(tradable)} tradable symbols — fetching snapshots in bulk…")

    # Batch snapshot request to cut down on round-trips
    data_client = _get_data_client()
    qualified: list[str] = []
    BATCH = 1000

    for i in range(0, len(tradable), BATCH):
        batch = tradable[i : i + BATCH]
        try:
            snap_req = StockSnapshotRequest(symbol_or_symbols=batch)
            snaps = _backoff_retry(data_client.get_stock_snapshot, snap_req)
            for sym, snap in snaps.items():
                try:
                    price = float(snap.latest_trade.price)
                    if config.UNIVERSE_MIN_PRICE <= price <= config.MAX_PRICE:
                        qualified.append(sym)
                except Exception:
                    pass
        except Exception as exc:
            logger.warning(f"Batch snapshot error (offset {i}): {exc}")

    # Cap AFTER price filter so max_symbols applies to the relevant universe
    if max_symbols:
        qualified = qualified[:max_symbols]

    logger.info(
        f"Price-filtered universe: {len(qualified)} symbols "
        f"(${config.UNIVERSE_MIN_PRICE}–${config.MAX_PRICE})"
    )
    return qualified
