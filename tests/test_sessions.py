from __future__ import annotations

import pytest

from tests.conftest import et
from tradingdesk.sessions import Phase, SessionClock
from tradingdesk.settings import SessionsConfig

# 2026-11-09 is a Monday
MON = (2026, 11, 9)


@pytest.fixture
def clock() -> SessionClock:
    return SessionClock(SessionsConfig())


@pytest.mark.parametrize(
    "hm, phase",
    [
        ((5, 29), Phase.IDLE),
        ((5, 30), Phase.STARTUP),
        ((6, 0), Phase.PREMARKET),
        ((9, 29), Phase.PREMARKET),
        ((9, 30), Phase.OPEN),
        ((10, 59), Phase.OPEN),
        ((11, 0), Phase.WIND_DOWN),
        ((11, 29), Phase.WIND_DOWN),
        ((11, 30), Phase.ENDED),
        ((15, 0), Phase.ENDED),
    ],
)
def test_phases(clock, hm, phase):
    assert clock.phase_at(et(*MON, *hm)) is phase


def test_weekend_is_idle(clock):
    assert clock.phase_at(et(2026, 11, 7, 10, 0)) is Phase.IDLE  # Saturday


def test_trading_session(clock):
    assert clock.trading_session_at(et(*MON, 7, 0)) == "premarket"
    assert clock.trading_session_at(et(*MON, 10, 0)) == "open"
    assert clock.trading_session_at(et(*MON, 5, 45)) is None
    assert clock.trading_session_at(et(*MON, 11, 0)) is None


def test_no_new_trades_at_cutoff(clock):
    assert clock.accepting_new_trades(et(*MON, 10, 59, 59))
    assert not clock.accepting_new_trades(et(*MON, 11, 0))


def test_first_and_last_minute_blackouts():
    clock = SessionClock(SessionsConfig(no_entries_first_minutes=10, no_entries_last_minutes=15))
    assert not clock.accepting_new_trades(et(*MON, 9, 35))
    assert clock.accepting_new_trades(et(*MON, 9, 40))
    assert not clock.accepting_new_trades(et(*MON, 6, 5))       # premarket first 10 min
    assert clock.accepting_new_trades(et(*MON, 10, 44))
    assert not clock.accepting_new_trades(et(*MON, 10, 45))


def test_hold_mode_extends_wind_down():
    clock = SessionClock(SessionsConfig(wind_down_mode="hold"))
    assert clock.phase_at(et(*MON, 13, 0)) is Phase.WIND_DOWN
    assert clock.phase_at(et(*MON, 16, 0)) is Phase.ENDED


def test_next_change(clock):
    assert clock.next_change(et(*MON, 9, 0)) == (Phase.OPEN, et(*MON, 9, 30))
    assert clock.next_change(et(*MON, 10, 0)) == (Phase.WIND_DOWN, et(*MON, 11, 0))


def test_next_change_rolls_over_weekend(clock):
    friday_after = et(2026, 11, 13, 12, 0)
    assert clock.next_change(friday_after) == (Phase.STARTUP, et(2026, 11, 16, 5, 30))


def test_naive_datetime_rejected(clock):
    from datetime import datetime
    with pytest.raises(ValueError):
        clock.phase_at(datetime(2026, 11, 9, 10, 0))


def test_utc_input_converted(clock):
    from datetime import datetime, timezone
    # 15:00 UTC on Nov 9 = 10:00 ET (EST, UTC-5)
    assert clock.phase_at(datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)) is Phase.OPEN
