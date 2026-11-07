"""
Journal — SQLite record of every candidate (including rejected and expired),
every agent verdict, every trade-management action, and every LLM call.

A candidate is stored as its full JSON plus indexed columns for reporting.
Saving the same candidate again replaces its row; the Candidate model itself
guarantees verdicts were only ever appended.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from tradingdesk.llm.costs import LLMUsage
from tradingdesk.models import Candidate, CandidateStatus
from tradingdesk.sessions import ET, now_et

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS candidates (
    id               TEXT PRIMARY KEY,
    ticker           TEXT NOT NULL,
    trade_date       TEXT NOT NULL,
    session          TEXT NOT NULL,
    status           TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    rejected_by      TEXT,
    catalyst_grade   TEXT,
    catalyst_type    TEXT,
    human_decision   TEXT,
    decision_seconds REAL,
    entry            REAL,
    stop             REAL,
    target           REAL,
    shares           INTEGER,
    realized_pnl     REAL,
    r_multiple       REAL,
    data             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cand_date   ON candidates(trade_date);
CREATE INDEX IF NOT EXISTS ix_cand_status ON candidates(status);
CREATE INDEX IF NOT EXISTS ix_cand_ticker ON candidates(ticker);

CREATE TABLE IF NOT EXISTS verdicts (
    candidate_id TEXT NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    agent        TEXT NOT NULL,
    decision     TEXT NOT NULL,
    reasons      TEXT NOT NULL,
    at           TEXT NOT NULL,
    PRIMARY KEY (candidate_id, seq)
);
CREATE INDEX IF NOT EXISTS ix_verdict_agent ON verdicts(agent, decision);

CREATE TABLE IF NOT EXISTS trade_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,
    trade_date   TEXT NOT NULL,
    candidate_id TEXT,
    ticker       TEXT,
    event        TEXT NOT NULL,
    detail       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_event_date ON trade_events(trade_date);

CREATE TABLE IF NOT EXISTS llm_usage (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    at                 TEXT NOT NULL,
    trade_date         TEXT NOT NULL,
    agent              TEXT NOT NULL,
    model              TEXT NOT NULL,
    local              INTEGER NOT NULL,
    input_tokens       INTEGER NOT NULL,
    output_tokens      INTEGER NOT NULL,
    cache_read_tokens  INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    cost_usd           REAL NOT NULL,
    ticker             TEXT,
    candidate_id       TEXT
);
CREATE INDEX IF NOT EXISTS ix_llm_date_agent ON llm_usage(trade_date, agent);
"""


class Journal:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if str(path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Journal":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # -- candidates -----------------------------------------------------------
    def save_candidate(self, c: Candidate) -> None:
        pt, rs, human = c.proposed_trade, c.risk_sizing, c.human
        row = {
            "id": c.id,
            "ticker": c.ticker,
            "trade_date": c.trade_date,
            "session": c.session,
            "status": c.status.value,
            "created_at": c.created_at.isoformat(),
            "updated_at": now_et().isoformat(),
            "rejected_by": c.rejected_by.value if c.rejected_by else None,
            "catalyst_grade": c.news.catalyst_grade if c.news else None,
            "catalyst_type": c.news.catalyst_type if c.news else None,
            "human_decision": human.decision if human else None,
            "decision_seconds": human.decision_seconds if human else None,
            "entry": pt.entry if pt else None,
            "stop": pt.stop if pt else None,
            "target": pt.target if pt else None,
            "shares": (human.approved_shares if human and human.approved_shares else (rs.shares if rs else None)),
            "realized_pnl": c.realized_pnl if c.fills else None,
            "r_multiple": c.r_multiple,
            "data": c.model_dump_json(),
        }
        cols = ", ".join(row)
        marks = ", ".join(f":{k}" for k in row)
        updates = ", ".join(f"{k}=excluded.{k}" for k in row if k != "id")
        with self._tx() as conn:
            conn.execute(
                f"INSERT INTO candidates ({cols}) VALUES ({marks}) ON CONFLICT(id) DO UPDATE SET {updates}", row
            )
            conn.execute("DELETE FROM verdicts WHERE candidate_id = ?", (c.id,))
            conn.executemany(
                "INSERT INTO verdicts(candidate_id, seq, agent, decision, reasons, at) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (c.id, i, v.agent.value, v.decision, json.dumps(list(v.reasons)), v.timestamp.isoformat())
                    for i, v in enumerate(c.verdicts)
                ],
            )

    def get_candidate(self, candidate_id: str) -> Candidate | None:
        with self._lock:
            r = self._conn.execute("SELECT data FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
        return Candidate.model_validate_json(r["data"]) if r else None

    def list_candidates(
        self, trade_date: str | None = None, status: CandidateStatus | str | None = None
    ) -> list[Candidate]:
        sql, args = "SELECT data FROM candidates WHERE 1=1", []
        if trade_date:
            sql += " AND trade_date = ?"
            args.append(trade_date)
        if status:
            sql += " AND status = ?"
            args.append(str(CandidateStatus(status)))
        sql += " ORDER BY created_at"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [Candidate.model_validate_json(r["data"]) for r in rows]

    # -- trade events -------------------------------------------------------------
    def log_event(
        self, event: str, *, candidate_id: str | None = None, ticker: str | None = None, **detail: Any
    ) -> None:
        at = now_et()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO trade_events(at, trade_date, candidate_id, ticker, event, detail) VALUES (?, ?, ?, ?, ?, ?)",
                (at.isoformat(), at.date().isoformat(), candidate_id, ticker, event, json.dumps(detail, default=str)),
            )

    def events(self, candidate_id: str | None = None, trade_date: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM trade_events WHERE 1=1", []
        if candidate_id:
            sql += " AND candidate_id = ?"
            args.append(candidate_id)
        if trade_date:
            sql += " AND trade_date = ?"
            args.append(trade_date)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY id", args).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]

    # -- LLM usage -------------------------------------------------------------------
    def log_llm_usage(self, u: LLMUsage) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO llm_usage(at, trade_date, agent, model, local, input_tokens, output_tokens,
                       cache_read_tokens, cache_write_tokens, cost_usd, ticker, candidate_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    u.at.isoformat(), u.at.astimezone(ET).date().isoformat(), u.agent, u.model, int(u.local),
                    u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens,
                    u.cost_usd, u.ticker, u.candidate_id,
                ),
            )

    def llm_spend(self, trade_date: str, agent: str | None = None) -> float:
        sql, args = "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE trade_date = ?", [trade_date]
        if agent:
            sql += " AND agent = ?"
            args.append(agent)
        with self._lock:
            return float(self._conn.execute(sql, args).fetchone()[0])

    def llm_spend_by_agent(self, trade_date: str) -> dict[str, float]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT agent, SUM(cost_usd) FROM llm_usage WHERE trade_date = ? GROUP BY agent ORDER BY agent",
                (trade_date,),
            ).fetchall()
        return {r[0]: float(r[1]) for r in rows}

    def recent_token_average(self, agent: str, window: int) -> float | None:
        """Average total tokens over the agent's last `window` calls (None if no history)."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT input_tokens + output_tokens + cache_read_tokens + cache_write_tokens
                   FROM llm_usage WHERE agent = ? ORDER BY id DESC LIMIT ?""",
                (agent, window),
            ).fetchall()
        return sum(r[0] for r in rows) / len(rows) if rows else None
