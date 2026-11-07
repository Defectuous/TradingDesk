"""
The Candidate — the contract that moves down the agent chain.

Rules enforced here (not just by convention):
- Each stage's data (scout, news, sentiment, proposed_trade, risk_sizing, human)
  can be set once. Setting it again raises StageAlreadySet.
- `verdicts`, `fills` and `status_history` are append-only tuples.
- `status` only changes through `transition()`, which checks ALLOWED_TRANSITIONS.
- A gate verdict is "pass" or "reject". There is no "approve" or "modify"
  decision: gates can only say no.
- The human can reduce size, never increase it.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from tradingdesk.sessions import ET, TradingSession, now_et

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def _aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return v.astimezone(ET)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class CandidateStatus(StrEnum):
    SPOTTED = "spotted"
    RESEARCHED = "researched"
    CHALLENGED = "challenged"
    RISK_CLEARED = "risk_cleared"
    AWAITING_HUMAN = "awaiting_human"
    HUMAN_APPROVED = "human_approved"
    HUMAN_PASSED = "human_passed"
    EXPIRED = "expired"
    REJECTED = "rejected"
    FILLED = "filled"
    CLOSED = "closed"


S = CandidateStatus
ALLOWED_TRANSITIONS: dict[CandidateStatus, frozenset[CandidateStatus]] = {
    S.SPOTTED:        frozenset({S.RESEARCHED, S.REJECTED, S.EXPIRED}),
    S.RESEARCHED:     frozenset({S.CHALLENGED, S.REJECTED, S.EXPIRED}),
    S.CHALLENGED:     frozenset({S.RISK_CLEARED, S.REJECTED, S.EXPIRED}),
    S.RISK_CLEARED:   frozenset({S.AWAITING_HUMAN, S.REJECTED, S.EXPIRED}),
    # REJECTED from here = click-time Risk Officer re-check failed
    S.AWAITING_HUMAN: frozenset({S.HUMAN_APPROVED, S.HUMAN_PASSED, S.EXPIRED, S.REJECTED}),
    # REJECTED = broker rejected the order; EXPIRED = entry never filled (e.g. cancelled at cutoff)
    S.HUMAN_APPROVED: frozenset({S.FILLED, S.REJECTED, S.EXPIRED}),
    S.FILLED:         frozenset({S.CLOSED}),
    S.HUMAN_PASSED:   frozenset(),
    S.EXPIRED:        frozenset(),
    S.REJECTED:       frozenset(),
    S.CLOSED:         frozenset(),
}
TERMINAL_STATUSES = frozenset(s for s, nxt in ALLOWED_TRANSITIONS.items() if not nxt)


class Agent(StrEnum):
    SCOUT = "scout"
    NEWSIE = "newsie"
    SENTIMENT = "sentiment"
    MANAGER = "manager"
    SKEPTIC = "skeptic"
    RISK_OFFICER = "risk_officer"
    RISK_RECHECK = "risk_recheck"     # click-time re-check
    DESK = "desk"                     # ticket expiry
    HUMAN = "human"
    EXECUTOR = "executor"
    BROKER = "broker"
    TRADE_MANAGER = "trade_manager"


CatalystGrade = Literal["A", "B", "C", "D", "F"]


# ---------------------------------------------------------------------------
# Shared records
# ---------------------------------------------------------------------------

class Verdict(_Frozen):
    agent: Agent
    decision: Literal["pass", "reject"]
    reasons: tuple[str, ...] = ()
    timestamp: datetime = Field(default_factory=now_et)
    details: dict[str, Any] = {}

    check_ts = field_validator("timestamp")(_aware)


class StatusChange(_Frozen):
    status: CandidateStatus
    at: datetime
    by: Agent

    check_ts = field_validator("at")(_aware)


class Headline(_Frozen):
    """A normalized news item from any NewsSource."""

    source: str
    tickers: tuple[str, ...]
    title: str
    summary: str = ""
    url: str = ""
    published_at: datetime
    also_seen_in: tuple[str, ...] = ()   # other sources carrying the same story (after dedup)

    check_ts = field_validator("published_at")(_aware)


# ---------------------------------------------------------------------------
# Stage data
# ---------------------------------------------------------------------------

class PillarCheck(_Frozen):
    passed: bool
    value: float | str | None = None
    threshold: float | str | None = None
    note: str | None = None


class ScoutData(_Frozen):
    price: float = Field(gt=0)
    change_pct: float
    gap_pct: float | None = None
    relative_volume: float = Field(ge=0)
    volume: int = Field(0, ge=0)
    float_shares: int | None = Field(None, ge=0)
    spread_pct: float | None = Field(None, ge=0)
    vwap: float | None = None
    day_open: float | None = None
    premarket_high: float | None = None
    high_of_day: float | None = None
    pillars: dict[str, PillarCheck]          # e.g. {"rvol": ..., "momentum": ..., "catalyst": ...}
    qualifies_reduced: bool = False           # WTBot's half-size path (missing/vague catalyst)
    halted: bool = False

    @property
    def pillars_passed(self) -> int:
        return sum(1 for p in self.pillars.values() if p.passed)


class GradedHeadline(_Frozen):
    headline: Headline
    grade: CatalystGrade | None = None


class FilingFlags(_Frozen):
    offering: bool = False          # S-1 / S-3 / 424B*
    dilution: bool = False          # offering, ATM, warrants, convertibles...
    insider_buy: bool = False       # Form 4
    insider_sell: bool = False      # Form 4
    forms: tuple[str, ...] = ()     # raw form types seen, e.g. ("8-K", "424B5")


class NewsData(_Frozen):
    catalyst_grade: CatalystGrade
    catalyst_type: str               # FDA, Contract, Buyout, Earnings, Partnership, Trial data, Offering, None
    grade_reason: str                # one sentence
    headlines: tuple[GradedHeadline, ...] = ()
    filing_flags: FilingFlags = FilingFlags()
    graded_at: datetime
    graded_by: Literal["claude", "local", "rules"] = "claude"

    check_ts = field_validator("graded_at")(_aware)


class SentimentData(_Frozen):
    buzz_score: float = Field(ge=0)
    buzz_trend: Literal["rising", "flat", "fading"]
    promo_warning: bool = False      # coordinated promotion / bot pattern detected
    market_regime: Literal["supportive", "neutral", "hostile"]
    regime_detail: dict[str, Any] = {}
    as_of: datetime

    check_ts = field_validator("as_of")(_aware)


class ProposedTrade(_Frozen):
    """Manager's plan from Scout data. Long only."""

    side: Literal["long"] = "long"
    entry: float = Field(gt=0)
    stop: float = Field(gt=0)
    target: float = Field(gt=0)
    entry_type: Literal["limit", "stop_limit", "marketable_limit"] = "limit"
    strategy: str = ""

    @model_validator(mode="after")
    def check_levels(self) -> "ProposedTrade":
        if not (self.stop < self.entry < self.target):
            raise ValueError(f"long trade needs stop < entry < target (got {self.stop}/{self.entry}/{self.target})")
        return self

    @property
    def risk_per_share(self) -> float:
        return round(self.entry - self.stop, 4)

    @property
    def risk_reward(self) -> float:
        return round((self.target - self.entry) / (self.entry - self.stop), 2)


