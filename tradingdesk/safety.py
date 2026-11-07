"""
Paper-only startup check.

Refuses to start if anything points at live trading, unless ALLOW_LIVE=true is
set in the environment. Nothing in this codebase sets that flag.

Rules
- Every broker must offer a real paper account or a dry-run mode. A broker with
  neither cannot be enabled without ALLOW_LIVE.
- Alpaca: config `broker.alpaca.paper` and env `ALPACA_PAPER` must both be true,
  `ALPACA_BASE_URL` (if set) must be the paper host, and the key ID must not be a
  live key (live IDs start with "AK", paper IDs with "PK").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from loguru import logger

from tradingdesk.settings import Settings

ALPACA_PAPER_HOST = "paper-api.alpaca.markets"
ALPACA_LIVE_HOST = "api.alpaca.markets"


@dataclass(frozen=True)
class BrokerSafetyProfile:
    """What a broker offers for non-live operation. Each Broker implementation registers one."""

    has_paper_account: bool
    has_dry_run: bool


BROKER_PROFILES: dict[str, BrokerSafetyProfile] = {
    "alpaca": BrokerSafetyProfile(has_paper_account=True, has_dry_run=True),
}


class LiveTradingRefused(RuntimeError):
    """Raised at startup when a live-trading key, URL or setting is detected."""

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        lines = "\n  - ".join(violations)
        super().__init__(
            "Refusing to start: live trading detected.\n  - "
            f"{lines}\n"
            "TradingDesk is paper-only. Fix the settings above (or, only if you truly intend "
            "live trading, set ALLOW_LIVE=true yourself)."
        )


@dataclass
class SafetyReport:
    broker: str
    mode: str                       # "paper" | "dry-run" | "LIVE"
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    live_allowed: bool = False


def _alpaca_violations(settings: Settings) -> tuple[list[str], list[str]]:
    s, cfg = settings.secrets, settings.config.broker.alpaca
    violations: list[str] = []
    warnings: list[str] = []

    if not cfg.paper:
        violations.append("config.yaml broker.alpaca.paper is false")
    if not s.alpaca_paper:
        violations.append("ALPACA_PAPER is false in the environment/.env")

    if s.alpaca_base_url:
        host = (urlparse(s.alpaca_base_url).hostname or "").lower()
        if host != ALPACA_PAPER_HOST:
            violations.append(
                f"ALPACA_BASE_URL points at {host or s.alpaca_base_url!r}, not {ALPACA_PAPER_HOST}"
            )

    key = s.alpaca_api_key
    if key.upper().startswith("AK"):
        violations.append("ALPACA_API_KEY looks like a LIVE key (starts with 'AK'); paper keys start with 'PK'")
    elif key and not key.upper().startswith("PK"):
        warnings.append("ALPACA_API_KEY has an unrecognised prefix; could not confirm it is a paper key")
    elif not key:
        warnings.append("ALPACA_API_KEY is not set; broker and market data will be unavailable")

    return violations, warnings


def check_paper_only(settings: Settings) -> SafetyReport:
    """Run the startup safety check. Raises LiveTradingRefused unless ALLOW_LIVE=true."""
    broker = settings.config.broker.name.lower()
    dry_run = settings.config.broker.dry_run
    violations: list[str] = []
    warnings: list[str] = []

    profile = BROKER_PROFILES.get(broker)
    if profile is None:
        violations.append(f"broker {broker!r} has no registered safety profile")
    elif not (profile.has_paper_account or profile.has_dry_run):
        violations.append(f"broker {broker!r} offers neither a paper account nor a dry-run mode")
    elif dry_run and not profile.has_dry_run:
        violations.append(f"broker {broker!r} has no dry-run mode but broker.dry_run is true")

    if broker == "alpaca":
        v, w = _alpaca_violations(settings)
        violations += v
        warnings += w

    allow_live = settings.secrets.allow_live
    mode = "dry-run" if dry_run else "paper"
    if violations:
        if not allow_live:
            raise LiveTradingRefused(violations)
        mode = "LIVE"
        logger.critical("ALLOW_LIVE=true — live-trading settings accepted: {}", "; ".join(violations))

    for w in warnings:
        logger.warning("safety: {}", w)

    return SafetyReport(
        broker=broker, mode=mode, violations=violations, warnings=warnings, live_allowed=allow_live
    )


def env_is_gitignored(root: Path) -> bool:
    """True if the project's .gitignore lists .env."""
    gi = root / ".gitignore"
    if not gi.exists():
        return False
    lines = {ln.strip() for ln in gi.read_text(encoding="utf-8").splitlines()}
    return ".env" in lines or "/.env" in lines
