"""
Entry point.  python -m tradingdesk [check]

Phase 1 only runs the startup checks: load config + .env, paper-only safety
check, open the Journal, and report the current session phase. Later phases
add the agent loop and the Trade Desk.
"""
from __future__ import annotations

import argparse
import sys

from loguru import logger

from tradingdesk.journal import Journal
from tradingdesk.logging_setup import setup_logging
from tradingdesk.safety import LiveTradingRefused, check_paper_only, env_is_gitignored
from tradingdesk.sessions import SessionClock, now_et
from tradingdesk.settings import Settings, load_settings


def _fmt_delta(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def startup_checks(settings: Settings) -> int:
    try:
        report = check_paper_only(settings)
    except LiveTradingRefused as exc:
        logger.critical(str(exc))
        return 2
    logger.info("Broker: {} ({})", report.broker, report.mode)

    if not env_is_gitignored(settings.root):
        logger.warning(".env is not listed in .gitignore")

    journal_path = settings.path(settings.config.paths.journal_db)
    with Journal(journal_path) as j:
        today = now_et().date().isoformat()
        logger.info("Journal: {} - LLM spend today ${:.4f} of ${:.2f} cap",
                    journal_path, j.llm_spend(today), settings.config.llm.daily_spend_cap_usd)

    clock = SessionClock(settings.config.sessions)
    now = now_et()
    phase = clock.phase_at(now)
    nxt, at = clock.next_change(now)
    logger.info("Session: {} - {} in {} (at {:%a %H:%M} ET)",
                phase.label, nxt.label, _fmt_delta((at - now).total_seconds()), at)

    news = [n for n in settings.config.news.sources if settings.source_enabled("news", n)]
    social = [n for n in settings.config.sentiment.sources if settings.source_enabled("sentiment", n)]
    logger.info("News sources on: {}", ", ".join(news) or "none")
    logger.info("Social sources on: {}", ", ".join(social) or "none")
    llm = settings.config.llm
    logger.info("LLMs: Newsie={} Skeptic={} local={} @ {}",
                llm.claude.newsie_model, llm.claude.skeptic_model, llm.local.model, llm.local.base_url)

    missing = [k for k, v in {
        "ALPACA_API_KEY": settings.secrets.alpaca_api_key,
        "ANTHROPIC_API_KEY": settings.secrets.anthropic_api_key,
        "DISCORD_WEBHOOK_URL": settings.secrets.discord_webhook_url,
        "SEC_USER_AGENT": settings.secrets.sec_user_agent,
    }.items() if not v]
    if missing:
        logger.warning("Not set in .env: {}", ", ".join(missing))
    logger.success("Startup checks passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tradingdesk")
    parser.add_argument("command", nargs="?", default="check", choices=["check", "run"],
                        help="check = startup checks only; run = Scout -> Manager -> tickets loop")
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--once", action="store_true", help="run one scan pass and exit")
    parser.add_argument("--force-session", choices=["premarket", "open"], default=None,
                        help="pretend we're in this session (testing outside market hours)")
    parser.add_argument("--max-symbols", type=int, default=None, help="cap the universe (testing)")
    parser.add_argument("--cash", type=float, default=None,
                        help="use this cash amount instead of reading the Alpaca account (testing)")
    args = parser.parse_args(argv)

    settings = load_settings(args.config)
    setup_logging(settings.path(settings.config.paths.log_dir), args.log_level)
    rc = startup_checks(settings)
    if rc or args.command == "check":
        return rc

    from tradingdesk.account import StaticCash
    from tradingdesk.market import WtbotMarketData
    from tradingdesk.runner import build_app, run_loop

    with Journal(settings.path(settings.config.paths.journal_db)) as journal:
        app = build_app(
            settings, journal, market=WtbotMarketData(max_symbols=args.max_symbols),
            force_session=args.force_session, cash=StaticCash(args.cash) if args.cash else None,
        )
        logger.info("Broker: {} (paper={}, dry_run={})", app.broker.name,
                    app.broker.capabilities().paper, app.broker.capabilities().dry_run)
        run_loop(app, settings.config.scout.poll_interval_sec, once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
