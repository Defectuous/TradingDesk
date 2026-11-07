"""
AlpacaBroker — the only Broker implementation for now. Paper account only.

Alpaca order rules this relies on (docs.alpaca.markets "Placing Orders", checked 2026-11-07):
- Extended hours: only limit orders, time_in_force day or gtc, extended_hours=true.
  Bracket/OCO/stop orders are not accepted, so premarket stops are software-managed.
- Bracket: time_in_force day or gtc, extended_hours false. Child legs activate after
  the entry is completely filled. limit_price/stop_price can be replaced (PATCH).
- OCO: exit-only, can be added to an existing position; replaceable.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any, TypeVar

from loguru import logger

from tradingdesk.broker.base import (
    AccountInfo,
    Broker,
    BrokerCapabilities,
    BrokerError,
    EntryRequest,
    MarketClock,
    OrderInfo,
    OrderStatus,
    Position,
    Side,
)
from tradingdesk.market import Quote
from tradingdesk.sessions import ET, now_et

T = TypeVar("T")

_STATUS = {
    "filled": OrderStatus.FILLED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "canceled": OrderStatus.CANCELED,
    "pending_cancel": OrderStatus.NEW,
    "expired": OrderStatus.EXPIRED,
    "done_for_day": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
    "suspended": OrderStatus.REJECTED,
    "stopped": OrderStatus.FILLED,
    "replaced": OrderStatus.REPLACED,
}


def _px(x: float) -> float:
    return round(x, 2) if x >= 1 else round(x, 4)


def _f(v: Any) -> float | None:
    return float(v) if v not in (None, "") else None


class AlpacaBroker(Broker):
    name = "alpaca"

    def __init__(self, api_key: str, secret_key: str, max_retries: int = 3, stale_trade_sec: float = 120) -> None:
        from alpaca.trading.client import TradingClient

        # paper=True is hard-coded: the paper-only startup check is not the only guard.
        self._tc = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)
        self._key, self._secret = api_key, secret_key
        self._max_retries = max_retries
        self._stale_trade_sec = stale_trade_sec
        self._data = None

    # -- plumbing ------------------------------------------------------------------
    def _call(self, fn: Callable[..., T], *args: Any, retry: bool = True, **kw: Any) -> T:
        """Capped retries with backoff on rate limits / 5xx / transport errors only.
        Order submissions pass retry=False: a resend could double an order (client_order_id
        makes that impossible at Alpaca anyway, but we don't lean on it)."""
        from alpaca.common.exceptions import APIError

        delay = 0.5
        for attempt in range(self._max_retries if retry else 1):
            try:
                return fn(*args, **kw)
            except APIError as exc:
                code = getattr(exc, "status_code", None)
                if retry and code in (429, 500, 502, 503, 504) and attempt < self._max_retries - 1:
                    logger.warning("alpaca {} -> HTTP {}, retry in {:.1f}s", fn.__name__, code, delay)
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise BrokerError(f"alpaca {fn.__name__}: {exc}") from exc
            except (ConnectionError, TimeoutError, OSError) as exc:
                if retry and attempt < self._max_retries - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise BrokerError(f"alpaca {fn.__name__}: {exc}") from exc
        raise BrokerError(f"alpaca {fn.__name__}: retries exhausted")

    @staticmethod
    def _order(o: Any) -> OrderInfo:
        raw_status = str(getattr(o.status, "value", o.status))
        return OrderInfo(
            id=str(o.id),
            client_order_id=str(o.client_order_id or ""),
            ticker=o.symbol,
            side="buy" if str(getattr(o.side, "value", o.side)) == "buy" else "sell",
            qty=int(float(o.qty or 0)),
            type=str(getattr(o.order_type or o.type, "value", o.order_type or o.type)),
            status=_STATUS.get(raw_status, OrderStatus.NEW),
            filled_qty=int(float(o.filled_qty or 0)),
            filled_avg_price=_f(o.filled_avg_price),
            limit_price=_f(o.limit_price),
            stop_price=_f(o.stop_price),
            order_class=str(getattr(o.order_class, "value", o.order_class) or "simple"),
            extended_hours=bool(o.extended_hours),
            legs=tuple(AlpacaBroker._order(leg) for leg in (o.legs or [])),
            submitted_at=o.submitted_at.astimezone(ET) if o.submitted_at else None,
        )

    # -- Broker ------------------------------------------------------------------------
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            paper=True, dry_run=False, native_bracket=True, native_oco=True, extended_hours=True,
            extended_hours_stops=False, short_selling=True, fractional=True, streaming_fills=True,
        )

    def account(self) -> AccountInfo:
        a = self._call(self._tc.get_account)
        return AccountInfo(cash=float(a.cash), buying_power=float(a.buying_power), equity=float(a.equity),
                           last_equity=float(a.last_equity), trading_blocked=bool(a.trading_blocked))

    def positions(self) -> list[Position]:
        return [
            Position(ticker=p.symbol, qty=int(float(p.qty)), avg_entry=float(p.avg_entry_price),
                     market_price=float(p.current_price or p.avg_entry_price), unrealized_pl=float(p.unrealized_pl or 0))
            for p in self._call(self._tc.get_all_positions)
        ]

    def open_orders(self, ticker: str | None = None) -> list[OrderInfo]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=False, limit=500,
                               symbols=[ticker] if ticker else None)
        return [self._order(o) for o in self._call(self._tc.get_orders, req)]

    def get_order(self, order_id: str) -> OrderInfo:
        from alpaca.trading.requests import GetOrderByIdRequest

        return self._order(self._call(self._tc.get_order_by_id, order_id, GetOrderByIdRequest(nested=True)))

    def get_order_by_client_id(self, client_order_id: str) -> OrderInfo | None:
        try:
            return self._order(self._call(self._tc.get_order_by_client_id, client_order_id, retry=False))
        except BrokerError:
            return None

    def _submit(self, req: Any) -> OrderInfo:
        existing = self.get_order_by_client_id(req.client_order_id)
        if existing is not None:   # idempotent: never submit the same client order id twice
            logger.warning("alpaca: order {} already exists, not resubmitting", req.client_order_id)
            return existing
        return self._order(self._call(self._tc.submit_order, req, retry=False))

    def submit_entry(self, req: EntryRequest) -> OrderInfo:
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, StopLimitOrderRequest, StopLossRequest, TakeProfitRequest

        common = dict(symbol=req.ticker, qty=req.qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                      client_order_id=req.client_order_id)
        if req.extended_hours:
            if req.stop_price or req.stop_loss or req.take_profit:
                raise BrokerError("extended-hours entries must be plain limit orders (no stop, bracket)")
            return self._submit(LimitOrderRequest(**common, limit_price=_px(req.limit_price), extended_hours=True))
        bracket: dict[str, Any] = {}
        if req.stop_loss and req.take_profit:
            bracket = dict(order_class=OrderClass.BRACKET, stop_loss=StopLossRequest(stop_price=_px(req.stop_loss)),
                           take_profit=TakeProfitRequest(limit_price=_px(req.take_profit)))
        if req.stop_price:
            return self._submit(StopLimitOrderRequest(**common, stop_price=_px(req.stop_price),
                                                      limit_price=_px(req.limit_price), **bracket))
        return self._submit(LimitOrderRequest(**common, limit_price=_px(req.limit_price), **bracket))

    def submit_exit_oco(self, ticker: str, qty: int, stop: float, target: float, client_order_id: str) -> OrderInfo:
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, StopLossRequest, TakeProfitRequest

        return self._submit(LimitOrderRequest(
            symbol=ticker, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id, limit_price=_px(target), order_class=OrderClass.OCO,
            take_profit=TakeProfitRequest(limit_price=_px(target)), stop_loss=StopLossRequest(stop_price=_px(stop)),
        ))

    def submit_limit(self, ticker: str, side: Side, qty: int, limit_price: float, client_order_id: str,
                     extended_hours: bool = False) -> OrderInfo:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest

        return self._submit(LimitOrderRequest(
            symbol=ticker, qty=qty, side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY, limit_price=_px(limit_price), extended_hours=extended_hours,
            client_order_id=client_order_id,
        ))

    def submit_market(self, ticker: str, side: Side, qty: int, client_order_id: str) -> OrderInfo:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        return self._submit(MarketOrderRequest(
            symbol=ticker, qty=qty, side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY, client_order_id=client_order_id,
        ))

    def replace_order(self, order_id: str, *, stop_price: float | None = None,
                      limit_price: float | None = None, qty: int | None = None) -> OrderInfo:
        from alpaca.trading.requests import ReplaceOrderRequest

        req = ReplaceOrderRequest(qty=qty, stop_price=_px(stop_price) if stop_price else None,
                                  limit_price=_px(limit_price) if limit_price else None)
        return self._order(self._call(self._tc.replace_order_by_id, order_id, req, retry=False))

    def cancel(self, order_id: str) -> None:
        self._call(self._tc.cancel_order_by_id, order_id)

    def clock(self) -> MarketClock:
        c = self._call(self._tc.get_clock)
        return MarketClock(is_open=bool(c.is_open), next_open=c.next_open.astimezone(ET),
                           next_close=c.next_close.astimezone(ET))

    def _data_client(self) -> Any:
        if self._data is None:
            from alpaca.data.historical import StockHistoricalDataClient

            self._data = StockHistoricalDataClient(api_key=self._key, secret_key=self._secret)
        return self._data

    def _snapshot(self, ticker: str) -> Any:
        from alpaca.data.requests import StockSnapshotRequest

        return self._call(self._data_client().get_stock_snapshot,
                          StockSnapshotRequest(symbol_or_symbols=[ticker])).get(ticker)

    def quote(self, ticker: str) -> Quote | None:
        snap = self._snapshot(ticker)
        if snap is None:
            return None
        q = snap.latest_quote
        return Quote(price=float(snap.latest_trade.price), bid=float(q.bid_price or 0) if q else 0.0,
                     ask=float(q.ask_price or 0) if q else 0.0)

    def is_halted(self, ticker: str) -> bool:
        """Heuristic (Alpaca's trading API has no halt flag): asset not tradable, or no trade
        for `stale_trade_sec` while the regular session is open. Phase 3b may switch to the
        data stream's trading-status messages."""
        try:
            asset = self._call(self._tc.get_asset, ticker)
            if not asset.tradable or str(getattr(asset.status, "value", asset.status)) != "active":
                return True
            if not self.clock().is_open:
                return False
            snap = self._snapshot(ticker)
            last = snap.latest_trade.timestamp if snap and snap.latest_trade else None
            return last is None or now_et() - last.astimezone(ET) > timedelta(seconds=self._stale_trade_sec)
        except BrokerError:
            return False

