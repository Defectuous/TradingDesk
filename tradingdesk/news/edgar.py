"""
SEC EDGAR filings: a news source (filings as headlines) plus the ticket's filing flags.

Ported from WTBot llm_tweaks/edgar.py. Free official endpoints, no key; the SEC
requires a User-Agent with contact info (SEC_USER_AGENT in .env) and allows at
most 10 requests/second (we stay at news.sources.edgar.max_requests_per_sec).

  https://www.sec.gov/files/company_tickers.json          ticker -> CIK (refreshed weekly)
  https://data.sec.gov/submissions/CIK##########.json     filing index (refreshed each poll, cached 5 min)
  https://www.sec.gov/Archives/edgar/data/<cik>/<acc>/... Form 4 XML (immutable, cached forever)

Flags (FilingFlags on the ticket):
  offering      S-1 / S-3 / F-1 / F-3 / 424B*
  dilution      offering forms, or an 8-K item 3.02 (unregistered sale of equity)
  insider_buy   Form 4 open-market purchase (code P)
  insider_sell  Form 4 open-market sale (code S)
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import threading
import time
import xml.etree.ElementTree as ET_XML
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from loguru import logger

from tradingdesk.models import FilingFlags, Headline
from tradingdesk.sessions import ET

ITEMS_8K = {
    "1.01": "material agreement", "1.02": "agreement terminated", "1.03": "bankruptcy",
    "2.01": "acquisition/disposition completed", "2.02": "results of operations",
    "2.03": "new debt obligation", "2.05": "restructuring costs", "2.06": "impairment",
    "3.01": "DELISTING / listing-deficiency notice", "3.02": "UNREGISTERED SALE OF EQUITY (dilution)",
    "3.03": "change to shareholder rights", "4.01": "auditor change", "4.02": "prior financials unreliable",
    "5.01": "change in control", "5.02": "officer/director change", "5.03": "charter/bylaw amendment",
    "5.07": "shareholder vote results", "8.01": "other events",
}
OFFERING_PREFIXES = ("S-1", "S-3", "F-1", "F-3", "424B")
FORM4_CODES = {"P": "open-market BUY", "S": "open-market SALE"}


@dataclass
class Filing:
    form: str
    accepted: datetime            # ET
    items: tuple[str, ...]
    url: str


@dataclass
class EdgarResult:
    headlines: list[Headline] = field(default_factory=list)
    flags: FilingFlags = FilingFlags()
    offering_today: bool = False   # offering form or 8-K 3.02 accepted today (caps the grade at D)


class EdgarClient:
    name = "edgar"

    def __init__(self, user_agent: str, cache_dir: Path, forms: list[str], max_rps: float = 5,
                 lookback_days: int = 3) -> None:
        if not user_agent.strip():
            raise ValueError("SEC_USER_AGENT is required by the SEC")
        self.ua = user_agent.strip()
        self.cache = cache_dir
        self.forms = forms
        self.min_gap = 1.0 / max(0.5, min(max_rps, 9.0))
        self.lookback = timedelta(days=lookback_days)
        self._lock = threading.Lock()
        self._last = 0.0
        self._tickers: dict[str, int] | None = None
        self._results: dict[str, tuple[float, EdgarResult]] = {}

    # -- HTTP with rate limit + disk cache ---------------------------------------------------------
    def _get(self, url: str, kind: str = "json", max_age_sec: float | None = None) -> Any:
        self.cache.mkdir(parents=True, exist_ok=True)
        p = self.cache / (hashlib.md5(url.encode()).hexdigest() + (".json" if kind == "json" else ".txt"))
        if p.exists() and (max_age_sec is None or time.time() - p.stat().st_mtime < max_age_sec):
            text = p.read_text(encoding="utf-8")
            return json.loads(text) if kind == "json" else text
        r = None
        for attempt in range(3):
            with self._lock:
                wait = self._last + self.min_gap - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._last = time.monotonic()
            r = requests.get(url, headers={"User-Agent": self.ua, "Accept-Encoding": "gzip, deflate"}, timeout=20)
            if r.status_code == 404:
                return None
            if r.status_code == 200:
                break
            time.sleep(2 * (attempt + 1))
        assert r is not None
        r.raise_for_status()
        p.write_text(r.text, encoding="utf-8")
        return r.json() if kind == "json" else r.text

    def cik_for(self, ticker: str) -> int | None:
        if self._tickers is None:
            d = self._get("https://www.sec.gov/files/company_tickers.json", max_age_sec=7 * 86400) or {}
            self._tickers = {v["ticker"].upper(): int(v["cik_str"]) for v in d.values()}
        return self._tickers.get(ticker.upper().replace(".", "-"))

    def filings(self, ticker: str, start: datetime, end: datetime) -> list[Filing]:
        cik = self.cik_for(ticker)
        if not cik:
            return []
        sub = self._get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", max_age_sec=300)
        if not sub:
            return []
        r = sub["filings"]["recent"]
        out = []
        for i, form in enumerate(r["form"]):
            if not any(fnmatch.fnmatch(form, pat) for pat in self.forms):
                continue
            acc = datetime.strptime(r["acceptanceDateTime"][i][:19], "%Y-%m-%dT%H:%M:%S") \
                .replace(tzinfo=timezone.utc).astimezone(ET)
            if start <= acc <= end:
                doc = r["primaryDocument"][i].split("/")[-1]       # strip the xsl render prefix -> raw XML
                out.append(Filing(form, acc, tuple(x for x in (r["items"][i] or "").split(",") if x),
                                  f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                                  f"{r['accessionNumber'][i].replace('-', '')}/{doc}"))
        return out

    def _form4(self, url: str) -> list[tuple[str, float, float, str]]:
        """[(code, shares, price, owner)] for open-market buys/sells."""
        x = self._get(url, kind="text")
        if not x or not x.lstrip().startswith("<"):
            return []
        try:
            root = ET_XML.fromstring(x)
        except ET_XML.ParseError:
            return []
        for el in root.iter():
            el.tag = el.tag.split("}")[-1]
        owner = (root.findtext(".//rptOwnerName") or "").strip()
        out = []
        for t in root.iter("nonDerivativeTransaction"):
            code = (t.findtext("transactionCoding/transactionCode") or "").strip()
            if code not in FORM4_CODES:
                continue
            try:
                sh = float(t.findtext("transactionAmounts/transactionShares/value") or 0)
                px = float(t.findtext("transactionAmounts/transactionPricePerShare/value") or 0)
            except ValueError:
                sh = px = 0.0
            out.append((code, sh, px, owner))
        return out

    # -- results ------------------------------------------------------------------------------------
    def research(self, ticker: str, now: datetime) -> EdgarResult:
        """Filing headlines + flags for the last lookback_days. Cached for 5 minutes per ticker."""
        hit = self._results.get(ticker)
        if hit and time.monotonic() - hit[0] < 300:
            return hit[1]
        res = EdgarResult()
        forms_seen: list[str] = []
        offering = dilution = buy = sell = False
        for f in self.filings(ticker, now - self.lookback, now):
            forms_seen.append(f.form)
            today = f.accepted.date() == now.date()
            title = None
            if f.form.startswith(OFFERING_PREFIXES):
                offering = dilution = True
                res.offering_today |= today
                title = f"SEC filing {f.form}: offering / registration (potential DILUTION)"
            elif f.form in ("8-K", "6-K"):
                items = [i for i in f.items if i in ITEMS_8K]
                if "3.02" in items:
                    dilution = True
                    res.offering_today |= today
                if items:
                    title = f"SEC filing {f.form}: " + "; ".join(f"{i} {ITEMS_8K[i]}" for i in items)
            elif f.form == "4":
                for code, sh, px, owner in self._form4(f.url):
                    buy |= code == "P"
                    sell |= code == "S"
                    title = f"SEC Form 4: {owner} {FORM4_CODES[code]} {sh:,.0f} sh @ ${px:.2f}"
            if title:
                res.headlines.append(Headline(source=self.name, tickers=(ticker,), title=title, url=f.url,
                                              published_at=f.accepted, outlet="SEC EDGAR"))
        res.flags = FilingFlags(offering=offering, dilution=dilution, insider_buy=buy, insider_sell=sell,
                                forms=tuple(dict.fromkeys(forms_seen)))
        self._results[ticker] = (time.monotonic(), res)
        return res

    # NewsSource interface
    def fetch(self, ticker: str, since: datetime) -> list[Headline]:
        return [h for h in self.research(ticker, datetime.now(ET)).headlines if h.published_at >= since]


def make_edgar(settings) -> EdgarClient | None:
    if not settings.source_enabled("news", "edgar"):
        return None
    ua = settings.secrets.sec_user_agent
    if not ua:
        logger.warning("EDGAR is off: set SEC_USER_AGENT in .env (the SEC requires a contact, e.g. "
                       "'TradingDesk you@example.com')")
        return None
    cfg = settings.config.news.sources["edgar"].model_extra or {}
    return EdgarClient(ua, settings.path(Path(settings.config.paths.data_dir)) / "edgar_cache",
                       forms=list(cfg.get("forms") or ["8-K", "S-1", "S-3", "F-1", "F-3", "424B*", "4"]),
                       max_rps=float(cfg.get("max_requests_per_sec", 5)),
                       lookback_days=int(cfg.get("lookback_days", 3)))
