"""In-memory state of one approved trade while it's being worked and managed."""
from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from tradingdesk.models import Candidate


class TradeState(StrEnum):
    ARMED = "armed"                  # premarket: waiting for price to cross the entry (software trigger)
    WORKING = "working"              # entry order(s) at the broker
    OPEN = "open"                    # holding shares
    CLOSING = "closing"              # Closer is selling
    STUCK_HALTED = "stuck_halted"    # can't close: halted; retried until it lifts
    CLOSED = "closed"
    EXPIRED = "expired"              # never filled


@dataclass
class Leg:
    name: str                        # "a" (partial at target 1) / "b" (runner)
    qty: int
    target: float
    entry_id: str | None = None      # regular-hours bracket parent
    stop_id: str | None = None       # broker-held stop (bracket leg or OCO)
    target_id: str | None = None     # broker-held target
    software: bool = False           # stop/target watched by the Trade Manager
    target_done: bool = False


@dataclass
class ManagedTrade:
    candidate: Candidate
    entry: float
    stop: float
    risk_per_share: float
    legs: list[Leg]
    state: TradeState
    session_entry: str               # premarket | open (how the entry was placed)
    software_entry_id: str | None = None
    entry_submitted_at: datetime | None = None
    entry_attempts: int = 0
    breakeven_done: bool = False
    tightened: bool = False
    high: float = 0.0
    seen: dict[str, tuple[int, float]] = field(default_factory=dict)   # order id -> (filled qty, avg)
    roles: dict[str, str] = field(default_factory=dict)                # order id -> entry/stop/target/close:*
    done_orders: set[str] = field(default_factory=set)
    close_future: Future | None = None
    close_reason: str | None = None
    last_close_try: datetime | None = None
    stuck_alerted: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def ticker(self) -> str:
        return self.candidate.ticker

    @property
    def held(self) -> int:
        return self.candidate.position_qty

    @property
    def active(self) -> bool:
        return self.state not in (TradeState.CLOSED, TradeState.EXPIRED)

    @property
    def software_managed(self) -> bool:
        return any(leg.software for leg in self.legs)
