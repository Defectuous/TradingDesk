# Build Prompt: Multi-Agent Trading Team

Paste everything below the line into Claude in VS Code, or save this file in the project root and tell Claude: "Read trading_team_build_prompt.md and follow it."

---

## Your role

You are helping me restructure my existing Python momentum day-trading bot into a team of specialized agents. Work in phases. **Stop at the end of each phase**, summarize what you built, show me how to run it, and wait for my go-ahead before starting the next phase.

## Before writing any code

1. Read the existing project and give me a short map of what's already there: entry point, how Alpaca is used, how Newsfilter.io is used, where the Claude API catalyst classification lives, and where the 5 Pillars screener logic lives.
2. Point out anything that would conflict with the plan below.
3. Propose the folder structure you'll use, reusing my existing code wherever it fits. Do not rewrite working code just to restyle it.
4. Wait for my approval.

## What already exists (verify against the code)

- Python project, edited in VS Code
- Alpaca Markets for market data (and trading)
- Newsfilter.io for news — **but my subscription is in limbo** (I have a support ticket open). Treat Newsfilter as optional: the system must run fully without it and pick it up automatically if I add a key later.
- Anthropic Claude API for news catalyst classification
- Ross Cameron's 5 Pillars momentum screener
- I've had a spike in Claude API token usage before, so cost control matters
- I have a home Ubuntu server ("defectserver", Tesla V100 16GB) that can run a local Qwen 14B model. Treat it as an OpenAI-compatible endpoint whose URL comes from config. Ask me for details if you need them.

## Ground rules (non-negotiable)

- **A human starts every trade.** The system never opens a position on its own. It prepares a complete trade ticket and presents it on the Trade Desk; nothing is sent to the broker until I click Approve. After approval, execution and trade management are fully automated.
- **Paper trading only.** The Alpaca client must be created with paper trading on. Add a startup check that refuses to run if a live-trading key or URL is detected unless an explicit `ALLOW_LIVE=true` flag is set. Do not set that flag anywhere yourself.
- **Two lanes.** Scout, Risk Officer, Executor, Manager orchestration and Journal are plain Python, no LLM calls. Only Newsie, Sentiment and Skeptic use LLMs.
- **No LLM calls in the hot path.** Newsie and Sentiment score tickers in the background and cache results. When the Scout flags a mover, the Manager reads cached scores; it never waits on a fresh LLM call except the single Skeptic call.
- **Gates can only say no.** Skeptic and Risk Officer can reject a candidate but cannot modify or force-approve one. Risk Officer has final veto and nothing overrides it.
- **All secrets in `.env`**, loaded with python-dotenv. Never hardcode keys. Make sure `.env` is in `.gitignore`.
- **All config in one place** (`config.yaml` or a settings module): limits, thresholds, model names, polling intervals, endpoints.
- Times in US/Eastern. Respect market hours, premarket and halts.
- Type hints throughout, logging (not print), and graceful handling of API errors with capped retries and backoff. Retries must never multiply LLM spend.

## My trading session

I work overnight and trade after my shift. All times below go in config so I can change them.

- **~5:30 ET: startup.** Health checks, load limits, Newsie and Sentiment start building the cache (overnight headlines, fresh EDGAR filings, chatter). No tickets yet.
- **6:00–9:30 ET: premarket session.** The Scout runs on premarket gappers and tickets can appear. Premarket has thin volume, wide spreads and different order rules, so:
  - Use stricter premarket settings in config: tighter max spread, smaller max position size / dollars at risk, higher minimum volume.
  - Check what Alpaca (and the `Broker` capabilities) allows in extended hours. I believe extended-hours orders are limited (e.g., limit orders only, and possibly no native bracket orders), but verify against current Alpaca docs. If brackets aren't supported, the Trade Manager must manage the stop and target in software, and the Trade Desk ticket must clearly show "stop is software-managed."
  - Tickets show a PREMARKET badge.
