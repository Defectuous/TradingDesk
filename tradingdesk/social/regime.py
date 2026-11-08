"""
Market regime (plain Python): supportive / neutral / hostile, from the index
ETFs' trend and the VIX level. Small-cap momentum works best when the tape is
risk-on; a hostile regime is context for the Skeptic and for you on the ticket.

Score (config sentiment.regime):
  each index ETF (SPY, QQQ, IWM)
    +1 price above its N-day average        -1 below
    +1 up >= up_day_pct today               -1 down <= down_day_pct today
  VIX
    +1 below vix_calm   -1 above vix_fear   -1 more if up >= vix_spike_pct today
  total >= supportive_at -> supportive, <= hostile_at -> hostile, else neutral

Data: Alpaca daily bars + snapshots for the ETFs (same account as the Scout);
VIX from Yahoo (Alpaca has no index data), best effort: without it the regime
uses the ETFs only and says so.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from loguru import logger

from tradingdesk.sessions import ET

Regime = Literal["supportive", "neutral", "hostile"]


@dataclass(frozen=True)
class IndexReading:
    symbol: str
    price: float
    prev_close: float
    sma: float | None

    @property
    def day_pct(self) -> float:
        return round((self.price - self.prev_close) / self.prev_close * 100, 2) if self.prev_close else 0.0


@dataclass(frozen=True)
class VixReading:
    level: float
    prev_close: float | None

    @property
    def day_pct(self) -> float | None:
        return round((self.level - self.prev_close) / self.prev_close * 100, 2) if self.prev_close else None


@dataclass
class RegimeResult:
    regime: Regime
    score: int
    detail: dict[str, Any] = field(default_factory=dict)


def compute_regime(indexes: list[IndexReading], vix: VixReading | None, cfg) -> RegimeResult:
    score, parts = 0, {}
    for r in indexes:
        s, why = 0, []
        if r.sma:
            s += 1 if r.price > r.sma else -1
            why.append(f"{'above' if r.price > r.sma else 'below'} {cfg.sma_days}d avg")
        if r.day_pct >= cfg.up_day_pct:
            s += 1
        elif r.day_pct <= cfg.down_day_pct:
            s -= 1
        why.append(f"{r.day_pct:+.2f}% today")
        score += s
        parts[r.symbol] = {"score": s, "day_pct": r.day_pct, "price": r.price, "sma": r.sma, "note": ", ".join(why)}
    if vix is not None:
        s = 1 if vix.level < cfg.vix_calm else (-1 if vix.level > cfg.vix_fear else 0)
        if vix.day_pct is not None and vix.day_pct >= cfg.vix_spike_pct:
            s -= 1
        score += s
        parts["VIX"] = {"score": s, "level": round(vix.level, 2), "day_pct": vix.day_pct}
    else:
        parts["VIX"] = {"score": 0, "note": "unavailable"}
    regime: Regime = "supportive" if score >= cfg.supportive_at else ("hostile" if score <= cfg.hostile_at else "neutral")
    return RegimeResult(regime, score, {"score": score, "parts": parts})


def summary(r: RegimeResult) -> str:
    p = r.detail.get("parts", {})
    bits = [f"{k} {v['day_pct']:+.1f}%" for k, v in p.items() if k != "VIX" and v.get("day_pct") is not None]
    vix = p.get("VIX", {})
    bits.append(f"VIX {vix['level']:.1f}" if "level" in vix else "VIX n/a")
    return ", ".join(bits)


class RegimeData(Protocol):
    def indexes(self, symbols: list[str], sma_days: int) -> list[IndexReading]: ...
    def vix(self, symbol: str) -> VixReading | None: ...


class LiveRegimeData:
    """Alpaca for the ETFs, Yahoo for the VIX."""

    def indexes(self, symbols: list[str], sma_days: int) -> list[IndexReading]:
        from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
        from alpaca.data.timeframe import TimeFrame

        from tradingdesk.legacy.wtbot import alpaca_client as ac

        client = ac._get_data_client()
        today = datetime.now(ET).date()
        bars = ac._backoff_retry(client.get_stock_bars, StockBarsRequest(
            symbol_or_symbols=symbols, timeframe=TimeFrame.Day,
            start=datetime.now(ET) - timedelta(days=sma_days * 2 + 10))).data
        snaps = ac._backoff_retry(client.get_stock_snapshot, StockSnapshotRequest(symbol_or_symbols=symbols))
        out = []
        for sym in symbols:
            hist = [b for b in bars.get(sym, []) if b.timestamp.astimezone(ET).date() < today]
            snap = snaps.get(sym)
            if not hist or snap is None or snap.latest_trade is None:
                logger.warning("regime: no data for {}", sym)
                continue
            closes = [float(b.close) for b in hist][-sma_days:]
            out.append(IndexReading(sym, float(snap.latest_trade.price), float(hist[-1].close),
                                    round(sum(closes) / len(closes), 2) if len(closes) >= sma_days // 2 else None))
        return out

    def vix(self, symbol: str) -> VixReading | None:
        try:
            import yfinance as yf

            fi = yf.Ticker(symbol).fast_info
            level, prev = fi.get("lastPrice"), fi.get("previousClose")
            return VixReading(float(level), float(prev) if prev else None) if level else None
        except Exception as exc:
            logger.debug("regime: VIX unavailable: {}", exc)
            return None
