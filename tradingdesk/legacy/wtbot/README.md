# WTBot code, reused as-is

Copied from `C:\Users\defec\Documents\WTBot\screener\` on 2026-11-07 (WTBot commit
`310a550` plus its uncommitted working-tree changes to `alpaca_client.py`, which is
the version WTBot runs live).

The only edits are import lines:

- `import config` → `from tradingdesk.legacy.wtbot import config` (a shim fed from config.yaml)
- `from screener import alpaca_client` → `from tradingdesk.legacy.wtbot import alpaca_client`

| File | Used for |
|---|---|
| `pillar_checks.py` | Ross Cameron 5 Pillars (Scout). Logic unchanged; don't edit without asking. |
| `alpaca_client.py` | Snapshots, RVOL with outlier trimming, bars, quotes, universe |
| `technical_levels.py` | Premarket high/low, high of day, VWAP, bars |
| `yahoo_news.py` | Float share lookup, and Yahoo news for Newsie |
| `google_news.py` | Google News RSS for Newsie (copied 2026-11-08 from WTBot commit `abc2ed8`) |

Not copied: `strategy_selector.py`. The Manager uses one breakout setup instead
(decided 2026-11-07), so there is no per-pass Claude strategy call.

Also reused for Newsie, copied into `tradingdesk/news/flags.py` without logic changes: WTBot's roundup /
ticker-named checks (`ai_catalyst.py`), keyword flags (`news_flags.py`) and junk-stub regex
(`llm_tweaks/prompts.py`). `llm_tweaks/edgar.py` was ported to `tradingdesk/news/edgar.py`.
