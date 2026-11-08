"""
Trade Desk page wiring, in NiceGUI's simulated browser (no real server or browser).

The rules themselves (re-check, idempotency, reduce-only) are tested at the
DeskService level in test_execution.py; these check that the buttons reach them
and that the page reflects the result.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from tests.test_execution import OPEN_NOW, Rig
from tradingdesk.agents.risk_officer import AccountState
from tradingdesk.desk import service as service_mod
from tradingdesk.desk import view as view_mod
from tradingdesk.desk.ui import page_factory
from tradingdesk.desk.view import ActionLog, DeskView
from tradingdesk.market import Quote
from tradingdesk.models import CandidateStatus

S = CandidateStatus


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture
def rig(make_settings, tmp_path, monkeypatch):
    clock = Clock(OPEN_NOW)
    monkeypatch.setattr(service_mod, "now_et", clock)
    monkeypatch.setattr(view_mod, "now_et", clock)
    r = Rig(make_settings(), tmp_path, regular=True)
    r.clock = clock
    r.market.add("ABCD", price=4.95, pm_high=4.60, hod=5.00, quote=Quote(4.95, 4.94, 4.96))
    r.price("ABCD", 4.95)
    r.view = DeskView(r.app, snapshot_ttl=0, slow_ttl=0)
    r.log = ActionLog()
    yield r
    r.journal.close()


def run(rig, scenario) -> None:
    async def main() -> None:
        root = page_factory(rig.view, rig.desk, rig.log, alert_sound=True)
        async with user_simulation(root) as user:
            await user.open("/")
            await scenario(user)
    asyncio.run(main())


async def until(cond, tries: int = 40) -> None:
    for _ in range(tries):
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met")


def test_ticket_renders_and_approve_sends_once(rig):
    c = rig.ticket(OPEN_NOW)

    async def scenario(user):
        await user.should_see("ABCD", retries=30)
        await user.should_see("Catalyst pending (Newsie: phase 4)")
        await user.should_see("stub Skeptic (phase 2): no review yet")
        assert len(list(_buttons(user, exclude=("kill",)))) == 2      # Approve + Pass (helper sanity)
        user.find(marker="approve:ABCD").click()
        user.find(marker="approve:ABCD").click()             # double click
        await until(lambda: c.status is S.HUMAN_APPROVED)
        await user.should_see(marker="result:ABCD", content="approved", retries=30)

    run(rig, scenario)
    assert len(rig.submits()) == 6                           # one trade's bracket orders, not two
    assert any("Approve ABCD: approved" in line for line in rig.log.recent())


def test_reduce_size_and_note(rig):
    c = rig.ticket(OPEN_NOW)

    async def scenario(user):
        await user.should_see(marker="size:ABCD", retries=30)
        size = user.find(marker="size:ABCD")
        size.clear().type("10")
        user.find(marker="note:ABCD").type("half size, extended")
        user.find(marker="approve:ABCD").click()
        await until(lambda: c.status is S.HUMAN_APPROVED)

    run(rig, scenario)
    assert c.human.approved_shares == 10 and c.human.note == "half size, extended"


def test_size_above_max_refused_in_page(rig):
    c = rig.ticket(OPEN_NOW)
    max_shares = c.risk_sizing.shares

    async def scenario(user):
        await user.should_see(marker="size:ABCD", retries=30)
        user.find(marker="size:ABCD").clear().type(str(max_shares + 5))
        user.find(marker="approve:ABCD").click()
        await user.should_see(f"size must be 1–{max_shares}", retries=10)

    run(rig, scenario)
    assert c.status is S.AWAITING_HUMAN and rig.submits() == []


def test_pass_with_note(rig):
    c = rig.ticket(OPEN_NOW)

    async def scenario(user):
        await user.should_see(marker="pass:ABCD", retries=30)
        user.find(marker="note:ABCD").type("too extended")
        user.find(marker="pass:ABCD").click()
        await until(lambda: c.status is S.HUMAN_PASSED)

    run(rig, scenario)
    assert c.human.note == "too extended" and rig.submits() == []


def test_approve_disabled_when_loss_limit_hit(rig, monkeypatch):
    c = rig.ticket(OPEN_NOW)
    losing = AccountState(cash=1000, buying_power=1000, realized_pnl_today=-60)
    monkeypatch.setattr(rig.app.pipeline.accounts, "state", lambda now: losing)

    async def scenario(user):
        await user.should_see(marker="banner", content="Daily loss limit hit", retries=30)
        btn = next(iter(user.find(marker="approve:ABCD").elements))
        await until(lambda: not btn.enabled)
        user.find(marker="approve:ABCD").click()             # disabled: the click does nothing
        await asyncio.sleep(0.2)

    run(rig, scenario)
    assert c.status is S.AWAITING_HUMAN and rig.submits() == []


def test_expired_ticket_shows_reason(rig):
    c = rig.ticket(OPEN_NOW)

    async def scenario(user):
        await user.should_see(marker="approve:ABCD", retries=30)
        rig.desk.expire_stale(OPEN_NOW + timedelta(seconds=91))
        await user.should_see(marker="result:ABCD", content="expired", retries=40)

    run(rig, scenario)
    assert c.status is S.EXPIRED


def test_flatten_and_kill_switch_need_confirm(rig):
    c = rig.ticket(OPEN_NOW)
    assert rig.desk.approve(c.id, now=OPEN_NOW).ok
    rig.price("ABCD", 5.01, ask=5.02)
    rig.tm.tick(OPEN_NOW + timedelta(seconds=5))
    assert c.status is S.FILLED

    async def scenario(user):
        await user.should_see(marker="flatten:ABCD", retries=30)
        user.find(marker="flatten:ABCD").click()
        await asyncio.sleep(0.1)
        assert rig.broker.position("ABCD") is not None       # nothing happens until confirmed
        user.find(marker="kill").click()
        await user.should_see(marker="kill-confirm", retries=10)
        user.find(marker="kill-confirm").click()
        await until(lambda: rig.broker.position("ABCD") is None, tries=100)

    run(rig, scenario)
    assert any("KILL SWITCH" in line for line in rig.log.recent())


def test_no_ticket_message(rig):
    async def scenario(user):
        await user.should_see("No tickets waiting.")
        await user.should_see("Flat.")

    run(rig, scenario)


def test_rejected_panel_is_read_only(rig):
    c = rig.ticket(OPEN_NOW)
    rig.price("ABCD", 4.95, bid=4.80, ask=4.96)                 # spread blows out -> re-check rejects
    assert not rig.desk.approve(c.id, now=OPEN_NOW + timedelta(seconds=5)).ok

    async def scenario(user):
        await user.should_see("Rejected today (1)", retries=30)
        await user.should_see("risk recheck")
        await until(lambda: not any(True for _ in _buttons(user, exclude=("kill",))))

    run(rig, scenario)
    assert c.status is S.REJECTED and rig.submits() == []


def _buttons(user, exclude: tuple[str, ...] = ()):
    """Visible buttons other than the header's kill switch (the only one with no ticket/position)."""
    from nicegui import ElementFilter

    with user.client:
        for b in ElementFilter(kind=ui.button):
            if not any(m in b._markers for m in exclude) and b.visible:
                yield b