- **9:30–11:00 ET: open session.** Normal settings. This is the main window.
- **11:00 ET: hard stop for new trades.** No new tickets after this; any ticket still waiting expires. Open positions keep being managed by the Trade Manager.
- **At 11:00, also cancel unfilled entry orders** so a late fill can't open a new position.
- **Position wind-down:** configurable time (default 11:30 ET) by which the Trade Manager closes anything still open, so nothing is left running after I've stepped away. I can switch this to "hold until stop/target/close" in config. Optional config setting: tighten stops between 11:00 and the wind-down time to lock in gains (off by default).
  - For each open position, in this order: (1) cancel its attached stop and target orders first, so they can't fire after the close and leave me accidentally short; (2) close with a marketable limit order priced just through the bid (not a plain market order, which can fill badly on thin small caps); (3) if unfilled, re-price and retry every few seconds, falling back to a market order after a configurable number of tries; (4) confirm with the broker that the position is actually flat.
  - Handle partial fills by working the remaining shares the same way.
  - **Halted stocks** can't be closed: keep retrying when the halt lifts, show the position as "stuck — halted" on the Trade Desk, and send a Discord alert so I know something is still open.
  - Log each close with reason `wind-down` so the Journal can compare wind-down exits to normal ones.
  - When done, send a Discord summary (positions closed, any still open and why, day P&L).
- **Shutdown:** after the last position closes, Newsie and Sentiment stop polling (to save API spend) and the Journal writes the day's summary. The Trade Desk shows "Session ended" with the summary.
- The Trade Desk status bar shows which session it is (Startup / Premarket / Open / Wind-down / Ended) and a countdown to the next change.
- The Skeptic's time-of-day check should know these windows (premarket liquidity, the open's volatility, late-morning fade).
- The Journal report should break results down by session (premarket vs open) and by time of day, so I can see whether premarket is worth trading and whether my decisions get worse later in the morning.

## Architecture

```
Market data ──> Scout ───────┐
News + EDGAR ─> Newsie ──────┼──> Manager ──> Skeptic ──> Risk Officer ──> TRADE DESK ──(I click Approve)──> Risk re-check ──> Executor ──> Trade Manager
Social + tape > Sentiment ───┘                  │              │              │                                  │                              │
                                                └──────────────┴──────────────┴────────────> Journal <───────────┴──────────────────────────────┘
```

### Candidate file (the contract between agents)

Define a pydantic model, e.g. `Candidate`, that moves down the chain. Each stage appends its verdict; nothing overwrites earlier stages. Suggested fields:

- `ticker`, `created_at`, `status` (spotted / researched / challenged / risk_cleared / awaiting_human / human_approved / human_passed / expired / rejected / filled / closed)
- Human decision: `human_decision`, `decided_at`, seconds from ticket shown to decision, optional note I typed
- Scout data: price, % change, relative volume, float, the 5 Pillars checks passed/failed
- Newsie data: catalyst headline(s), catalyst type label, catalyst grade (A–F) with a one-sentence reason, recent filings flags (offering / dilution / insider activity)
- Sentiment data: buzz score and trend (rising/flat/fading), market regime (supportive / neutral / hostile)
- Proposed trade: entry, stop, target, risk/reward ratio, share size
- `verdicts`: list of `{agent, decision, reasons, timestamp}`
- `rejected_by` and `rejection_reasons` if rejected

### Agents

**Scout (plain Python)**
- Wrap my existing 5 Pillars screener. Do not change its logic without asking.
- Emits a `Candidate` for each ticker that passes, with a cooldown so the same ticker isn't re-emitted every tick.

**Newsie (LLM, background)**
- **Pluggable news sources.** Define a common `NewsSource` interface (fetch headlines for a ticker or the market, return a normalized `Headline` with source, ticker(s), title, summary, URL, published time). Each source is enabled/disabled in config and fails independently without stopping the others. Sources:
  - **Alpaca News API** — optional, **disabled by default**. I haven't gotten it working reliably yet, so the system must not depend on it. Build the source class so it can be switched on in config later; if you spot why my current Alpaca news code isn't working while mapping the project, tell me, but don't make it a blocker.
  - **Yahoo Finance** — there is no official news API. Use per-ticker RSS feeds and/or the unofficial `yfinance` news data. Treat it as best-effort: expect format changes and rate limits, and handle failures quietly.
  - **Google Finance** — has no news API. Use Google News RSS search per ticker/company name as the equivalent.
  - **SEC EDGAR** — filings, as below.
  - **Newsfilter.io** — optional. Keep my existing integration, wrapped in the same interface, **disabled by default**, and auto-enabled only when a Newsfilter key is present in `.env`.
  - Design it so adding another source later (e.g., Benzinga, Finnhub) is just a new class plus a config entry.
- **Deduplicate** headlines across sources (same story from several outlets) before any LLM sees them, using URL plus normalized-title similarity, and keep the earliest timestamp. Never pay to grade the same story twice.
- Respect each site's terms of service and rate limits. No scraping that requires bypassing logins, paywalls or bot protection. If a source can't be accessed legitimately, tell me instead of working around it.
- EDGAR forms to flag: 8-K (catalysts), S-1, S-3, 424B* (offerings/dilution), Form 4 (insider buys/sells). Use EDGAR's public endpoints with a proper User-Agent header and respect their rate limits.
- Uses the **local Qwen model** for first-pass filtering (is this headline relevant and material at all?) and **Claude** only for grading headlines that pass the filter. Reuse my existing Claude classification code where possible.
- Caches results per ticker with a timestamp; doesn't re-grade the same headline twice.
- **Catalyst grading scale.** Grade each ticker's catalyst A–F with this rubric (keep it in config or a prompt file so I can tweak it), and return a short catalyst type label alongside the grade (e.g., FDA, Contract, Buyout, Earnings, Partnership, Trial data, Offering, None):
  - **A — major, fresh, company-changing:** FDA approval / Fast Track / Breakthrough designation, large contract with a named partner, buyout or merger offer, earnings that blow past expectations with raised guidance. Published today, credible source.
  - **B — solid but smaller:** partnership, patent, positive trial data, earnings beat, notable analyst upgrade.
  - **C — weak or vague:** "exploring strategic alternatives," conference appearance, minor product update, vague letter to shareholders.
  - **D — recycled or promotional:** old news reposted, paid-promotion articles, press releases with no substance.
  - **F — none or negative:** no news found for the move, or the news is bearish (offering, reverse split, delisting notice, failed trial).
  - Freshness counts: news older than the current session drops at least one grade. An offering filed the same day caps the grade at D no matter what else is out.
  - Store the reasoning in one sentence so I can see why it got that grade.

**Sentiment (LLM, background)**
- Two outputs: (1) crowd buzz — mention velocity and tone across social media; (2) market regime — computed in plain Python from SPY, QQQ, IWM trend and VIX level.
- **Pluggable social sources**, same pattern as news: a common `SocialSource` interface returning normalized posts (platform, ticker, text, author follower count if available, timestamp, engagement), each enabled/disabled in config and failing independently. Sources to support:
  - **X (x.com)** — required. Use the official X API. Access is paid and tiered, so before building, check the current API tiers, prices and rate limits and tell me what's realistic for cashtag searches (e.g., `$TICKER`) at my scale. Design the X source to poll only tickers that are already on the Scout's radar to keep usage low.
  - **StockTwits** and **Reddit** (e.g., r/wallstreetbets, r/pennystocks, r/Daytrading) via their official APIs.
  - Build it so other platforms can be added later as new classes.
- Weight buzz by account reach and penalize obvious spam/bot patterns (brand-new accounts, identical posts, pump language). A spike in coordinated promotion is a warning for the Skeptic, not a bullish signal.
- Respect each platform's terms of service and rate limits; no scraping that gets around logins or bot protection. If a platform can't be accessed legitimately or affordably, tell me rather than working around it.
- Uses the local Qwen model for tone scoring. Regime is pure code.
- Caches per ticker.

**Manager (plain Python orchestrator)**
- A state machine, not an LLM. Takes a Scout candidate, attaches cached Newsie and Sentiment data, computes the proposed entry/stop/target and risk/reward from the Scout data, then passes it to Skeptic → Risk Officer → Trade Desk in order.
- Any stage rejecting ends the chain; the candidate goes to the Journal either way.
- The Manager never calls the Executor directly. Only an Approve action from the Trade Desk can.

**Skeptic (LLM, one Claude call per candidate)**
- Its only job is to argue against the trade. Prompt it adversarially: "Find the strongest reasons this trade will fail."
- Must work through this checklist and return structured JSON (`decision: reject|pass`, `reasons: [...]`, `checks: {...}`):
  - Dilution risk: recent or open shelf registration, offering filings, history of offerings
  - Extension: how far price has already run from the open / VWAP / premarket high
  - Spread and liquidity
  - Time of day (e.g., late-day momentum is weaker)
  - Halt risk
  - Catalyst quality: is the news real and fresh, or recycled / promotional?
  - Risk/reward from the *current* price, not the ideal entry
  - Market regime from Sentiment
- Some of these checks can be computed in code first and passed in as facts; only use the LLM for the judgment part.
- Default to reject if the response fails to parse.

