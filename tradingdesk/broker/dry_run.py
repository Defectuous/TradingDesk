"""
DryRunBroker — logs orders and simulates fills from quotes, never sends anything.

Used by tests, the broker contract suite, `broker.dry_run: true`, and replay mode
(phase 7). It enforces the same order rules as Alpaca so code that works here
behaves the same on paper:
- outside regular hours only plain limit orders with extended_hours=True are worked;
  brackets/OCO/stop orders are rejected if marked extended_hours, and other orders
  wait for the open
- bracket legs activate only after the entry is completely filled; legs are OCO
- client_order_id is unique (resubmitting returns the existing order)

Fills happen in `match()`, which every read call runs first. Tests drive prices
with `set_quote()` and `set_regular_hours()`.
"""
from __future__ import annotations

import itertools
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

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
from tradingdesk.sessions import now_et


@dataclass
class _Order:
    info: OrderInfo
    active: bool = True                   # bracket legs start inactive
    triggered: bool = False               # stop / stop-limit triggered
    oco_with: str | None = None
    children: list[str] = field(default_factory=list)


class DryRunBroker(Broker):
    name = "dry_run"

    def __init__(self, cash: float = 1000.0, clock: Callable[[], datetime] = now_et,
                 regular_hours: bool = True, fill_ratio: float = 1.0,
                 quote_source: Callable[[str], Quote | None] | None = None,
                 regular_hours_source: Callable[[], bool] | None = None) -> None:
        self._lock = threading.RLock()
        self._ids = itertools.count(1)
        self._orders: dict[str, _Order] = {}
        self._by_client: dict[str, str] = {}
        self._quotes: dict[str, Quote] = {}
        self._halted: set[str] = set()
        self._pos: dict[str, tuple[int, float]] = {}      # ticker -> (qty, avg)
        self._cash = cash
        self._start_equity = cash
        self._now = clock
        self._regular = regular_hours
        self.fill_ratio = fill_ratio                       # <1.0 simulates partial fills
        self.log: list[str] = []
        # live dry-run: pull quotes for tickers we're working, at most once a second
        self._quote_source = quote_source
        self._regular_source = regular_hours_source
        self._quoted_at: dict[str, float] = {}

    # -- test controls -------------------------------------------------------------
    def set_quote(self, ticker: str, price: float, bid: float | None = None, ask: float | None = None) -> None:
        with self._lock:
            self._quotes[ticker] = Quote(price, bid if bid is not None else round(price - 0.01, 4),
                                         ask if ask is not None else round(price + 0.01, 4))

    def set_regular_hours(self, value: bool) -> None:
        self._regular = value

    def set_halted(self, ticker: str, halted: bool = True) -> None:
        (self._halted.add if halted else self._halted.discard)(ticker)

    # -- Broker --------------------------------------------------------------------
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            paper=False, dry_run=True, native_bracket=True, native_oco=True, extended_hours=True,
            extended_hours_stops=False, short_selling=False, fractional=False, streaming_fills=False,
        )

    def account(self) -> AccountInfo:
        with self._lock:
            self.match()
            mv = sum(q * self._mark(t) for t, (q, _) in self._pos.items())
            return AccountInfo(cash=round(self._cash, 2), buying_power=round(self._cash, 2),
                               equity=round(self._cash + mv, 2), last_equity=self._start_equity)

    def positions(self) -> list[Position]:
        with self._lock:
            self.match()
            return [Position(t, q, round(avg, 4), self._mark(t), round(q * (self._mark(t) - avg), 2))
                    for t, (q, avg) in self._pos.items() if q]

    def open_orders(self, ticker: str | None = None) -> list[OrderInfo]:
        with self._lock:
            self.match()
            return [o.info for o in self._orders.values()
                    if o.info.status.is_open and (ticker is None or o.info.ticker == ticker)]

    def get_order(self, order_id: str) -> OrderInfo:
        with self._lock:
            self.match()
            if order_id not in self._orders:
                raise BrokerError(f"no order {order_id}")
            return self._with_legs(self._orders[order_id])

    def get_order_by_client_id(self, client_order_id: str) -> OrderInfo | None:
        with self._lock:
            oid = self._by_client.get(client_order_id)
            return self.get_order(oid) if oid else None

    def submit_entry(self, req: EntryRequest) -> OrderInfo:
        if req.extended_hours and (req.stop_price or req.stop_loss or req.take_profit):
            raise BrokerError("extended-hours entries must be plain limit orders (no stop, bracket)")
        bracket = bool(req.stop_loss and req.take_profit)
        typ = "stop_limit" if req.stop_price else "limit"
        with self._lock:
            if req.client_order_id in self._by_client:
                return self.get_order(self._by_client[req.client_order_id])
            parent = self._new(req.ticker, "buy", req.qty, typ, req.client_order_id, limit=req.limit_price,
                               stop=req.stop_price, order_class="bracket" if bracket else "simple",
                               extended=req.extended_hours)
            if bracket:
                tp = self._new(req.ticker, "sell", req.qty, "limit", f"{req.client_order_id}-tp",
                               limit=req.take_profit, parent=parent.info.id, active=False, order_class="bracket")
                sl = self._new(req.ticker, "sell", req.qty, "stop", f"{req.client_order_id}-sl",
                               stop=req.stop_loss, parent=parent.info.id, active=False, order_class="bracket")
                tp.oco_with, sl.oco_with = sl.info.id, tp.info.id
                parent.children = [tp.info.id, sl.info.id]
            self.match()
            return self._with_legs(parent)

    def submit_exit_oco(self, ticker: str, qty: int, stop: float, target: float, client_order_id: str) -> OrderInfo:
        with self._lock:
            if client_order_id in self._by_client:
                return self.get_order(self._by_client[client_order_id])
            if not self._regular:
                raise BrokerError("OCO orders are not accepted in extended hours")
            tp = self._new(ticker, "sell", qty, "limit", client_order_id, limit=target, order_class="oco")
            sl = self._new(ticker, "sell", qty, "stop", f"{client_order_id}-sl", stop=stop,
                           order_class="oco", parent=tp.info.id)
            tp.oco_with, sl.oco_with = sl.info.id, tp.info.id
            tp.children = [sl.info.id]
            self.match()
            return self._with_legs(tp)

    def submit_limit(self, ticker: str, side: Side, qty: int, limit_price: float, client_order_id: str,
                     extended_hours: bool = False) -> OrderInfo:
        with self._lock:
            if client_order_id in self._by_client:
                return self.get_order(self._by_client[client_order_id])
            o = self._new(ticker, side, qty, "limit", client_order_id, limit=limit_price, extended=extended_hours)
            self.match()
            return self._orders[o.info.id].info

    def submit_market(self, ticker: str, side: Side, qty: int, client_order_id: str) -> OrderInfo:
        with self._lock:
            if client_order_id in self._by_client:
                return self.get_order(self._by_client[client_order_id])
            if not self._regular:
                raise BrokerError("market orders are not accepted in extended hours")
            o = self._new(ticker, side, qty, "market", client_order_id)
            self.match()
            return self._orders[o.info.id].info

    def replace_order(self, order_id: str, *, stop_price: float | None = None,
                      limit_price: float | None = None, qty: int | None = None) -> OrderInfo:
        with self._lock:
            o = self._orders.get(order_id)
            if o is None or not o.info.status.is_open:
                raise BrokerError(f"order {order_id} is not open")
            o.info = replace(o.info, stop_price=stop_price or o.info.stop_price,
                             limit_price=limit_price or o.info.limit_price, qty=qty or o.info.qty)
            self.log.append(f"replace {order_id} stop={o.info.stop_price} limit={o.info.limit_price}")
            self.match()
            return o.info

    def cancel(self, order_id: str) -> None:
        with self._lock:
            o = self._orders.get(order_id)
            if o is None:
                raise BrokerError(f"no order {order_id}")
            if o.info.status.is_open:
                self._set(o, status=OrderStatus.CANCELED)
                partner = [o.oco_with] if o.oco_with else []
                for cid in o.children + partner:
                    child = self._orders[cid]
                    if child.info.status.is_open:
                        self._set(child, status=OrderStatus.CANCELED)
            self.log.append(f"cancel {order_id}")

    def clock(self) -> MarketClock:
        now = self._now()
        return MarketClock(is_open=self._regular, next_open=now + timedelta(hours=1), next_close=now + timedelta(hours=6))

    def quote(self, ticker: str) -> Quote | None:
        return self._quotes.get(ticker)

    def is_halted(self, ticker: str) -> bool:
        return ticker in self._halted

    # -- simulation --------------------------------------------------------------------
    def _new(self, ticker: str, side: Side, qty: int, typ: str, client_id: str, *, limit: float | None = None,
             stop: float | None = None, order_class: str = "simple", extended: bool = False,
             parent: str | None = None, active: bool = True) -> _Order:
        if qty < 1:
            raise BrokerError("qty must be >= 1")
        oid = f"dry-{next(self._ids)}"
        info = OrderInfo(id=oid, client_order_id=client_id, ticker=ticker, side=side, qty=qty, type=typ,
                         status=OrderStatus.NEW, limit_price=limit, stop_price=stop, order_class=order_class,
                         extended_hours=extended, parent_id=parent, submitted_at=self._now())
        if extended and (typ != "limit" or order_class != "simple"):
            info = replace(info, status=OrderStatus.REJECTED, reject_reason="extended hours: limit orders only")
        if side == "sell" and parent is None and order_class == "simple":
            held = self._pos.get(ticker, (0, 0.0))[0]
            working = sum(o.info.qty - o.info.filled_qty for o in self._orders.values()
                          if o.info.ticker == ticker and o.info.side == "sell" and o.info.status.is_open and o.active)
            if qty > held - working:
                info = replace(info, status=OrderStatus.REJECTED, reject_reason="insufficient qty (no shorting)")
        o = _Order(info, active=active)
        self._orders[oid] = o
        self._by_client[client_id] = oid
        self.log.append(f"submit {side} {qty} {ticker} {typ} limit={limit} stop={stop} class={order_class} "
                        f"ext={extended} -> {info.status}")
        logger.info("[dry-run] {}", self.log[-1])
        return o

    def _with_legs(self, o: _Order) -> OrderInfo:
        return replace(o.info, legs=tuple(self._orders[c].info for c in o.children))

    def _mark(self, ticker: str) -> float:
        q = self._quotes.get(ticker)
        return q.price if q else self._pos.get(ticker, (0, 0.0))[1]

    def _set(self, o: _Order, **kw) -> None:
        o.info = replace(o.info, **kw)

    def _refresh_live(self) -> None:
        import time as _t

        if self._regular_source is not None:
            self._regular = self._regular_source()
        if self._quote_source is None:
            return
        tickers = {o.info.ticker for o in self._orders.values() if o.info.status.is_open} |                   {t for t, (q, _) in self._pos.items() if q}
        for t in tickers:
            if _t.monotonic() - self._quoted_at.get(t, 0) >= 1.0:
                try:
                    q = self._quote_source(t)
                except Exception as exc:
                    logger.debug("[dry-run] quote {} failed: {}", t, exc)
                    q = None
                if q:
                    self._quotes[t] = q
                self._quoted_at[t] = _t.monotonic()

    def match(self) -> None:
        with self._lock:
            self._refresh_live()
            for o in list(self._orders.values()):
                info = o.info
                if not (o.active and info.status.is_open) or info.ticker in self._halted:
                    continue
                q = self._quotes.get(info.ticker)
                if q is None:
                    continue
                if not self._regular and not info.extended_hours:
                    continue                                   # waits for the open, like Alpaca
                px = self._fill_price(o, q)
                if px is None:
                    continue
                remaining = info.qty - info.filled_qty
                qty = remaining if self.fill_ratio >= 1 else max(1, math.floor(remaining * self.fill_ratio))
                self._fill(o, qty, px)

    def _fill_price(self, o: _Order, q: Quote) -> float | None:
        i = o.info
        ask, bid = q.ask or q.price, q.bid or q.price
        if i.type == "market":
            return ask if i.side == "buy" else bid
        if i.type in ("stop", "stop_limit") and not o.triggered:
            hit = q.price >= i.stop_price if i.side == "buy" else bid <= i.stop_price
            if not hit:
                return None
            o.triggered = True
        if i.type == "stop":
            return ask if i.side == "buy" else bid
        if i.side == "buy":
            return ask if ask <= i.limit_price else None
        return bid if bid >= i.limit_price else None

    def _fill(self, o: _Order, qty: int, px: float) -> None:
        i = o.info
        total = i.filled_qty + qty
        avg = ((i.filled_avg_price or 0) * i.filled_qty + px * qty) / total
        status = OrderStatus.FILLED if total >= i.qty else OrderStatus.PARTIALLY_FILLED
        self._set(o, filled_qty=total, filled_avg_price=round(avg, 4), status=status)
        held, held_avg = self._pos.get(i.ticker, (0, 0.0))
        if i.side == "buy":
            self._cash -= qty * px
            self._pos[i.ticker] = (held + qty, (held * held_avg + qty * px) / (held + qty))
        else:
            self._cash += qty * px
            self._pos[i.ticker] = (held - qty, held_avg if held - qty else 0.0)
        self.log.append(f"fill {i.side} {qty} {i.ticker} @ {px} ({i.id} {status})")
        logger.info("[dry-run] {}", self.log[-1])
        if status is OrderStatus.FILLED:
            for cid in o.children:                         # bracket: legs live after a complete fill
                child = self._orders[cid]
                if i.order_class == "bracket":
                    child.active = True
                    self._set(child, qty=total)
            if o.oco_with:                                 # OCO: the other leg cancels
                other = self._orders[o.oco_with]
                if other.info.status.is_open:
                    self._set(other, status=OrderStatus.CANCELED)
