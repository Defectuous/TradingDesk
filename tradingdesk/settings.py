"""
Typed settings: config.yaml (everything non-secret) + .env (secrets only).

`load_settings()` never mutates os.environ. Values in the real process
environment win over values in the .env file, so a one-off override like
`set ALPACA_PAPER=true` behaves as expected.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import time
from pathlib import Path
from typing import Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config.yaml"
DEFAULT_ENV = PROJECT_ROOT / ".env"


class _Strict(BaseModel):
    """Reject unknown keys so a typo in config.yaml fails loudly instead of being ignored."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# config.yaml sections
# ---------------------------------------------------------------------------

class PathsConfig(_Strict):
    data_dir: Path = Path("data")
    journal_db: Path = Path("data/journal.db")
    log_dir: Path = Path("logs")


class SessionsConfig(_Strict):
    startup: time = time(5, 30)
    premarket_start: time = time(6, 0)
    open: time = time(9, 30)
    new_trade_cutoff: time = time(11, 0)
    wind_down: time = time(11, 30)
    market_close: time = time(16, 0)
    wind_down_mode: Literal["close", "hold"] = "close"
    tighten_stops_after_cutoff: bool = False
    no_entries_first_minutes: int = Field(0, ge=0)
    no_entries_last_minutes: int = Field(0, ge=0)

    @field_validator(
        "startup", "premarket_start", "open", "new_trade_cutoff", "wind_down", "market_close",
        mode="before",
    )
    @classmethod
    def parse_hhmm(cls, v: object) -> object:
        if isinstance(v, str):
            hh, mm = v.strip().split(":")
            return time(int(hh), int(mm))
        if isinstance(v, int):  # YAML 1.1 reads unquoted 09:30 as sexagesimal int
            raise ValueError("quote session times in config.yaml, e.g. \"09:30\"")
        return v

    @model_validator(mode="after")
    def check_order(self) -> "SessionsConfig":
        seq = [self.startup, self.premarket_start, self.open, self.new_trade_cutoff, self.wind_down]
        if seq != sorted(seq) or len(set(seq)) != len(seq):
            raise ValueError(
                "session times must be strictly increasing: "
                "startup < premarket_start < open < new_trade_cutoff < wind_down"
            )
        if self.market_close <= self.new_trade_cutoff:
            raise ValueError("market_close must be after new_trade_cutoff")
        return self


class SessionRisk(_Strict):
    max_risk_per_trade_pct: float = Field(gt=0, le=10)    # % of current cash
    max_position_pct: float = Field(gt=0, le=100)         # % of current cash
    max_spread_pct: float = Field(gt=0)
    min_volume: int = Field(ge=0)
    min_risk_reward: float = Field(gt=0)


class RiskConfig(_Strict):
    cash_refresh_sec: float = Field(60, gt=0)
    max_daily_loss_pct: float = Field(gt=0, le=50)        # % of start-of-day cash
    max_trades_per_day: int = Field(gt=0)
    max_open_positions: int = Field(gt=0)
    premarket: SessionRisk
    open: SessionRisk

    def for_session(self, session: Literal["premarket", "open"]) -> SessionRisk:
        return self.premarket if session == "premarket" else self.open


class PillarsConfig(_Strict):
    min_rvol: float = 2.0
    min_daily_change_pct: float = 10.0
    min_gap_pct: float = 5.0
    min_price: float = 1.0
    max_price: float = 20.0
    universe_min_price: float = 0.10
    max_float_shares: int = 20_000_000
    rvol_lookback_days: int = 30
    rvol_outlier_multiple: float = 3.0
    strong_momentum_pct: float = 15.0


class ScoutConfig(_Strict):
    poll_interval_sec: float = Field(30, gt=0)
    cooldown_sec: float = Field(600, ge=0)
    max_movers_per_pass: int = Field(25, ge=1)
    universe_refresh_min: float = Field(30, gt=0)
    workers: int = Field(8, ge=1, le=32)
    pillars: PillarsConfig = PillarsConfig()