class RiskSizing(_Frozen):
    """Risk Officer's final share size and the limits it was computed under."""

    shares: int = Field(ge=1)
    dollars_at_risk: float = Field(ge=0)
    position_value: float = Field(ge=0)
    session: TradingSession
    software_managed_stop: bool = False   # True when the broker can't hold the stop (e.g. extended hours)
    limits: dict[str, float] = {}


class HumanDecision(_Frozen):
    decision: Literal["approved", "passed", "expired"]
    shown_at: datetime
    decided_at: datetime
    note: str | None = Field(None, max_length=200)
    approved_shares: int | None = Field(None, ge=1)

    check_ts = field_validator("shown_at", "decided_at")(_aware)

    @model_validator(mode="after")
    def check_decision(self) -> "HumanDecision":
        if self.decided_at < self.shown_at:
            raise ValueError("decided_at is before shown_at")
        if self.decision == "approved" and self.approved_shares is None:
            raise ValueError("an approval must carry approved_shares")
        if self.decision != "approved" and self.approved_shares is not None:
            raise ValueError("only an approval carries approved_shares")
        return self

    @property
    def decision_seconds(self) -> float:
        return round((self.decided_at - self.shown_at).total_seconds(), 2)


class Fill(_Frozen):
    side: Literal["buy", "sell"]
    qty: int = Field(ge=1)
    price: float = Field(gt=0)
    at: datetime
    order_id: str = ""
    reason: str = ""   # entry | stop | target | partial | trail | wind-down | flatten | kill-switch

    check_ts = field_validator("at")(_aware)


# ---------------------------------------------------------------------------
# Candidate
# ---------------------------------------------------------------------------

class StageAlreadySet(RuntimeError):
    pass


class IllegalTransition(RuntimeError):
    pass


_SET_ONCE = ("scout", "news", "sentiment", "proposed_trade", "risk_sizing", "human", "rejected_by")
_APPEND_ONLY = ("verdicts", "fills", "status_history", "rejection_reasons")
_GUARDED = frozenset(_SET_ONCE + _APPEND_ONLY + ("status", "id", "ticker", "created_at", "session"))


