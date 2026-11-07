# TradingDesk Roadmap

Not part of the current build. The current code is designed so each item can be
added without restructuring.

## Additional brokers

Each one becomes a new `Broker` implementation and must pass the broker contract tests
(`tests/broker_contract/`, phase 3). Every broker needs a real paper account or a
dry-run mode; one with neither can't be enabled without `ALLOW_LIVE=true`.

- **Robinhood**: wanted, but stock-trading API access is unsettled. Its long-standing
  official public API has been crypto-only, and it has been opening some form of
  AI-agent trading access whose details are unclear. When this comes up: check
  Robinhood's current official developer docs and report exactly what's supported for
  stocks. **Official, documented access only.** No reverse-engineered or unofficial
  libraries (e.g., ones that log in with username/password); they break without warning
  and can get the account restricted.
- **Other brokers with official APIs** to evaluate: Interactive Brokers, Charles Schwab,
  Tradier, E*TRADE, Webull (WTBot's roadmap also lists TradeStation). Before building any,
  verify each one's current API access, paper-trading support, bracket-order support,
  costs and approval requirements, and present a comparison.
- **Several brokers at once** (choose the broker per ticket on the Trade Desk), after
  single-broker swapping works.

## Other ideas

- Approve tickets from my phone (Discord buttons or a mobile-friendly Trade Desk), with
  the same click-time Risk Officer re-check.
- Re-enable **Alpaca News** once it's reliable. Likely cause of the empty results today:
  WTBot's `NewsRequest(exclude_contentless=True)` drops headline-only items, which is most
  small-cap wire news (about 76% of WTBot's calls returned 0 articles).
- Re-enable **Newsfilter.io** once its subscription site lets me sign up for API access.
  Use the official key-based API only; WTBot's old login-and-scrape approach
  (`_archive/news_scraper.py`) is not coming back.
- Additional social platforms beyond X, StockTwits and Reddit.

- Halt detection from Alpaca's market-data trading-status stream (today: asset status +
  "no trades for 2 minutes during the session" heuristic).
- Streaming order updates (Alpaca trade_updates) instead of polling every 2s.

## Carried over from WTBot's roadmap (still relevant)

- Consecutive-loss circuit breaker (pause after N losses in a row).
- PDT day-trade counter on the status bar.
- Multi-timeframe confirmation (5-min agrees with 1-min).
- Level 2 depth at the entry price.
- Equity curve and weekly P&L summary to Discord.
- Live-trading readiness checklist: at least 20 paper trades with positive expectancy,
  then start at 25% size.