class ManagerConfig(_Strict):
    """The single breakout setup the Manager proposes (no strategy selector)."""

    entry_buffer_pct: float = Field(0.1, ge=0)    # entry this % above the breakout level
    stop_lookback_bars: int = Field(5, ge=1)      # stop under the low of the last N 1-min bars
    stop_buffer_pct: float = Field(0.2, ge=0)
    min_stop_pct: float = Field(1.0, ge=0)        # never tighter than this % below entry
    max_stop_pct: float = Field(8.0, gt=0)        # wider than this = no trade
    target_r: float = Field(2.0, gt=0)            # first target at entry + N x risk
    target_2_r: float = Field(3.0, gt=0)
    max_chase_pct: float = Field(2.0, ge=0)       # skip if price already this far past entry
    reject_no_catalyst: bool = True               # WTBot's hard gate: graded "none" = no trade

    @model_validator(mode="after")
    def check_stops(self) -> "ManagerConfig":
        if self.min_stop_pct >= self.max_stop_pct:
            raise ValueError("manager.min_stop_pct must be below max_stop_pct")
        return self


class DeskConfig(_Strict):
    host: str = "127.0.0.1"
    port: int = 8765
    ticket_ttl_sec: float = Field(90, gt=0)
    max_entry_drift_pct: float = Field(1.5, gt=0)
    alert_sound: bool = True


class AlpacaConfig(_Strict):
    paper: bool = True
    data_feed: Literal["sip", "iex"] = "sip"


class BrokerConfig(_Strict):
    name: str = "alpaca"
    dry_run: bool = False
    alpaca: AlpacaConfig = AlpacaConfig()


class TradeManagerConfig(_Strict):
    breakeven_at_r: float = 1.0
    partial_take_fraction: float = Field(0.5, ge=0, le=1)
    trail_after_partial: bool = True
    trail_pct: float = Field(3.0, gt=0)
    poll_interval_sec: float = Field(2, gt=0)


class WindDownConfig(_Strict):
    close_limit_offset_pct: float = Field(0.3, ge=0)
    retry_interval_sec: float = Field(3, gt=0)
    max_limit_attempts: int = Field(5, ge=1)
    halt_retry_interval_sec: float = Field(15, gt=0)


class ModelPrice(_Strict):
    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_write: float = Field(ge=0)
    cache_read: float = Field(ge=0)


class ClaudeConfig(_Strict):
    newsie_model: str
    skeptic_model: str
    newsie_max_tokens: int = 1024
    skeptic_max_tokens: int = 2048
    timeout_sec: float = 60


class LocalLLMConfig(_Strict):
    base_url: str
    model: str
    thinking: bool = True
    fallback_model: str | None = None
    timeout_sec: float = 120


class LLMConfig(_Strict):
    daily_spend_cap_usd: float = Field(ge=0)
    spike_warn_multiple: float = Field(3.0, gt=1)
    spike_window_calls: int = Field(20, ge=1)
    max_retries: int = Field(2, ge=0, le=5)
    claude: ClaudeConfig
    local: LocalLLMConfig
    pricing: dict[str, ModelPrice]


class SourceToggle(BaseModel):
    """A source entry. `enabled` may be true, false, or "auto" (on when its key is present).
    Extra per-source options (forms, subreddits, rate limits...) are kept as-is."""

    model_config = ConfigDict(extra="allow", frozen=True)
    enabled: bool | Literal["auto"] = False


class NewsConfig(_Strict):
    poll_interval_sec: float = 60
    lookback_hours: int = 24
    dedup_title_similarity: float = Field(0.90, gt=0, le=1)
    rubric_file: Path = Path("prompts/catalyst_rubric.md")
    sources: dict[str, SourceToggle] = {}


class SentimentConfig(_Strict):
    poll_interval_sec: float = 120
    regime_symbols: list[str] = ["SPY", "QQQ", "IWM"]
    vix_symbol: str = "VIX"
    sources: dict[str, SourceToggle] = {}


class AlertsConfig(_Strict):
    discord_enabled: bool = True


