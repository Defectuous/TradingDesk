"""Broker selection (config.yaml `broker`)."""
from __future__ import annotations

from tradingdesk.broker.base import Broker
from tradingdesk.market import MarketData
from tradingdesk.settings import Settings


def make_broker(settings: Settings, market: MarketData | None = None) -> Broker:
    cfg = settings.config.broker
    if cfg.dry_run:
        from tradingdesk.broker.dry_run import DryRunBroker
        from tradingdesk.sessions import ET, now_et

        def regular() -> bool:
            t = now_et().astimezone(ET)
            return t.weekday() < 5 and settings.config.sessions.open <= t.time() < settings.config.sessions.market_close

        return DryRunBroker(cash=1000.0, quote_source=market.quote if market else None, regular_hours_source=regular)
    if cfg.name == "alpaca":
        from tradingdesk.broker.alpaca import AlpacaBroker

        return AlpacaBroker(settings.secrets.alpaca_api_key, settings.secrets.alpaca_secret_key)
    raise ValueError(f"unknown broker {cfg.name!r}")
