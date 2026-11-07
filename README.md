# TradingDesk

A team of agents around the WTBot 5 Pillars momentum screener. **Paper trading only.
A human approves every trade** on the Trade Desk; nothing reaches the broker before that.

Build plan: [trading_team_build_prompt.md](trading_team_build_prompt.md). Status: phase 3a done (broker, execution, trade management); 3b (NiceGUI Trade Desk) next.

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
.venv\Scripts\python.exe -m tradingdesk run                  # live loop (paper)
.venv\Scripts\python.exe -m tradingdesk run --once --force-session open   # one pass, any time
.venv\Scripts\python.exe -m pytest                           # tests (offline)
.venv\Scripts\python.exe -m pytest tests/broker_contract --paper   # broker contract vs Alpaca PAPER
```

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
  alerts.py              Discord webhook (notification only)
tests/
  broker_contract/       every Broker must pass these
```