**Risk Officer (plain Python, final veto)**
- Configurable hard limits, for example: max dollars risked per trade, max position size, max daily loss (stop trading for the day when hit), max trades per day, max open positions, no trading in the first N minutes or last N minutes if configured, minimum risk/reward. Limits can differ per session (premarket vs open), and nothing is approved outside the session windows in "My trading session".
- Computes final share size from the stop distance and the per-trade risk limit.
- Cannot be bypassed by any other component.

**Trade Desk (the human control point)**
- A local web dashboard, served only on my machine/LAN (bind to localhost by default). Suggest a stack (e.g., FastAPI + a simple HTML/JS front end with live updates, or NiceGUI/Streamlit) and explain the tradeoff before building. It must update live without manual refresh.
- **Ticket queue:** each risk-cleared candidate appears as a ticket showing everything in one view:
  - Ticker, price, % change, relative volume, float, 5 Pillars checklist
  - Catalyst grade badge in the ticket header shown as grade + type (e.g., "Catalyst A · FDA"), with the one-sentence grading reason
  - Catalyst headline(s) with grade, and filing flags (offering / dilution / insider)
  - Buzz trend and market regime
  - Proposed entry, stop, target, risk/reward, share size, dollars at risk
  - The Skeptic's objections, even though it passed — I want to see the case against
  - A small intraday chart if practical
  - A countdown showing how long the ticket stays valid
- **Actions per ticket:** Approve, Pass (with an optional one-line note), and reduce size (smaller only, never larger). I cannot edit the stop to widen risk.
- **Ticket expiry:** tickets auto-expire after a configurable number of seconds, or immediately if price moves past a configurable distance from the proposed entry. Expired tickets are journaled as `expired`.
- **Re-check at click time:** when I click Approve, re-run the Risk Officer against the current price, account state and daily limits before sending anything. If it fails now, show why and don't trade. Approve buttons must be idempotent (double-clicking can never send two orders).
- **Rejected panel:** a collapsible list of candidates the Skeptic or Risk Officer killed, with reasons. Read-only; there is no override button.
- **Open positions panel:** live P&L, current stop and target, and a Flatten button per position plus a Flatten All / kill switch that cancels open orders and closes everything.
- **Status bar:** daily P&L vs max daily loss, trades used vs max per day, LLM spend vs daily cap, and health of each data feed and agent. If the daily loss limit is hit, the Approve buttons disable for the rest of the day.
- **Alerts:** when a new ticket arrives, play a sound in the dashboard and send a Discord webhook message (webhook URL in `.env`) with the ticker, catalyst grade, R:R and a link to the dashboard. Discord is notification only; no approving from Discord in this version.

**Broker layer (build now, even though only Alpaca is implemented)**
- Define a `Broker` interface that the Executor, Trade Manager, Risk Officer and Trade Desk talk to — never a broker SDK directly. Methods along the lines of: account info (buying power, equity, day-trade count), positions, open orders, place bracket order, modify stop/target, cancel, flatten position, flatten all, order/fill updates, and `capabilities()`.
- `capabilities()` reports what that broker supports: paper trading, native bracket/OCO orders, short selling, fractional shares, extended hours, streaming fills. If a broker lacks native brackets, the Trade Manager must simulate them safely (and the Trade Desk should flag that the stop is software-managed, not held at the broker).
- `AlpacaBroker` is the only implementation for now. Selecting the broker is a config setting.
- Paper-only rule becomes per broker: every broker must offer either a real paper account or a dry-run mode that logs orders without sending them. A broker with neither cannot be enabled without `ALLOW_LIVE=true`.
- Write a reusable contract test suite that any future `Broker` implementation must pass against a paper/dry-run account.

**Executor (plain Python)**
- Only callable from a Trade Desk approval that passed the click-time Risk Officer re-check.
- Talks to the `Broker` interface only.
- Places **bracket orders** through Alpaca (entry + stop loss + take profit attached from the start).
- Handles partial fills, rejections and cancels; reports fills back to the candidate.
- Paper account only (see ground rules).

**Trade Manager (plain Python, automated after approval)**
- Manages open positions according to configurable rules, for example: move stop to breakeven after +1R, take partial profit at the first target, trail the rest, and close everything by a configured time before the close.
- Every action it takes is logged to the Journal and shown on the Trade Desk.
- Runs automatically; I can always override with Flatten.

