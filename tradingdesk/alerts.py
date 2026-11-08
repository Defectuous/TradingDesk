"""
Discord alerts (notification only; nothing can be approved from Discord).

Posts run on a background thread so a slow webhook never blocks trading.
Retries are capped and honour Discord's rate-limit retry_after.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any, Protocol

import requests
from loguru import logger

from tradingdesk.models import Candidate


class Notifier(Protocol):
    def send(self, content: str, embed: dict[str, Any] | None = None) -> None: ...


class NullNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, content: str, embed: dict[str, Any] | None = None) -> None:
        self.sent.append(content)


class DiscordNotifier:
    def __init__(self, webhook_url: str, max_retries: int = 2) -> None:
        self.url = webhook_url
        self.max_retries = max_retries
        self.last_error: str | None = None      # shown on the Trade Desk health row
        self._q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=200)
        threading.Thread(target=self._worker, name="discord", daemon=True).start()

    def send(self, content: str, embed: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"content": content[:1900], "username": "TradingDesk"}
        if embed:
            payload["embeds"] = [embed]
        try:
            self._q.put_nowait(payload)
        except queue.Full:
            logger.warning("discord queue full, dropping alert")

    def _worker(self) -> None:
        while True:
            payload = self._q.get()
            for attempt in range(self.max_retries + 1):
                try:
                    r = requests.post(self.url, json=payload, timeout=10)
                    if r.status_code == 429:
                        time.sleep(min(float(r.json().get("retry_after", 1)), 10))
                        continue
                    if r.status_code >= 400:
                        logger.warning("discord HTTP {}: {}", r.status_code, r.text[:120])
                        self.last_error = f"HTTP {r.status_code}"
                    else:
                        self.last_error = None
                    break
                except requests.RequestException as exc:
                    logger.warning("discord post failed ({}): {}", attempt + 1, exc)
                    self.last_error = str(exc)[:120]
                    time.sleep(1 + attempt)


def ticket_message(c: Candidate, desk_url: str) -> str:
    t, r, n = c.proposed_trade, c.risk_sizing, c.news
    grade = f"Catalyst {n.catalyst_grade} · {n.catalyst_type}" if n else "Catalyst pending"
    badge = " [PREMARKET]" if c.session == "premarket" else ""
    soft = " · stop is software-managed" if r and r.software_managed_stop else ""
    return (f"🔔 **{c.ticker}**{badge} — {grade} — R:R {t.risk_reward} — entry {t.entry} / stop {t.stop} / "
            f"target {t.target} — {r.shares} sh, ${r.dollars_at_risk} at risk{soft}\n{desk_url}")
