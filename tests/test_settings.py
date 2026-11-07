from __future__ import annotations

from datetime import time

import pytest
from pydantic import ValidationError

from tests.conftest import PAPER_ENV
from tradingdesk.settings import load_settings


def test_real_config_loads(make_settings):
    s = make_settings()
    c = s.config
    assert c.sessions.open == time(9, 30)
    assert c.sessions.new_trade_cutoff == time(11, 0)
    assert c.risk.premarket.max_risk_per_trade_pct < c.risk.open.max_risk_per_trade_pct
    assert c.risk.premarket.max_spread_pct <= c.risk.open.max_spread_pct
    assert c.desk.host == "127.0.0.1"
    assert c.broker.alpaca.paper is True


def test_typo_in_config_fails(make_settings):
    with pytest.raises(ValidationError):
        make_settings(risk={"max_daily_los_usd": 10})


def test_session_times_must_be_ordered(make_settings):
    with pytest.raises(ValidationError, match="strictly increasing"):
        make_settings(sessions={"open": "05:00"})


def test_newsfilter_auto_follows_key(make_settings):
    assert not make_settings().source_enabled("news", "newsfilter")
    on = make_settings(env={**PAPER_ENV, "NEWSFILTER_API_KEY": "nf-key"})
    assert on.source_enabled("news", "newsfilter")


def test_alpaca_news_off_by_default(make_settings):
    s = make_settings()
    assert not s.source_enabled("news", "alpaca_news")
    assert s.source_enabled("news", "yahoo")
    assert s.source_enabled("news", "edgar")


def test_unknown_source_is_off(make_settings):
    assert not make_settings().source_enabled("news", "benzinga")


def test_env_overrides_dotenv_file(tmp_path, make_settings):
    make_settings()  # writes tmp config.yaml
    env_file = tmp_path / ".env"
    env_file.write_text("ALPACA_API_KEY=PKFROMFILE\nALPACA_PAPER=true\n", encoding="utf-8")
    from_file = load_settings(tmp_path / "config.yaml", env_file=env_file, environ={})
    assert from_file.secrets.alpaca_api_key == "PKFROMFILE"
    overridden = load_settings(tmp_path / "config.yaml", env_file=env_file, environ={"ALPACA_API_KEY": "PKENV"})
    assert overridden.secrets.alpaca_api_key == "PKENV"


def test_pricing_present_for_configured_models(make_settings):
    llm = make_settings().config.llm
    assert llm.claude.newsie_model in llm.pricing
    assert llm.claude.skeptic_model in llm.pricing