class Candidate(BaseModel):
    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    ticker: str
    created_at: datetime = Field(default_factory=now_et)
    session: TradingSession
    status: CandidateStatus = CandidateStatus.SPOTTED
    status_history: tuple[StatusChange, ...] = ()

    scout: ScoutData | None = None
    news: NewsData | None = None
    sentiment: SentimentData | None = None
    proposed_trade: ProposedTrade | None = None
    risk_sizing: RiskSizing | None = None
    human: HumanDecision | None = None
    fills: tuple[Fill, ...] = ()

    verdicts: tuple[Verdict, ...] = ()
    rejected_by: Agent | None = None
    rejection_reasons: tuple[str, ...] = ()

    check_ts = field_validator("created_at")(_aware)
    _unlocked: bool = PrivateAttr(default=False)

    @field_validator("ticker")
    @classmethod
    def check_ticker(cls, v: str) -> str:
        v = v.strip().upper()
        if not _TICKER_RE.match(v):
            raise ValueError(f"invalid ticker {v!r}")
        return v

    # -- write guards -------------------------------------------------------
    def __setattr__(self, name: str, value: Any) -> None:
        if name in _GUARDED and not getattr(self, "_unlocked", False):
            current = getattr(self, name, None)
            if name in _SET_ONCE:
                if current is not None:
                    raise StageAlreadySet(f"{name} is already set on {self.ticker}; stages never overwrite")
            elif name in _APPEND_ONLY:
                value = tuple(value)
                if value[: len(current)] != current:
                    raise StageAlreadySet(f"{name} is append-only")
            else:
                raise StageAlreadySet(f"{name} can only change through Candidate methods")
        super().__setattr__(name, value)

    def _set(self, name: str, value: Any) -> None:
        self._unlocked = True
        try:
            super().__setattr__(name, value)
        finally:
            self._unlocked = False

    # -- stage attachment ---------------------------------------------------
    def attach(self, stage: str, data: BaseModel) -> None:
        """Attach a stage's data once. stage is one of scout/news/sentiment/proposed_trade/risk_sizing."""
        if stage not in ("scout", "news", "sentiment", "proposed_trade", "risk_sizing"):
            raise ValueError(f"unknown stage {stage!r}")
        setattr(self, stage, data)

    # -- status ---------------------------------------------------------------
    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def transition(self, new: CandidateStatus, by: Agent, at: datetime | None = None) -> None:
        new = CandidateStatus(new)
        if new not in ALLOWED_TRANSITIONS[self.status]:
            raise IllegalTransition(f"{self.ticker}: {self.status} -> {new} is not allowed")
        self.status_history = (*self.status_history, StatusChange(status=new, at=at or now_et(), by=by))
        self._set("status", new)

    # -- verdicts ---------------------------------------------------------------
    def add_verdict(self, verdict: Verdict) -> None:
        self.verdicts = (*self.verdicts, verdict)

    def record_pass(self, agent: Agent, reasons: list[str] | tuple[str, ...] = (), **details: Any) -> Verdict:
        v = Verdict(agent=agent, decision="pass", reasons=tuple(reasons), details=details)
        self.add_verdict(v)
        return v

    def reject(self, agent: Agent, reasons: list[str] | tuple[str, ...], **details: Any) -> Verdict:
        """Any gate can end the chain. Records the verdict and moves to REJECTED."""
        if not reasons:
            raise ValueError("a rejection needs at least one reason")
        if CandidateStatus.REJECTED not in ALLOWED_TRANSITIONS[self.status]:
            raise IllegalTransition(f"{self.ticker}: cannot reject from {self.status}")
        v = Verdict(agent=agent, decision="reject", reasons=tuple(reasons), details=details)
        self.add_verdict(v)
        self.rejected_by = agent
        self.rejection_reasons = (*self.rejection_reasons, *v.reasons)
        self.transition(CandidateStatus.REJECTED, by=agent, at=v.timestamp)
        return v

    # -- human decision -----------------------------------------------------------
    def record_human(self, decision: HumanDecision) -> None:
        if self.status is not CandidateStatus.AWAITING_HUMAN:
            raise IllegalTransition(f"{self.ticker}: no ticket awaiting a decision (status={self.status})")
        if decision.decision == "approved":
            if self.risk_sizing is None:
                raise IllegalTransition("cannot approve before the Risk Officer has sized the trade")
            if decision.approved_shares > self.risk_sizing.shares:  # type: ignore[operator]
                raise ValueError("size can only be reduced, never increased")
        self.human = decision
        nxt = {
            "approved": CandidateStatus.HUMAN_APPROVED,
            "passed": CandidateStatus.HUMAN_PASSED,
            "expired": CandidateStatus.EXPIRED,
        }[decision.decision]
        by = Agent.DESK if decision.decision == "expired" else Agent.HUMAN
        self.transition(nxt, by=by, at=decision.decided_at)

    # -- fills ---------------------------------------------------------------------
    def add_fill(self, fill: Fill) -> None:
        self.fills = (*self.fills, fill)

    @property
    def position_qty(self) -> int:
        return sum(f.qty if f.side == "buy" else -f.qty for f in self.fills)

    @property
    def realized_pnl(self) -> float:
        """P&L of the shares already sold, against the average buy price."""
        buys = [f for f in self.fills if f.side == "buy"]
        sells = [f for f in self.fills if f.side == "sell"]
        if not buys or not sells:
            return 0.0
        bought = sum(f.qty for f in buys)
        avg_buy = sum(f.qty * f.price for f in buys) / bought
        return round(sum(f.qty * (f.price - avg_buy) for f in sells), 2)

    @property
    def r_multiple(self) -> float | None:
        if not self.risk_sizing or not self.risk_sizing.dollars_at_risk or not self.fills:
            return None
        return round(self.realized_pnl / self.risk_sizing.dollars_at_risk, 2)

    @property
    def trade_date(self) -> str:
        return self.created_at.astimezone(ET).date().isoformat()
