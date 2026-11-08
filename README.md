# TradingDesk

A team of agents around the WTBot 5 Pillars momentum screener. **Paper trading only.
A human approves every trade** on the Trade Desk; nothing reaches the broker before that.

Build plan: [trading_team_build_prompt.md](trading_team_build_prompt.md). Status: phase 3 done (broker, execution, trade management, NiceGUI Trade Desk); phase 4 (Newsie) next.

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
.venv\Scripts\python.exe -m tradingdesk run --once --force-session open   # one scan pass, any time
.venv\Scripts\python.exe -m pytest                           # tests (offline)
.venv\Scripts\python.exe -m pytest tests/broker_contract --paper   # broker contract vs Alpaca PAPER
```

## Trade Desk

`run` opens the dashboard in your browser (`desk.open_browser`). It binds to
`127.0.0.1` by default (`desk.host`); set it to a LAN address only if you want other
machines to reach it. Everything updates live, once a second.

- **Tickets**: everything about the trade in one card, with a countdown. Approve,
  Pass (optional note) or lower the share count. Size can only go down; the stop
  can't be edited. Approve re-checks risk at click time, and double clicks send one order.
- **Open positions**: live P&L, stop, targets, "software-managed stop" and
  "stuck — halted" flags, a Flatten button per position (with confirm).
- **Status bar**: session and countdown, day P&L vs max loss, trades used, LLM spend,
  broker, feed/agent health. Approve buttons turn off for the day at the loss limit.
- **FLATTEN ALL / KILL**: expires waiting tickets, cancels every order, closes everything.
- **Rejected today**: read-only, with reasons. No override.
- New tickets beep (browsers only allow sound after your first click on the page)
  and send a Discord alert.

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
  alerts.py              Discord webhook (notification only)
tests/
  broker_contract/       every Broker must pass these
```
