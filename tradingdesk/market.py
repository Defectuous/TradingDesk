"""
Market data used by the Scout, Manager and Risk Officer.

`MarketData` is the interface; agents never touch a data SDK directly, so tests
and the replay mode (phase 7) can swap in recorded data. `WtbotMarketData` is the
live implementation over the copied WTBot code (tradingdesk/legacy/wtbot).

Dicts returned by snapshot()/premarket_levels()/session_levels() keep WTBot's
shapes so its pillar logic can consume them unchanged.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Protocol

from loguru import logger

from tradingdesk.sessions import ET, now_et


@dataclass(frozen=True)
class Quote:
    price: float
    bid: float = 0.0
    ask: float = 0.0

    @property
    def spread_pct(self) -> float | None:
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            return None
        mid = (self.bid + self.ask) / 2
        return round((self.ask - self.bid) / mid * 100, 3)


@dataclass(frozen=True)
class Mover:
    ticker: str
    price: float
    change_pct: float


class MarketData(Protocol):
    def universe(self) -> list[str]: ...
    def movers(self, universe: list[str], min_change_pct: float, limit: int) -> list[Mover]: ...
    def snapshot(self, ticker: str) -> dict[str, Any] | None: ...
    def float_shares(self, ticker: str) -> int | None: ...
    def premarket_levels(self, ticker: str) -> dict[str, Any] | None: ...
    def session_levels(self, ticker: str) -> dict[str, Any] | None: ...
    def quote(self, ticker: str) -> Quote | None: ...


class WtbotMarketData:
    """Live Alpaca/Yahoo data through WTBot's own client code."""

    def __init__(self, max_symbols: int | None = None) -> None:
        # Imported lazily so tests never need alpaca-py credentials.
        from tradingdesk.legacy.wtbot import alpaca_client, technical_levels, yahoo_news

        self._ac = alpaca_client
        self._tl = technical_levels
        self._yn = yahoo_news
        self._max_symbols = max_symbols

    def universe(self) -> list[str]:
        return asyncio.run(self._ac.get_universe_async(max_symbols=self._max_symbols))

    def movers(self, universe: list[str], min_change_pct: float, limit: int) -> list[Mover]:
        """One batched snapshot request per 1,000 symbols; % change vs the prior close.
        Same prior-close rule as WTBot's get_snapshot (premarket: yesterday's daily bar)."""
        from alpaca.data.requests import StockSnapshotRequest

        client = self._ac._get_data_client()
        today = now_et().date()
        out: list[Mover] = []
        for i in range(0, len(universe), 1000):
            batch = universe[i : i + 1000]
            try:
                snaps = self._ac._backoff_retry(
                    client.get_stock_snapshot, StockSnapshotRequest(symbol_or_symbols=batch)
                )
            except Exception as exc:  # one bad batch must not stop the scan
                logger.warning("movers: snapshot batch {} failed: {}", i, exc)
                continue
            for sym, snap in snaps.items():
                try:
                    price = float(snap.latest_trade.price)
                    daily = snap.daily_bar
                    if daily is not None and daily.timestamp.astimezone(ET).date() == today:
                        prev = float(snap.previous_daily_bar.close)
                    elif daily is not None:
                        prev = float(daily.close)
                    else:
                        continue
                    if prev > 0:
                        chg = (price - prev) / prev * 100
                        if chg >= min_change_pct:
                            out.append(Mover(sym, price, round(chg, 2)))
                except Exception:
                    continue
        out.sort(key=lambda m: m.change_pct, reverse=True)
        return out[:limit]

    def snapshot(self, ticker: str) -> dict[str, Any] | None:
        return self._ac.get_snapshot(ticker)

    def float_shares(self, ticker: str) -> int | None:
        return self._yn.get_float(ticker)

    def premarket_levels(self, ticker: str) -> dict[str, Any] | None:
        return self._tl.get_premarket_levels(ticker)

    def session_levels(self, ticker: str) -> dict[str, Any] | None:
        return self._tl.get_session_levels(ticker)

    def quote(self, ticker: str) -> Quote | None:
        q = self._ac.get_latest_quotes([ticker]).get(ticker)
        return Quote(q["price"], q["bid"], q["ask"]) if q else None


class UniverseCache:
    """The price-filtered universe is expensive; rebuild it every `refresh_min` minutes."""

    def __init__(self, market: MarketData, refresh_min: float) -> None:
        self._market = market
        self._refresh_sec = refresh_min * 60
        self._symbols: list[str] = []
        self._built_at: float = 0.0

    def get(self) -> list[str]:
        if not self._symbols or time.monotonic() - self._built_at >= self._refresh_sec:
            try:
                fresh = self._market.universe()
            except Exception as exc:
                logger.error("universe refresh failed: {}", exc)
                fresh = []
            if fresh:
                self._symbols, self._built_at = fresh, time.monotonic()
                logger.info("universe: {} symbols", len(fresh))
        return self._symbols

