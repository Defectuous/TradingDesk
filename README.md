# TradingDesk

A team of agents around the WTBot 5 Pillars momentum screener. **Paper trading only.
A human approves every trade** on the Trade Desk; nothing reaches the broker before that.

Build plan: [trading_team_build_prompt.md](trading_team_build_prompt.md). Status: phase 5 done (Sentiment: StockTwits via its official MCP server, promo checks, market regime); phase 6 (Skeptic) next.

## Setup

```powershell
py -3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env      # then fill in keys
```

- Secrets go in `.env`. Everything else goes in `config.yaml`.
- Startup refuses to run if anything points at live trading, unless `ALLOW_LIVE=true` is set.

## Run

```powershell
.venv\Scripts\python.exe -m tradingdesk                      # startup checks
.venv\Scripts\python.exe -m tradingdesk run                  # agents + Trade Desk at http://127.0.0.1:8765
.venv\Scripts\python.exe -m tradingdesk run --force-session open --cash 1000   # desk outside market hours
.venv\Scripts\python.exe -m tradingdesk run --no-desk         # console only (can't approve)
.venv\Scripts\python.exe -m tradingdesk stocktwits-login      # one-time browser login for Sentiment
.venv\Scripts\python.exe -m tradingdesk run --once --force-session open   # one scan pass, any time
.venv\Scripts\python.exe -m pytest                           # tests (offline)
.venv\Scripts\python.exe -m pytest tests/broker_contract --paper   # broker contract vs Alpaca PAPER
```

## Trade Desk

`run` opens the dashboard in your browser (`desk.open_browser`). It binds to
`127.0.0.1` by default (`desk.host`); set it to a LAN address only if you want other
machines to reach it. Everything updates live, once a second.

- **Tickets**: everything about the trade in one card, with a countdown. Approve,
  Pass (optional note) or lower the share count. Approve opens a confirmation with the
  shares, levels and $ at risk; nothing is sent until you confirm. Size can only go down; the stop
  can't be edited. Approve re-checks risk at click time, and double clicks send one order.
- **Open positions**: live P&L, stop, targets, "software-managed stop" and
  "stuck — halted" flags, a Flatten button per position (with confirm).
- **Status bar**: session and countdown, day P&L vs max loss, trades used, LLM spend,
  broker, feed/agent health. Approve buttons turn off for the day at the loss limit.
- **FLATTEN ALL / KILL**: expires waiting tickets, cancels every order, closes everything.
- **Rejected today**: read-only, with reasons. No override.
- New tickets beep (browsers only allow sound after your first click on the page)
  and send a Discord alert.

## Newsie

Runs in the background during Startup / Premarket / Open; the Scout and Manager only read its cache.

1. **Watchlist**: the Scout's top movers each pass (and premarket movers during Startup), biggest first.
2. **Sources** (each fails on its own): Yahoo Finance, Google News RSS, SEC EDGAR. Alpaca News and
   Newsfilter are built but off. EDGAR needs `SEC_USER_AGENT` in `.env`.
3. **Dedup** across sources (same URL or ≥90% similar titles), earliest time kept, other sources listed.
4. **Free filters**: WTBot's junk-stub and roundup rules, then qwen (local) drops headlines that aren't about
   the ticker or aren't material. It fails open (keeps everything) if the server is down.
5. **Grade only when the relevant headlines change**: Claude (`llm.claude.newsie_model`, structured JSON,
   cached system prompt, effort low). Over the daily spend cap, or if Claude fails: WTBot's hints2 local
   setup. If that fails too: keyword rules.
6. **Rubric rules enforced in code**: news older than the session (before the previous close) drops one
   grade; an offering filed today (EDGAR offering forms / 8-K 3.02, or an offering headline) caps at D.
7. Grades go to the cache and the Journal (`news_grades`), so a restart doesn't pay twice. Every LLM call is
   logged with tokens and cost (`llm_usage`).

On tickets: the grade badge (with "(local)" / "(rules)" when Claude didn't grade it), the one-sentence
reason, each headline's grade, publisher and which sources carried it, SEC filings, and filing flags.

## Sentiment

Runs alongside Newsie (Startup / Premarket / Open), only for tickers on the Scout's radar.

- **StockTwits**, through StockTwits' official MCP server (https://ai.stocktwits.com) with your account:
  one `get_symbol_pulse` call per ticker per poll (sentiment score, chatter level now vs. normal, watchers,
  newest posts). That's the "approved API" route StockTwits' Terms (section 5) allow; it never posts.
  Log in once with `python -m tradingdesk stocktwits-login`; the token lives in `data/` and refreshes itself.
  Rate-limited? It rests (retry-after, or 5 min; an hour after three in a row).
- **Promotion checks** (plain Python): near-identical posts, one account flooding the stream, pump language,
  new accounts when known. A promo warning discounts buzz and is flagged for the Skeptic, never as bullish.
- **Tone**: qwen labels recent posts bullish / bearish / neutral (free, local; skipped if the server is down).
- **Buzz** = chatter level (0-100) discounted by the promo share; **trend** = rising / flat / fading vs. the
  reading 10 minutes ago (or StockTwits' day level before that).
- **Market regime** (plain Python): SPY / QQQ / IWM vs. their 20-day average and today's move, plus the VIX
  (from Yahoo; Alpaca has no index data). Every ticket gets it, even without a social reading.
- **Not wired in**: X (paid API, see the cost note in ROADMAP), Reddit (needs Reddit's approval).

## Layout

```
config.yaml              limits, sessions, thresholds, models, endpoints, sources
tradingdesk/
  settings.py            typed config.yaml + .env loader
  safety.py              paper-only startup check
  models.py              Candidate (the contract between agents) and stage records
  sessions.py            session clock: Startup / Premarket / Open / Wind-down / Ended
  journal/store.py       SQLite journal: candidates, verdicts, trade events, LLM usage
  llm/costs.py           LLM usage records and cost estimates
  agents/                scout, breakout setup, manager, skeptic (stub), risk_officer
  legacy/wtbot/          WTBot code reused unchanged (5 Pillars, Alpaca data, levels, float)
  broker/                Broker interface, AlpacaBroker (paper only), DryRunBroker
  execution/             executor, trade_manager, closer (marketable-limit close + retries)
  desk/service.py        Trade Desk logic: expiry, approve/pass, click-time re-check, kill switch
  desk/view.py           what the dashboard shows, as plain data (snapshots, health, chart SVG)
  desk/ui.py             the NiceGUI page: renders snapshots, buttons call desk/service.py
  desk/server.py         serves the dashboard; runs the scan + manage loops in background threads
  health.py              heartbeats for the status bar's feed/agent health
  social/                Sentiment: stocktwits_mcp (official MCP + OAuth login), stocktwits source, spam
                         (promo checks), tone (qwen), regime (SPY/QQQ/IWM + VIX), sentiment (watchlist + loop)
  watchlist.py           movers the background agents research
  news/                  Newsie: sources, edgar, dedup, flags (WTBot keyword rules), local_llm (Ollama),
                         grader (Claude / local / rules + relevance filter), newsie (watchlist + loop)
prompts/
  catalyst_rubric.md     the A-F rubric Newsie grades with (edit freely)
  catalyst_types.md      WTBot's catalyst type guide (from its Claude classifier prompt)
  local_catalyst_hints2.md  WTBot's best local-model prompt, byte for byte (used when the spend cap is hit)
  alerts.py              Discord webhook (notification only)
tests/
  broker_contract/       every Broker must pass these
```
