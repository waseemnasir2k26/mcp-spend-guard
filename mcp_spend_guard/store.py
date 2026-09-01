"""SQLite persistence.

Counters must survive a restart, otherwise a crash-looping agent resets its own
budget every few seconds.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    created_at   REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    config_path  TEXT,
    runs         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    tool        TEXT NOT NULL,
    request_id  TEXT,
    ts          REAL NOT NULL,
    cost_usd    REAL NOT NULL DEFAULT 0.0,
    status      TEXT NOT NULL,
    detail      TEXT
);

CREATE INDEX IF NOT EXISTS idx_calls_session ON calls(session_id);
CREATE INDEX IF NOT EXISTS idx_calls_session_ts ON calls(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_calls_session_tool ON calls(session_id, tool);
"""

STATUS_ALLOWED = "allowed"
STATUS_BLOCKED = "blocked"
STATUS_ERROR = "error"

# An errored call still consumed budget: it was dispatched to the real server.
COUNTED = (STATUS_ALLOWED, STATUS_ERROR)


@dataclass
class ToolRow:
    tool: str
    allowed: int
    blocked: int
    errors: int
    spend_usd: float


@dataclass
class SessionRow:
    session_id: str
    created_at: float
    last_seen_at: float
    runs: int
    allowed: int
    blocked: int
    errors: int
    spend_usd: float


class Store:
    """Thin SQLite wrapper. One connection per guard process."""

    def __init__(self, db_path):
        self.path = Path(db_path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------------- sessions ----------------

    def start_session(self, session_id: str, config_path: Optional[str] = None) -> None:
        now = time.time()
        cur = self.conn.execute(
            "UPDATE sessions SET last_seen_at=?, runs=runs+1,"
            " config_path=COALESCE(?, config_path) WHERE session_id=?",
            (now, config_path, session_id),
        )
        if cur.rowcount == 0:
            self.conn.execute(
                "INSERT INTO sessions(session_id, created_at, last_seen_at, config_path, runs)"
                " VALUES (?,?,?,?,1)",
                (session_id, now, now, config_path),
            )
        self.conn.commit()

    def touch_session(self, session_id: str) -> None:
        self.conn.execute(
            "UPDATE sessions SET last_seen_at=? WHERE session_id=?", (time.time(), session_id)
        )
        self.conn.commit()

    # ---------------- calls ----------------

    def record_call(
        self,
        session_id: str,
        tool: str,
        status: str,
        cost_usd: float = 0.0,
        request_id: Optional[str] = None,
        detail: Optional[str] = None,
        ts: Optional[float] = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO calls(session_id, tool, request_id, ts, cost_usd, status, detail)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                session_id,
                tool,
                request_id,
                ts if ts is not None else time.time(),
                cost_usd,
                status,
                detail,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def mark_call_error(self, call_id: int, detail: Optional[str] = None) -> None:
        self.conn.execute(
            "UPDATE calls SET status=?, detail=? WHERE id=?", (STATUS_ERROR, detail, call_id)
        )
        self.conn.commit()

    # ---------------- counters ----------------

    def total_allowed(self, session_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM calls WHERE session_id=? AND status IN (?,?)",
            (session_id,) + COUNTED,
        ).fetchone()
        return int(row["n"])

    def tool_allowed(self, session_id: str, tool: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM calls WHERE session_id=? AND tool=? AND status IN (?,?)",
            (session_id, tool) + COUNTED,
        ).fetchone()
        return int(row["n"])

    def allowed_since(self, session_id: str, since_ts: float) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM calls WHERE session_id=? AND ts>=? AND status IN (?,?)",
            (session_id, since_ts) + COUNTED,
        ).fetchone()
        return int(row["n"])

    def spend(self, session_id: str) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0.0) AS s FROM calls"
            " WHERE session_id=? AND status IN (?,?)",
            (session_id,) + COUNTED,
        ).fetchone()
        return float(row["s"])

    def session_started_at(self, session_id: str) -> Optional[float]:
        row = self.conn.execute(
            "SELECT created_at FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        return float(row["created_at"]) if row else None

    # ---------------- reporting ----------------

    def sessions(self) -> List[SessionRow]:
        rows = self.conn.execute(
            """
            SELECT s.session_id, s.created_at, s.last_seen_at, s.runs,
                   COALESCE(SUM(c.status IN ('allowed','error')), 0) AS allowed,
                   COALESCE(SUM(c.status = 'blocked'), 0)            AS blocked,
                   COALESCE(SUM(c.status = 'error'), 0)              AS errors,
                   COALESCE(SUM(CASE WHEN c.status IN ('allowed','error')
                                     THEN c.cost_usd ELSE 0 END), 0.0) AS spend
            FROM sessions s
            LEFT JOIN calls c ON c.session_id = s.session_id
            GROUP BY s.session_id
            ORDER BY s.last_seen_at DESC
            """
        ).fetchall()
        return [
            SessionRow(
                session_id=r["session_id"],
                created_at=float(r["created_at"]),
                last_seen_at=float(r["last_seen_at"]),
                runs=int(r["runs"]),
                allowed=int(r["allowed"]),
                blocked=int(r["blocked"]),
                errors=int(r["errors"]),
                spend_usd=float(r["spend"]),
            )
            for r in rows
        ]

    def tools(self, session_id: str) -> List[ToolRow]:
        rows = self.conn.execute(
            """
            SELECT tool,
                   SUM(status IN ('allowed','error')) AS allowed,
                   SUM(status = 'blocked')            AS blocked,
                   SUM(status = 'error')              AS errors,
                   SUM(CASE WHEN status IN ('allowed','error') THEN cost_usd ELSE 0 END) AS spend
            FROM calls WHERE session_id=?
            GROUP BY tool ORDER BY spend DESC, allowed DESC, tool ASC
            """,
            (session_id,),
        ).fetchall()
        return [
            ToolRow(
                tool=r["tool"],
                allowed=int(r["allowed"] or 0),
                blocked=int(r["blocked"] or 0),
                errors=int(r["errors"] or 0),
                spend_usd=float(r["spend"] or 0.0),
            )
            for r in rows
        ]

    def block_reasons(self, session_id: str) -> Dict[str, int]:
        rows = self.conn.execute(
            "SELECT detail, COUNT(*) AS n FROM calls WHERE session_id=? AND status='blocked'"
            " GROUP BY detail ORDER BY n DESC",
            (session_id,),
        ).fetchall()
        return {(r["detail"] or "unknown"): int(r["n"]) for r in rows}

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass
