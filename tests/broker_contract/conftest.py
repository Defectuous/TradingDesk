from __future__ import annotations

import pytest

from tradingdesk.broker.dry_run import DryRunBroker


def _brokers(config: pytest.Config) -> list[str]:
    return ["dry_run", "alpaca_paper"] if config.getoption("--paper") else ["dry_run"]


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "broker" in metafunc.fixturenames:
        metafunc.parametrize("broker", _brokers(metafunc.config), indirect=True)


@pytest.fixture
def broker(request: pytest.FixtureRequest):
    if request.param == "dry_run":
        b = DryRunBroker(cash=1000)
        b.set_quote("F", 10.00, bid=9.99, ask=10.01)
        return b
    from tradingdesk.broker.alpaca import AlpacaBroker
    from tradingdesk.safety import check_paper_only
    from tradingdesk.settings import load_settings

    settings = load_settings()
    check_paper_only(settings)               # never run the contract against anything live
    return AlpacaBroker(settings.secrets.alpaca_api_key, settings.secrets.alpaca_secret_key)


@pytest.fixture
def regular_hours_only(broker):
    if not broker.clock().is_open:
        pytest.skip("round trip needs the regular session")
