from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from tradingdesk.sessions import ET
from tradingdesk.settings import DEFAULT_CONFIG, Settings, load_settings

PAPER_ENV = {"ALPACA_API_KEY": "PKTEST123", "ALPACA_SECRET_KEY": "secret", "ALPACA_PAPER": "true"}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


@pytest.fixture
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    """Build Settings from the real config.yaml plus overrides, with a controlled
    environment (never the developer's real .env or os.environ)."""
    base = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))

    def _make(env: dict[str, str] | None = None, **overrides: Any) -> Settings:
        cfg = _deep_merge(base, overrides)
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        empty_env = tmp_path / "empty.env"
        empty_env.write_text("", encoding="utf-8")
        return load_settings(cfg_path, env_file=empty_env, environ=PAPER_ENV if env is None else env)

    return _make


def et(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=ET)
