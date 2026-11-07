from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import PAPER_ENV
from tradingdesk.safety import (
    BROKER_PROFILES,
    BrokerSafetyProfile,
    LiveTradingRefused,
    check_paper_only,
    env_is_gitignored,
)
from tradingdesk.settings import PROJECT_ROOT


def test_paper_settings_pass(make_settings):
    report = check_paper_only(make_settings())
    assert report.mode == "paper"
    assert report.violations == []
    assert not report.live_allowed


def test_dry_run_mode_reported(make_settings):
    report = check_paper_only(make_settings(broker={"dry_run": True}))
    assert report.mode == "dry-run"


@pytest.mark.parametrize(
    "env_change, expected",
    [
        ({"ALPACA_API_KEY": "AKLIVEKEY"}, "LIVE key"),
        ({"ALPACA_PAPER": "false"}, "ALPACA_PAPER is false"),
        ({"ALPACA_PAPER": "0"}, "ALPACA_PAPER is false"),
        ({"ALPACA_BASE_URL": "https://api.alpaca.markets"}, "api.alpaca.markets"),
        ({"ALPACA_BASE_URL": "https://api.alpaca.markets/v2"}, "api.alpaca.markets"),
    ],
)
def test_live_env_refused(make_settings, env_change, expected):
    with pytest.raises(LiveTradingRefused) as exc:
        check_paper_only(make_settings(env={**PAPER_ENV, **env_change}))
    assert any(expected in v for v in exc.value.violations)


def test_paper_base_url_is_fine(make_settings):
    env = {**PAPER_ENV, "ALPACA_BASE_URL": "https://paper-api.alpaca.markets"}
    assert check_paper_only(make_settings(env=env)).violations == []


def test_config_paper_false_refused(make_settings):
    with pytest.raises(LiveTradingRefused, match="broker.alpaca.paper is false"):
        check_paper_only(make_settings(broker={"alpaca": {"paper": False}}))


def test_dry_run_does_not_excuse_live_key(make_settings):
    with pytest.raises(LiveTradingRefused):
        check_paper_only(make_settings(env={**PAPER_ENV, "ALPACA_API_KEY": "AKLIVE"}, broker={"dry_run": True}))


@pytest.mark.parametrize("value", ["1", "yes", "TRUE ", "True", "on"])
def test_allow_live_requires_exact_true(make_settings, value):
    """Only the exact word 'true' (any case, surrounding spaces trimmed) unlocks live."""
    env = {**PAPER_ENV, "ALPACA_API_KEY": "AKLIVE", "ALLOW_LIVE": value}
    if value.strip().lower() == "true":
        report = check_paper_only(make_settings(env=env))
        assert report.mode == "LIVE" and report.live_allowed
    else:
        with pytest.raises(LiveTradingRefused):
            check_paper_only(make_settings(env=env))


def test_allow_live_cannot_come_from_config(make_settings):
    with pytest.raises(Exception):  # config.yaml rejects unknown keys
        make_settings(broker={"allow_live": True})


def test_unknown_broker_refused(make_settings):
    with pytest.raises(LiveTradingRefused, match="no registered safety profile"):
        check_paper_only(make_settings(broker={"name": "robinhood"}))


def test_broker_without_paper_or_dry_run_refused(make_settings, monkeypatch):
    monkeypatch.setitem(BROKER_PROFILES, "nopaper", BrokerSafetyProfile(False, False))
    with pytest.raises(LiveTradingRefused, match="neither a paper account nor a dry-run"):
        check_paper_only(make_settings(broker={"name": "nopaper"}))


def test_unrecognised_key_prefix_warns_but_runs(make_settings):
    report = check_paper_only(make_settings(env={**PAPER_ENV, "ALPACA_API_KEY": "XX123"}))
    assert report.mode == "paper" and report.warnings


def test_secrets_not_in_repr(make_settings):
    s = make_settings(env={**PAPER_ENV, "ANTHROPIC_API_KEY": "sk-ant-supersecret"})
    assert "supersecret" not in repr(s.secrets)
    assert "PKTEST123" not in repr(s.secrets)


def test_env_is_gitignored():
    assert env_is_gitignored(PROJECT_ROOT)


def test_gitignore_missing(tmp_path: Path):
    assert not env_is_gitignored(tmp_path)


def test_no_code_sets_allow_live():
    """Nothing in the package may set ALLOW_LIVE."""
    pkg = PROJECT_ROOT / "tradingdesk"
    for f in pkg.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert "ALLOW_LIVE=true" not in text.replace(" ", "") or f.name == "safety.py", f
        assert "environ[\"ALLOW_LIVE\"]" not in text, f