**Journal (plain Python)**
- SQLite database. Saves every candidate in its final state — including rejected ones — with every agent's verdict and reasons.
- Also logs LLM usage per call: agent, model, input/output tokens, estimated cost.
- Include a small report script: trades taken vs rejected, win rate, average R, which agent rejected what, and for rejected candidates what price did afterwards (so I can see if the Skeptic was right). Also total LLM spend per day and per agent.
- Also report on my own decisions: tickets I approved vs passed vs let expire, how each group's price action played out afterward, and my average decision time. I want to know whether my clicks add value over the system's picks.

## Cost controls

- Daily LLM spend cap in config. When hit, Newsie and Sentiment fall back to local-model-only and the Skeptic auto-rejects (no trading without the Skeptic).
- Use prompt caching on the Claude API for the static parts of the Skeptic and Newsie prompts.
- Keep prompts tight: pass structured facts, not raw page dumps.
- Log a warning if any single agent's token usage spikes well above its recent average.

## Testing

- Unit tests (pytest) for the Risk Officer limits, share sizing, the Manager state machine, and Skeptic JSON parsing including malformed responses.
- Tests proving no order can be placed without a Trade Desk approval, that approvals are idempotent, that expired tickets can't be approved, that the click-time re-check blocks trades when limits are hit, and that the kill switch cancels and flattens everything.
- A replay / dry-run mode that feeds recorded market data and headlines through the whole pipeline without placing orders, with LLM calls mockable.

## Roadmap (not part of the current build)

Don't build these now. Design the current code so they can be added without restructuring, and keep this list in a `ROADMAP.md` in the repo.

**Additional brokers** (each becomes a new `Broker` implementation that must pass the contract tests):
- **Robinhood** — wanted, but its access for stock trading is unsettled. Its long-standing official public API has been crypto-only, and it has recently been opening some form of AI-agent trading access whose details are unclear. When this item comes up: check Robinhood's current official developer documentation and tell me exactly what's officially supported for stocks. **Only use official, documented access.** Do not use reverse-engineered or unofficial libraries (e.g., ones that log in with my username/password); they break without warning and can get the account restricted.
- **Other brokers with official trading APIs** — candidates to evaluate: Interactive Brokers, Charles Schwab, Tradier, E*TRADE, Webull. When we get here, verify each one's current API access, paper-trading support, bracket order support, costs and approval requirements, and give me a comparison before building any.
- Support for running more than one broker at a time (choose the broker per ticket on the Trade Desk) is a later step after single-broker swapping works.

**Other roadmap ideas** (add to `ROADMAP.md`):
- Approving tickets from my phone (e.g., Discord buttons or a mobile-friendly Trade Desk), with the same click-time risk re-check.
- Re-enabling Alpaca News and Newsfilter.io once they work.
- Additional social platforms beyond X, StockTwits and Reddit.

## Phases

1. **Foundation:** project map, folder structure, config, `.env` handling, the `Candidate` model, Journal (SQLite), paper-only safety check. Tests for the model and the safety check.
2. **Scout + Manager + Risk Officer:** wire my existing screener into the plain-Python lane, with a stub Skeptic that always passes. Candidates stop at `awaiting_human`. Tests for Risk Officer and Manager.
3. **Broker layer + Trade Desk + Executor + Trade Manager:** the `Broker` interface with `AlpacaBroker` and the broker contract tests, the dashboard, ticket expiry, click-time re-check, bracket orders on paper, position management, kill switch, Discord alerts. Tests for the approval path and kill switch.
4. **Newsie:** pluggable news sources (Yahoo Finance, Google News RSS and EDGAR as the working set; Alpaca News and Newsfilter optional and off by default), dedup, local-model filter, Claude grading, caching, cost logging. Show its data on tickets, including which source(s) each headline came from.
5. **Sentiment:** pluggable social sources (X, StockTwits, Reddit), spam weighting, market regime, caching. Report back on X API cost before wiring it in. Show its data on tickets.
6. **Skeptic:** real adversarial Claude call with the checklist and JSON output; default-reject on errors. Show objections on tickets.
7. **Reporting + cost controls:** Journal report script (including my decision stats), spend cap, spike warnings, replay mode that also drives the Trade Desk so I can practice approving on recorded days.

At the end of each phase: list the files you created or changed, tell me how to run it and the tests, and note anything you assumed that I should check.
