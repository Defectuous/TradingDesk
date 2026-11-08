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
- Re-enable **Alpaca News** once it's reliable. Likely cause of WTBot's empty results:
  `NewsRequest(exclude_contentless=True)` drops headline-only items, which is most small-cap
  wire news (about 76% of WTBot's calls returned 0 articles). TradingDesk's `alpaca_news`
  source already asks with `exclude_contentless=False`; it's off until tested
  (`news.sources.alpaca_news.enabled`).
- Re-enable **Newsfilter.io** once its subscription site lets me sign up for API access.
  Use the official key-based API only; WTBot's old login-and-scrape approach
  (`_archive/news_scraper.py`) is not coming back.
- **X** (decide before wiring in): pay-per-use only now, $0.005 per post read (deduplicated within a
  24-hour UTC window), capped at 3M reads/month before Enterprise (official pricing page, 2026-11-08).
  Estimate for cashtag searches on 20 watched tickers every 2 minutes, 5:30-11:00 with ~2-10 new posts per
  poll: ~6,600-33,000 reads a day = ~$33-165/day (~$700-3,500/month). Narrowed to the top 5 movers every
  5 minutes: ~$15/day. Whether recent search's `$TICKER` operator is available on pay-per-use isn't stated
  in the official docs; confirm before building. StockTwits already covers the trader crowd for free.
- **Reddit**: new API access needs approval under Reddit's Responsible Builder Policy (self-service app
  creation is closed). Request access with a dedicated account describing the personal, read-only use; the
  source stays off until approved.
- Additional social platforms beyond X, StockTwits and Reddit.
- Halt detection from Alpaca's market-data trading-status stream (today: asset status +
  "no trades for 2 minutes during the session" heuristic).
- Streaming order updates (Alpaca trade_updates) instead of polling every 2s.

## Carried over from WTBot's roadmap (still relevant)

- Consecutive-loss circuit breaker (pause after N losses in a row).
- Multi-timeframe confirmation (5-min agrees with 1-min).
- Level 2 depth at the entry price.
- Equity curve and weekly P&L summary to Discord.
- Live-trading readiness checklist: at least 20 paper trades with positive expectancy,
  then start at 25% size.
