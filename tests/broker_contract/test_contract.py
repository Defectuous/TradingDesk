"""
Broker contract: every Broker implementation must pass these.

  pytest tests/broker_contract                 dry-run broker only (offline)
  pytest tests/broker_contract --paper         also the Alpaca PAPER account

On paper, every order is priced far from the market so it can't fill, and is
cancelled at the end. The one round-trip test (buy + sell 1 share) only runs
during regular hours.
"""
from __future__ import annotations

import time
import uuid

import pytest

from tradingdesk.broker.base import Broker, BrokerError, EntryRequest, OrderStatus

TICKER = "F"     # liquid, cheap


def _cid(tag: str) -> str:
    return f"td-contract-{tag}-{uuid.uuid4().hex[:8]}"


def _price(b: Broker) -> float:
    q = b.quote(TICKER)
    assert q is not None and q.price > 0
    return q.price


def _settle(b: Broker, oid: str, want_open: bool, timeout: float = 10) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if b.get_order(oid).status.is_open == want_open:
            return
        time.sleep(0.5)
    raise AssertionError(f"order {oid} open={not want_open} after {timeout}s")


def test_is_paper_or_dry_run(broker: Broker):
    caps = broker.capabilities()
    assert caps.paper or caps.dry_run
    assert not caps.extended_hours_stops        # Alpaca can't hold stops premarket; dry run mirrors it


def test_account_and_clock(broker: Broker):
    a = broker.account()
    assert a.cash >= 0 and a.buying_power >= 0 and a.equity >= 0
    c = broker.clock()
    assert c.next_open.tzinfo is not None and c.next_close.tzinfo is not None


def test_limit_order_lifecycle_and_idempotency(broker: Broker):
    px = round(max(_price(broker) * 0.5, 1.0), 2)          # far below market: won't fill
    cid = _cid("lim")
    o = broker.submit_limit(TICKER, "buy", 1, px, cid)
    try:
        assert o.status.is_open and o.client_order_id == cid and o.qty == 1
        assert any(x.id == o.id for x in broker.open_orders(TICKER))
        assert broker.get_order(o.id).limit_price == px
        assert broker.get_order_by_client_id(cid).id == o.id
        again = broker.submit_limit(TICKER, "buy", 1, px, cid)  # same client id -> same order, not a new one
        assert again.id == o.id
        assert sum(1 for x in broker.open_orders(TICKER) if x.client_order_id == cid) == 1
        # Alpaca refuses to replace a merely queued ("accepted") order while the market is
        # closed, so the replace check only runs when orders are live.
        if broker.clock().is_open:
            r = broker.replace_order(o.id, limit_price=round(px * 0.99, 2))
            _settle(broker, r.id, want_open=True)
            assert broker.get_order(r.id).limit_price == round(px * 0.99, 2)
    finally:
        broker.cancel_all(TICKER)
    for x in (o.id,):
        _settle(broker, x, want_open=False)


def test_bracket_with_stop_limit_entry(broker: Broker):
    """The Executor's regular-hours order: stop-limit entry + stop loss + take profit."""
    px = _price(broker)
    trigger = round(px * 1.5, 2)                           # far above market: never triggers
    o = broker.submit_entry(EntryRequest(
        ticker=TICKER, qty=1, client_order_id=_cid("brk"), limit_price=round(trigger * 1.005, 2),
        stop_price=trigger, stop_loss=round(trigger * 0.95, 2), take_profit=round(trigger * 1.10, 2),
    ))
    try:
        assert o.status.is_open and o.order_class == "bracket" and o.type == "stop_limit"
        types = sorted(leg.type for leg in broker.get_order(o.id).legs)
        assert types in (["limit", "stop"], ["limit", "stop_limit"])
    finally:
        broker.cancel(o.id)
    _settle(broker, o.id, want_open=False)
    for leg in broker.get_order(o.id).legs:
        assert not leg.status.is_open                       # legs die with the parent


def test_extended_hours_entry_must_be_plain_limit(broker: Broker):
    with pytest.raises(BrokerError):
        broker.submit_entry(EntryRequest(ticker=TICKER, qty=1, client_order_id=_cid("ext"), limit_price=1.0,
                                         stop_loss=0.9, take_profit=1.2, extended_hours=True))


def test_cancel_all(broker: Broker):
    px = round(max(_price(broker) * 0.5, 1.0), 2)
    ids = [broker.submit_limit(TICKER, "buy", 1, px, _cid(f"ca{i}")).id for i in range(2)]
    broker.cancel_all(TICKER)
    for oid in ids:
        _settle(broker, oid, want_open=False)
    assert not [o for o in broker.open_orders(TICKER) if o.id in ids]


def test_round_trip_one_share(broker: Broker, regular_hours_only):
    """Buy 1 share, confirm the position, sell it, confirm flat."""
    buy = broker.submit_market(TICKER, "buy", 1, _cid("rt-buy"))
    _settle(broker, buy.id, want_open=False, timeout=30)
    assert broker.get_order(buy.id).status is OrderStatus.FILLED
    pos = broker.position(TICKER)
    assert pos is not None and pos.qty >= 1
    sell = broker.submit_market(TICKER, "sell", 1, _cid("rt-sell"))
    _settle(broker, sell.id, want_open=False, timeout=30)
    assert broker.get_order(sell.id).status is OrderStatus.FILLED
