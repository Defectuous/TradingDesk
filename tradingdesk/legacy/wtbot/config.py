"""
Stand-in for WTBot's `config` module, so the copied WTBot code runs unchanged.

The copied modules read constants like `config.MIN_RVOL` at call time. Defaults
here match WTBot; `apply_settings()` overwrites them from TradingDesk settings
and must run before the first market-data call (the Alpaca clients are created
lazily with these keys).

Paper is always forced on here: WTBot's universe fetch builds a TradingClient.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tradingdesk.settings import Settings

ALPACA_API_KEY: str = ""
ALPACA_SECRET_KEY: str = ""
ALPACA_PAPER: bool = True

MIN_RVOL: float = 2.0
MIN_DAILY_CHANGE_PCT: float = 10.0
MIN_PRICE: float = 1.00
MAX_PRICE: float = 20.00
UNIVERSE_MIN_PRICE: float = 0.10
MAX_FLOAT_SHARES: int = 20_000_000
RVOL_LOOKBACK_DAYS: int = 30
RVOL_OUTLIER_MULTIPLE: float = 3.0
STRONG_MOMENTUM_PCT: float = 15.0
NEWS_LOOKBACK_HOURS: int = 24
FLOAT_LOOKUP_MIN_INTERVAL_SEC: float = 1.0


def apply_settings(settings: "Settings") -> None:
    g = globals()
    g["ALPACA_API_KEY"] = settings.secrets.alpaca_api_key
    g["ALPACA_SECRET_KEY"] = settings.secrets.alpaca_secret_key
    g["ALPACA_PAPER"] = True
    p = settings.config.scout.pillars
    g["MIN_RVOL"] = p.min_rvol
    g["MIN_DAILY_CHANGE_PCT"] = p.min_daily_change_pct
    g["MIN_PRICE"] = p.min_price
    g["MAX_PRICE"] = p.max_price
    g["UNIVERSE_MIN_PRICE"] = p.universe_min_price
    g["MAX_FLOAT_SHARES"] = p.max_float_shares
    g["RVOL_LOOKBACK_DAYS"] = p.rvol_lookback_days
    g["RVOL_OUTLIER_MULTIPLE"] = p.rvol_outlier_multiple
    g["STRONG_MOMENTUM_PCT"] = p.strong_momentum_pct
    g["NEWS_LOOKBACK_HOURS"] = settings.config.news.lookback_hours
