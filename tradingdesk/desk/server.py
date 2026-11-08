"""
Serves the Trade Desk and runs the agent loops behind it.

  main thread      NiceGUI / uvicorn (the dashboard)
  "scan" thread    runner.run_loop: Scout -> Manager -> tickets, plus the manage loop
                   (ticket expiry + Trade Manager) it starts

Stopping the server (Ctrl+C) stops both loops. When the session ends the scan
loop exits but the dashboard stays up to show the day's summary.
"""
from __future__ import annotations

import threading

from loguru import logger

from tradingdesk.desk.ui import page_factory
from tradingdesk.desk.view import ActionLog, DeskView
from tradingdesk.runner import App, run_loop

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def serve(app: App, poll_sec: float) -> None:
    from nicegui import app as web
    from nicegui import ui

    cfg = app.settings.config.desk
    if cfg.host not in _LOCAL_HOSTS:
        logger.warning("Trade Desk is listening on {} - anyone on that network can open it", cfg.host)
    view = DeskView(app)
    log = ActionLog()
    stop = threading.Event()

    def scan() -> None:
        try:
            run_loop(app, poll_sec, stop=stop)
        except Exception:
            logger.exception("scan loop crashed")
            app.health.error("scout", "scan loop crashed (see log)")
        logger.info("scan loop stopped; Trade Desk stays up")

    def start() -> None:
        threading.Thread(target=scan, name="scan", daemon=True).start()
        logger.success("Trade Desk: {}", app.desk.url)

    web.on_startup(start)
    web.on_shutdown(stop.set)
    ui.run(
        page_factory(view, app.desk, log, cfg.alert_sound),
        host=cfg.host, port=cfg.port, title="Trade Desk", favicon="📈", dark=None,
        reload=False, show=cfg.open_browser, show_welcome_message=False,
    )
