"""
Session clock — which part of the trading day it is, in US/Eastern.

Pure time logic. Market holidays and halts come from the broker at runtime
(phase 2/3); this module only knows weekdays and the configured windows.

Phases
  IDLE       before startup, and weekends
  STARTUP    startup .. premarket_start       (caching only, no tickets)
  PREMARKET  premarket_start .. open          (tickets allowed, stricter limits)
  OPEN       open .. new_trade_cutoff         (main window)
  WIND_DOWN  new_trade_cutoff .. wind_down    (no new trades; positions managed, then closed)
             (wind_down_mode=hold: until market_close)
  ENDED      after that
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo

from tradingdesk.settings import SessionsConfig

ET = ZoneInfo("America/New_York")

TradingSession = Literal["premarket", "open"]


def now_et() -> datetime:
    return datetime.now(ET)


def to_et(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime; pass a timezone-aware value")
    return dt.astimezone(ET)


class Phase(StrEnum):
    IDLE = "idle"
    STARTUP = "startup"
    PREMARKET = "premarket"
    OPEN = "open"
    WIND_DOWN = "wind_down"
    ENDED = "ended"

    @property
    def label(self) -> str:
        return {
            "idle": "Idle", "startup": "Startup", "premarket": "Premarket",
            "open": "Open", "wind_down": "Wind-down", "ended": "Ended",
        }[self.value]


class SessionClock:
    def __init__(self, cfg: SessionsConfig) -> None:
        self.cfg = cfg

    # -- boundaries -------------------------------------------------------
    @property
    def wind_down_end(self) -> time:
        return self.cfg.wind_down if self.cfg.wind_down_mode == "close" else self.cfg.market_close

    def _boundaries(self) -> list[tuple[time, Phase]]:
        c = self.cfg
        return [
            (c.startup, Phase.STARTUP),
            (c.premarket_start, Phase.PREMARKET),
            (c.open, Phase.OPEN),
            (c.new_trade_cutoff, Phase.WIND_DOWN),
            (self.wind_down_end, Phase.ENDED),
        ]

    @staticmethod
    def is_trading_weekday(d: date) -> bool:
        return d.weekday() < 5

    # -- queries ----------------------------------------------------------
    def phase_at(self, dt: datetime) -> Phase:
        dt = to_et(dt)
        if not self.is_trading_weekday(dt.date()):
            return Phase.IDLE
        t = dt.time()
        phase = Phase.IDLE
        for start, p in self._boundaries():
            if t >= start:
                phase = p
        return phase

    def trading_session_at(self, dt: datetime) -> TradingSession | None:
        """Which trading session's rules apply right now (None = no new trades)."""
        p = self.phase_at(dt)
        if p is Phase.PREMARKET:
            return "premarket"
        if p is Phase.OPEN:
            return "open"
        return None

    def accepting_new_trades(self, dt: datetime) -> bool:
        """True inside premarket/open, minus the optional first/last-N-minute blackouts."""
        session = self.trading_session_at(dt)
        if session is None:
            return False
        dt = to_et(dt)
        start = self.cfg.premarket_start if session == "premarket" else self.cfg.open
        session_start = datetime.combine(dt.date(), start, ET)
        if dt < session_start + timedelta(minutes=self.cfg.no_entries_first_minutes):
            return False
        cutoff = datetime.combine(dt.date(), self.cfg.new_trade_cutoff, ET)
        if dt >= cutoff - timedelta(minutes=self.cfg.no_entries_last_minutes):
            return False
        return True

    def next_change(self, dt: datetime) -> tuple[Phase, datetime]:
        """The next phase and when it starts (for the status-bar countdown)."""
        dt = to_et(dt)
        if self.is_trading_weekday(dt.date()):
            for start, p in self._boundaries():
                at = datetime.combine(dt.date(), start, ET)
                if at > dt:
                    return p, at
        d = dt.date() + timedelta(days=1)
        while not self.is_trading_weekday(d):
            d += timedelta(days=1)
        return Phase.STARTUP, datetime.combine(d, self.cfg.startup, ET)