class AppConfig(_Strict):
    paths: PathsConfig = PathsConfig()
    sessions: SessionsConfig = SessionsConfig()
    risk: RiskConfig
    scout: ScoutConfig = ScoutConfig()
    manager: ManagerConfig = ManagerConfig()
    desk: DeskConfig = DeskConfig()
    broker: BrokerConfig = BrokerConfig()
    trade_manager: TradeManagerConfig = TradeManagerConfig()
    wind_down: WindDownConfig = WindDownConfig()
    llm: LLMConfig
    news: NewsConfig = NewsConfig()
    sentiment: SentimentConfig = SentimentConfig()
    alerts: AlertsConfig = AlertsConfig()


# ---------------------------------------------------------------------------
# .env secrets
# ---------------------------------------------------------------------------

def _truthy(v: str | None, default: bool) -> bool:
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() not in ("false", "0", "no", "off")


class Secrets(BaseModel):
    """Secrets from .env. repr() never shows values."""

    model_config = ConfigDict(frozen=True)

    alpaca_api_key: str = Field("", repr=False)
    alpaca_secret_key: str = Field("", repr=False)
    alpaca_paper: bool = True
    alpaca_base_url: str = ""
    anthropic_api_key: str = Field("", repr=False)
    discord_webhook_url: str = Field("", repr=False)
    sec_user_agent: str = ""
    newsfilter_api_key: str = Field("", repr=False)
    x_bearer_token: str = Field("", repr=False)
    reddit_client_id: str = Field("", repr=False)
    reddit_client_secret: str = Field("", repr=False)
    reddit_user_agent: str = ""
    stocktwits_access_token: str = Field("", repr=False)
    # Only the exact string "true" (any case) enables live trading. Never set by code.
    allow_live: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str | None]) -> "Secrets":
        def g(k: str) -> str:
            return (env.get(k) or "").strip()

        return cls(
            alpaca_api_key=g("ALPACA_API_KEY"),
            alpaca_secret_key=g("ALPACA_SECRET_KEY"),
            alpaca_paper=_truthy(env.get("ALPACA_PAPER"), default=True),
            alpaca_base_url=g("ALPACA_BASE_URL"),
            anthropic_api_key=g("ANTHROPIC_API_KEY"),
            discord_webhook_url=g("DISCORD_WEBHOOK_URL"),
            sec_user_agent=g("SEC_USER_AGENT"),
            newsfilter_api_key=g("NEWSFILTER_API_KEY"),
            x_bearer_token=g("X_BEARER_TOKEN"),
            reddit_client_id=g("REDDIT_CLIENT_ID"),
            reddit_client_secret=g("REDDIT_CLIENT_SECRET"),
            reddit_user_agent=g("REDDIT_USER_AGENT"),
            stocktwits_access_token=g("STOCKTWITS_ACCESS_TOKEN"),
            allow_live=g("ALLOW_LIVE").lower() == "true",
        )


# Which secret switches an "auto" source on.
_AUTO_SOURCE_KEYS: dict[str, str] = {
    "newsfilter": "newsfilter_api_key",
    "x": "x_bearer_token",
}


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    config: AppConfig
    secrets: Secrets
    root: Path = PROJECT_ROOT

    def path(self, p: Path) -> Path:
        """Resolve a config path relative to the project root."""
        return p if p.is_absolute() else self.root / p

    def source_enabled(self, section: Literal["news", "sentiment"], name: str) -> bool:
        sources = getattr(self.config, section).sources
        toggle = sources.get(name)
        if toggle is None:
            return False
        if toggle.enabled == "auto":
            key_field = _AUTO_SOURCE_KEYS.get(name)
            return bool(key_field and getattr(self.secrets, key_field))
        return bool(toggle.enabled)


def load_settings(
    config_path: Path | str | None = None,
    env_file: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Load config.yaml + .env. `environ` defaults to os.environ (it wins over the .env file)."""
    cfg_path = Path(config_path) if config_path else DEFAULT_CONFIG
    with open(cfg_path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    config = AppConfig.model_validate(raw)

    env_path = Path(env_file) if env_file else DEFAULT_ENV
    merged: dict[str, str | None] = dict(dotenv_values(env_path)) if env_path.exists() else {}
    merged.update(os.environ if environ is None else environ)
    secrets = Secrets.from_env(merged)

    return Settings(config=config, secrets=secrets, root=cfg_path.resolve().parent)
