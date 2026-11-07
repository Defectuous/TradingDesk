"""
Risk Officer (plain Python, final veto).

Checks hard limits and computes the final share size. Nothing can override a
rejection: the Candidate model won't move to risk_cleared without this agent's
pass, and the Trade Desk re-runs `evaluate()` at click time (phase 3) with
`requested_shares` set to what the human approved.

Dollar limits are percentages of account cash (config.yaml `risk`):
  per-trade risk and max position  % of current cash
  daily loss limit                 % of the day's first cash reading

Cash-account mode (start-of-day cash below risk.cash_account_below_usd, default $5,000):
  no margin (buying power capped at cash), and only settled cash can buy. Sale
  proceeds settle T+1, so the day's total buying is capped at start-of-day cash
  minus what was already deployed today.

Sizing:
  effective entry = max(proposed entry, current ask/price)   R:R and risk use the *current* price
  shares = floor(min(per-trade risk, remaining daily-loss budget) / risk per share)
           capped by max position $ and buying power
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from tradingdesk.market import Quote
from tradingdesk.models import Agent, Candidate, RiskSizing
from tradingdesk.sessions import SessionClock
from tradingdesk.settings import RiskConfig


@dataclass(frozen=True)
class AccountState:
    cash: float                               # current account cash (sizing basis)
    buying_power: float
    day_start_cash: float | None = None       # first cash reading today (daily-loss basis)
    realized_pnl_today: float = 0.0
    open_pnl: float = 0.0
    trades_today: int = 0                     # approved trades (filled or working)
    deployed_today: float = 0.0               # $ of buys approved/filled today (cash-account settlement)
    open_positions: int = 0
    active_tickers: frozenset[str] = frozenset()   # held, working, or a ticket already waiting

    @property
    def day_pnl(self) -> float:
        return self.realized_pnl_today + self.open_pnl

    @property
    def loss_basis(self) -> float:
        return self.day_start_cash if self.day_start_cash is not None else self.cash


@dataclass(frozen=True)
class DollarLimits:
    max_risk_per_trade: float
    max_position: float
    max_daily_loss: float


@dataclass
class RiskDecision:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    sizing: RiskSizing | None = None
    checks: dict[str, float | str | bool | None] = field(default_factory=dict)
    agent: Agent = Agent.RISK_OFFICER


def _floor(x: float) -> int:
    """floor() that treats 24.9999999 as 25 (float noise), never rounding a real fraction up."""
    return math.floor(x + 1e-9)


class RiskOfficer:
    def __init__(self, cfg: RiskConfig, clock: SessionClock) -> None:
        self.cfg = cfg
        self.clock = clock

    def dollar_limits(self, session: str, account: AccountState) -> DollarLimits:
        lim = self.cfg.for_session(session)  # type: ignore[arg-type]
        cash = max(account.cash, 0.0)
        return DollarLimits(
            max_risk_per_trade=round(cash * lim.max_risk_per_trade_pct / 100, 2),
            max_position=round(cash * lim.max_position_pct / 100, 2),
            max_daily_loss=round(max(account.loss_basis, 0.0) * self.cfg.max_daily_loss_pct / 100, 2),
        )

    def cash_account(self, account: AccountState) -> bool:
        return account.loss_basis < self.cfg.cash_account_below_usd

    def daily_loss_hit(self, account: AccountState) -> bool:
        limit = self.dollar_limits("open", account).max_daily_loss
        return limit <= 0 or account.day_pnl <= -limit

    def evaluate(
        self,
        candidate: Candidate,
        account: AccountState,
        quote: Quote | None,
        now: datetime,
        requested_shares: int | None = None,
    ) -> RiskDecision:
        agent = Agent.RISK_RECHECK if requested_shares is not None else Agent.RISK_OFFICER
        d = RiskDecision(ok=False, agent=agent)
        no = d.reasons.append
        c, t, s = self.cfg, candidate.proposed_trade, candidate.scout

        # -- session and day-level limits ---------------------------------------
        session = self.clock.trading_session_at(now)
        if session is None or not self.clock.accepting_new_trades(now):
            no(f"outside trading windows ({self.clock.phase_at(now).label})")
        elif session != candidate.session:
            no(f"session changed from {candidate.session} to {session}")
        limits = c.for_session(candidate.session)
        usd = self.dollar_limits(candidate.session, account)
        d.checks["cash"] = account.cash

        if self.daily_loss_hit(account):
            no(f"daily loss limit reached (day P&L {account.day_pnl:.2f}, limit -{usd.max_daily_loss:.2f} = "
               f"{c.max_daily_loss_pct}% of ${account.loss_basis:.2f})")
        if account.trades_today >= c.max_trades_per_day:
            no(f"max trades per day reached ({account.trades_today}/{c.max_trades_per_day})")
        if account.open_positions >= c.max_open_positions:
            no(f"max open positions reached ({account.open_positions}/{c.max_open_positions})")
        if requested_shares is None and candidate.ticker in account.active_tickers:
            no(f"{candidate.ticker} already held, working, or waiting on the desk")

        if t is None or s is None:
            no("no proposed trade or scout data")
            return d
        if s.halted:
            no("stock is halted")

        # -- liquidity -------------------------------------------------------------
        spread = quote.spread_pct if quote else None
        d.checks["spread_pct"] = spread
        if spread is None:
            no("no live bid/ask quote")
        elif spread > limits.max_spread_pct:
            no(f"spread {spread:.2f}% > max {limits.max_spread_pct}%")
        d.checks["volume"] = s.volume
        if s.volume < limits.min_volume:
            no(f"volume {s.volume:,} < min {limits.min_volume:,}")

        # -- risk/reward from the current price ---------------------------------------
        current = max(quote.ask or quote.price, quote.price) if quote else s.price
        eff_entry = max(t.entry, current)
        d.checks["effective_entry"] = eff_entry
        if eff_entry <= t.stop:
            no(f"price {current} is at or below the stop {t.stop}")
            return d
        rps = eff_entry - t.stop
        rr = round((t.target - eff_entry) / rps, 2)   # 2dp: 1.9999 must count as 2.0
        d.checks["risk_reward"] = rr
        if rr < limits.min_risk_reward:
            no(f"R:R {rr:.2f} from current price < min {limits.min_risk_reward}")

        # -- sizing -------------------------------------------------------------------------
        loss_budget = usd.max_daily_loss + min(account.day_pnl, 0.0)
        risk_dollars = min(usd.max_risk_per_trade, loss_budget)
        shares = _floor(risk_dollars / rps) if risk_dollars > 0 else 0
        shares = min(shares, _floor(usd.max_position / eff_entry))
        cash_mode = self.cash_account(account)
        spendable = min(account.buying_power, account.cash) if cash_mode else account.buying_power
        shares = min(shares, _floor(max(spendable, 0) / eff_entry))
        settled_left = None
        if cash_mode:
            settled_left = round(account.loss_basis - account.deployed_today, 2)
            d.checks["settled_cash_left"] = settled_left
            shares = min(shares, _floor(max(settled_left, 0) / eff_entry))
            if settled_left < eff_entry:
                no(f"cash account: ${max(settled_left, 0):.2f} settled cash left today "
                   f"(${account.deployed_today:.2f} of ${account.loss_basis:.2f} already used; sales settle T+1)")
        if requested_shares is not None:
            if requested_shares < 1:
                no("requested size is below 1 share")
            elif requested_shares > shares:
                no(f"requested {requested_shares} shares > {shares} allowed now")
            shares = min(shares, requested_shares)
        d.checks["shares"] = shares
        if shares < 1 and not (settled_left is not None and settled_left < eff_entry):
            no(f"stop {rps:.2f}/share too wide for ${risk_dollars:.2f} risk / ${usd.max_position:.2f} max position "
               f"(cash ${account.cash:.2f})")

        if d.reasons:
            return d
        d.ok = True
        d.sizing = RiskSizing(
            shares=shares,
            dollars_at_risk=round(shares * rps, 2),
            position_value=round(shares * eff_entry, 2),
            session=candidate.session,
            # Alpaca can't hold a stop order in extended hours (verified in phase 3).
            software_managed_stop=candidate.session == "premarket",
            limits={
                "account_mode": "cash" if cash_mode else "margin",
                "cash": account.cash,
                **({"settled_cash_left": settled_left} if settled_left is not None else {}),
                "max_risk_per_trade_usd": usd.max_risk_per_trade,
                "max_position_usd": usd.max_position,
                "max_daily_loss_usd": usd.max_daily_loss,
                "max_spread_pct": limits.max_spread_pct,
                "min_risk_reward": limits.min_risk_reward,
                "risk_reward_now": rr,
            },
        )
        return d
