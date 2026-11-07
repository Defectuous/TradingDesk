"""
Closing a long position, the same way for every broker:

  1. cancel the position's attached stop/target (and any other open) orders first,
     and wait until the broker confirms they're gone, so nothing can fire after the
     close and leave the account short
  2. sell with a marketable limit priced just through the bid (not a plain market
     order: thin small caps fill badly)
  3. if unfilled, cancel and re-price every few seconds; after `max_limit_attempts`
     fall back to a market order (regular hours only; extended hours keep re-pricing)
  4. confirm with the broker that the position is flat
Partial fills are handled by always selling whatever the broker says is left.
A halted stock returns `stuck_halted` immediately; the caller retries later.
"""
from __future__ import annotations

import itertools
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from loguru import logger

from tradingdesk.broker.base import Broker, BrokerError
from tradingdesk.settings import WindDownConfig

RegisterFn = Callable[[str, str], None]   # (order_id, role) so fills get attributed


@dataclass
class CloseResult:
    ticker: str
    flat: bool
    stuck_halted: bool = False
    attempts: int = 0
    used_market: bool = False
    message: str = ""
    order_ids: list[str] = field(default_factory=list)


class Closer:
    _seq = itertools.count(1)

    def __init__(self, broker: Broker, cfg: WindDownConfig, sleep: Callable[[float], None] = time.sleep,
                 cancel_wait_sec: float = 5.0) -> None:
        self.broker = broker
        self.cfg = cfg
        self.sleep = sleep
        self.cancel_wait_sec = cancel_wait_sec

    def close(self, ticker: str, reason: str, *, client_prefix: str = "td-close",
              register: RegisterFn | None = None) -> CloseResult:
        res = CloseResult(ticker=ticker, flat=False)
        if self.broker.is_halted(ticker):
            res.stuck_halted, res.message = True, "halted"
            return res

        # 1. cancel everything working on this ticker and wait for confirmation
        self.broker.cancel_all(ticker)
        waited = 0.0
        while self.broker.open_orders(ticker) and waited < self.cancel_wait_sec:
            self.sleep(0.25)
            waited += 0.25
        if self.broker.open_orders(ticker):
            res.message = "open orders would not cancel; not selling (could leave the account short)"
            logger.error("close {}: {}", ticker, res.message)
            return res

        regular = self.broker.clock().is_open
        for attempt in itertools.count(1):
            pos = self.broker.position(ticker)
            if pos is None or pos.qty <= 0:
                res.flat = True
                break
            if self.broker.is_halted(ticker):
                res.stuck_halted, res.message = True, "halted during close"
                return res
            if attempt > self.cfg.max_limit_attempts + 5:
                res.message = f"still holding {pos.qty} after {attempt - 1} attempts"
                break
            res.attempts = attempt
            cid = f"{client_prefix}-{ticker}-{next(self._seq)}"
            try:
                if attempt > self.cfg.max_limit_attempts and regular:
                    o = self.broker.submit_market(ticker, "sell", pos.qty, cid)
                    res.used_market = True
                else:
                    q = self.broker.quote(ticker)
                    bid = (q.bid or q.price) if q else pos.market_price
                    # each retry goes a little deeper through the bid
                    offset = self.cfg.close_limit_offset_pct * min(attempt, self.cfg.max_limit_attempts)
                    limit = bid * (1 - offset / 100)
                    o = self.broker.submit_limit(ticker, "sell", pos.qty, limit, cid, extended_hours=not regular)
            except BrokerError as exc:
                logger.warning("close {} attempt {}: {}", ticker, attempt, exc)
                self.sleep(self.cfg.retry_interval_sec)
                continue
            res.order_ids.append(o.id)
            if register:
                register(o.id, f"close:{reason}")
            self.sleep(self.cfg.retry_interval_sec)
            o = self.broker.get_order(o.id)
            if o.status.is_open:
                try:
                    self.broker.cancel(o.id)
                except BrokerError:
                    pass

        # 4. confirm
        pos = self.broker.position(ticker)
        res.flat = pos is None or pos.qty <= 0
        res.message = res.message or ("flat" if res.flat else "not flat after close")
        logger.info("close {} ({}): {} after {} attempt(s){}", ticker, reason, res.message, res.attempts,
                    " incl. market order" if res.used_market else "")
        return res
