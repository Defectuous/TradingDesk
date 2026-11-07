"""In-memory MarketData for tests (WTBot dict shapes, no network)."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from tradingdesk.market import Mover, Quote


def bars(lows: list[float], highs: list[float] | None = None) -> list[SimpleNamespace]:
    highs = highs or [lo * 1.02 for lo in lows]
    return [SimpleNamespace(low=lo, high=hi, open=lo, close=hi, volume=10_000) for lo, hi in zip(lows, highs)]


def snapshot(ticker: str = "ABCD", price: float = 4.20, change: float = 32.0, rvol: float = 9.0,
             gap: float = 18.0, volume: int = 2_500_000) -> dict[str, Any]:
    prev = round(price / (1 + change / 100), 4)
    return {"symbol": ticker, "current_price": price, "prev_close": prev, "open_price": price,
            "daily_change_pct": change, "gap_pct": gap, "today_volume": volume,
            "avg_volume_30d": volume / rvol, "rvol": rvol}


@dataclass
class FakeMarket:
    snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    floats: dict[str, int | None] = field(default_factory=dict)
    pm: dict[str, dict[str, Any]] = field(default_factory=dict)
    sess: dict[str, dict[str, Any]] = field(default_factory=dict)
    quotes: dict[str, Quote] = field(default_factory=dict)
    snapshot_calls: list[str] = field(default_factory=list)

    def add(self, ticker: str = "ABCD", price: float = 4.20, float_shares: int | None = 6_000_000,
            pm_high: float = 4.24, lows: list[float] | None = None, hod: float | None = None,
            quote: Quote | None = None, **snap: Any) -> None:
        self.snapshots[ticker] = snapshot(ticker, price, **snap)
        self.floats[ticker] = float_shares
        self.pm[ticker] = {"pm_high": pm_high, "pm_low": 3.6, "pm_vwap": 3.95, "pm_volume": 900_000,
                           "bars": bars(lows or [4.10, 4.12, 4.15, 4.13, 4.16])}
        if hod is not None:
            self.sess[ticker] = {"hod": hod, "lod": 3.9, "vwap": 4.0,
                                 "bars": bars([round(hod * k, 2) for k in (0.97, 0.975, 0.98, 0.978, 0.982)])}
        self.quotes[ticker] = quote or Quote(price, round(price - 0.01, 2), round(price + 0.01, 2))

    def universe(self) -> list[str]:
        return list(self.snapshots)

    def movers(self, universe: list[str], min_change_pct: float, limit: int) -> list[Mover]:
        out = [Mover(t, s["current_price"], s["daily_change_pct"]) for t, s in self.snapshots.items()
               if t in universe and s["daily_change_pct"] >= min_change_pct]
        return sorted(out, key=lambda m: m.change_pct, reverse=True)[:limit]

    def snapshot(self, ticker: str) -> dict[str, Any] | None:
        self.snapshot_calls.append(ticker)
        return self.snapshots.get(ticker)

    def float_shares(self, ticker: str) -> int | None:
        return self.floats.get(ticker)

    def premarket_levels(self, ticker: str) -> dict[str, Any] | None:
        return self.pm.get(ticker)

    def session_levels(self, ticker: str) -> dict[str, Any] | None:
        return self.sess.get(ticker)

    def quote(self, ticker: str) -> Quote | None:
        return self.quotes.get(ticker)
