"""
Broker interface. The Executor, Trade Manager, Risk Officer and Trade Desk talk
to this, never to a broker SDK. Every implementation must pass the contract
tests in tests/broker_contract/.

Primitives only. Multi-step procedures (closing a position with marketable
limits and retries, flatten-all, the kill switch) live in
tradingdesk/execution/closer.py and work the same for any broker.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from tradingdesk.market import Quote

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class BrokerCapabilities:
    paper: bool                      # orders go to a paper account
    dry_run: bool                    # orders are simulated, never sent
    native_bracket: bool             # entry + stop + target held by the broker
    native_oco: bool                 # stop + target pair on an existing position
    extended_hours: bool             # can trade premarket at all
    extended_hours_stops: bool       # broker holds stops in extended hours (Alpaca: no)
    short_selling: bool
    fractional: bool
    streaming_fills: bool


class OrderStatus(StrEnum):
    NEW = "new"                      # accepted / pending / held: still working
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    REPLACED = "replaced"

    @property
    def is_open(self) -> bool:
        return self in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)


@dataclass(frozen=True)
class AccountInfo:
    cash: float
    buying_power: float
    equity: float
    last_equity: float
    trading_blocked: bool = False


@dataclass(frozen=True)
class Position:
    ticker: str
    qty: int
    avg_entry: float
    market_price: float
    unrealized_pl: float


@dataclass(frozen=True)
class OrderInfo:
    id: str
    client_order_id: str
    ticker: str
    side: Side
    qty: int
    type: str                        # market | limit | stop | stop_limit
    status: OrderStatus
    filled_qty: int = 0
    filled_avg_price: float | None = None
    limit_price: float | None = None
    stop_price: float | None = None
    order_class: str = "simple"      # simple | bracket | oco | oto
    extended_hours: bool = False
    parent_id: str | None = None
    legs: tuple["OrderInfo", ...] = ()
    submitted_at: datetime | None = None
    reject_reason: str | None = None


@dataclass(frozen=True)
class MarketClock:
    is_open: bool                    # regular session
    next_open: datetime
    next_close: datetime


@dataclass(frozen=True)
class EntryRequest:
    """A long entry. In regular hours with native brackets: one bracket order.
    In extended hours: a plain limit buy (stop/target are managed in software)."""

    ticker: str
    qty: int
    client_order_id: str
    limit_price: float                       # worst price we'll pay
    stop_price: float | None = None          # trigger for a stop-limit entry (regular hours only)
    stop_loss: float | None = None           # bracket stop (regular hours only)
    take_profit: float | None = None         # bracket target (regular hours only)
    extended_hours: bool = False


class BrokerError(RuntimeError):
    """An order or account call the broker refused or that failed in transport."""


class Broker(ABC):
    name: str = "broker"

    @abstractmethod
    def capabilities(self) -> BrokerCapabilities: ...

    @abstractmethod
    def account(self) -> AccountInfo: ...

    @abstractmethod
    def positions(self) -> list[Position]: ...

    def position(self, ticker: str) -> Position | None:
        return next((p for p in self.positions() if p.ticker == ticker), None)

    @abstractmethod
    def open_orders(self, ticker: str | None = None) -> list[OrderInfo]: ...

    @abstractmethod
    def get_order(self, order_id: str) -> OrderInfo: ...

    @abstractmethod
    def get_order_by_client_id(self, client_order_id: str) -> OrderInfo | None: ...

    @abstractmethod
    def submit_entry(self, req: EntryRequest) -> OrderInfo: ...

    @abstractmethod
    def submit_exit_oco(self, ticker: str, qty: int, stop: float, target: float, client_order_id: str) -> OrderInfo:
        """Protective stop + target for an existing long position (regular hours)."""

    @abstractmethod
    def submit_limit(self, ticker: str, side: Side, qty: int, limit_price: float, client_order_id: str,
                     extended_hours: bool = False) -> OrderInfo: ...

    @abstractmethod
    def submit_market(self, ticker: str, side: Side, qty: int, client_order_id: str) -> OrderInfo: ...

    @abstractmethod
    def replace_order(self, order_id: str, *, stop_price: float | None = None,
                      limit_price: float | None = None, qty: int | None = None) -> OrderInfo: ...

    @abstractmethod
    def cancel(self, order_id: str) -> None: ...

    def cancel_all(self, ticker: str | None = None) -> list[str]:
        ids = [o.id for o in self.open_orders(ticker)]
        for oid in ids:
            try:
                self.cancel(oid)
            except BrokerError:
                pass
        return ids

    @abstractmethod
    def clock(self) -> MarketClock: ...

    @abstractmethod
    def quote(self, ticker: str) -> Quote | None: ...

    @abstractmethod
    def is_halted(self, ticker: str) -> bool: ...

