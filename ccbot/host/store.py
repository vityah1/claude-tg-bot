"""The host's own SQLite file: the event journal, open requests, sessions.

Separate from the bot's state.db on purpose — one writer per file, and the
host must not depend on the bot's schema. Everything here is what has to
outlive a host restart: which sessions exist (to resume them), the prompt a
turn was started with (to offer it again if the turn was cut off), the
questions that were open (to tell the chat they are gone), and the journal
the bot replays after it has been away.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import paths

DB_PATH = paths.data_dir() / "host.db"

# The journal is for a bot that was away for a restart or an hour, not for
# history: the transcripts are the history.
_JOURNAL_TTL = 24 * 3600.0
_JOURNAL_MAX = 50_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts    REAL NOT NULL,
    name  TEXT,
    type  TEXT NOT NULL,
    json  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    rid       TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    sid       TEXT,
    tool      TEXT NOT NULL,
    input     TEXT NOT NULL,
    state     TEXT NOT NULL,      -- open | answered | lost
    answer    TEXT,
    opened_at REAL NOT NULL,
    closed_at REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    name       TEXT PRIMARY KEY,  -- the launch name, stable across /clear
    sid        TEXT NOT NULL,
    cwd        TEXT NOT NULL,
    state      TEXT NOT NULL,     -- live | dormant | closed
    inflight   TEXT,              -- prompts not answered yet: [{u, t, k}] (actor.encode_inflight)
    info       TEXT,              -- last init / metrics, for the bot's card
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class SessionRow:
    name: str
    sid: str
    cwd: str
    state: str
    inflight: str | None
    info: dict[str, Any]


class HostStore:
    def __init__(self, path: Path | None = None):
        path = path or DB_PATH
        paths.ensure(path.parent)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # -- journal -------------------------------------------------------
    def append(self, name: str | None, type_: str, data: dict[str, Any]) -> int:
        cur = self.conn.execute(
            "INSERT INTO events (ts, name, type, json) VALUES (?,?,?,?)",
            (time.time(), name, type_, json.dumps(data, ensure_ascii=False, default=str)))
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def head(self) -> int:
        r = self.conn.execute("SELECT MAX(seq) AS s FROM events").fetchone()
        return int(r["s"] or 0)

    def oldest(self) -> int:
        r = self.conn.execute("SELECT MIN(seq) AS s FROM events").fetchone()
        return int(r["s"] or 0)

    def since(self, seq: int, limit: int = 5000) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?",
            (seq, limit)).fetchall()

    def prune(self, acked: int) -> None:
        """Drop what the bot has seen and is old, or far too much of it."""
        cutoff = time.time() - _JOURNAL_TTL
        self.conn.execute("DELETE FROM events WHERE seq <= ? AND ts < ?",
                          (acked, cutoff))
        self.conn.execute(
            "DELETE FROM events WHERE seq <= (SELECT MAX(seq) FROM events) - ?",
            (_JOURNAL_MAX,))
        self.conn.commit()

    # -- requests ------------------------------------------------------
    def open_request(self, rid: str, name: str, sid: str, tool: str,
                     input_: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO requests (rid, name, sid, tool, input, state,"
            " opened_at) VALUES (?,?,?,?,?, 'open', ?)",
            (rid, name, sid, tool, json.dumps(input_, ensure_ascii=False, default=str),
             time.time()))
        self.conn.commit()

    def close_request(self, rid: str, state: str, answer: dict[str, Any] | None) -> bool:
        """open → answered/lost, once. False when it was not open any more."""
        cur = self.conn.execute(
            "UPDATE requests SET state=?, answer=?, closed_at=? WHERE rid=? AND state='open'",
            (state, json.dumps(answer, ensure_ascii=False, default=str) if answer else None,
             time.time(), rid))
        self.conn.commit()
        return cur.rowcount == 1

    def request(self, rid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM requests WHERE rid=?", (rid,)).fetchone()

    def open_requests(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM requests WHERE state='open' ORDER BY opened_at").fetchall()

    def lose_open_requests(self) -> list[sqlite3.Row]:
        """At startup: whatever was open died with the previous process."""
        rows = self.open_requests()
        for r in rows:
            self.close_request(r["rid"], "lost", None)
        self.conn.execute("DELETE FROM requests WHERE state != 'open' AND closed_at < ?",
                          (time.time() - _JOURNAL_TTL,))
        self.conn.commit()
        return rows

    # -- sessions ------------------------------------------------------
    @staticmethod
    def _row(r: sqlite3.Row) -> SessionRow:
        try:
            info = json.loads(r["info"] or "{}")
        except ValueError:
            info = {}
        return SessionRow(name=r["name"], sid=r["sid"], cwd=r["cwd"], state=r["state"],
                          inflight=r["inflight"], info=info if isinstance(info, dict) else {})

    def upsert_session(self, name: str, sid: str, cwd: str, state: str) -> None:
        now = time.time()
        self.conn.execute(
            "INSERT INTO sessions (name, sid, cwd, state, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET"
            " sid=excluded.sid, cwd=excluded.cwd, state=excluded.state,"
            " updated_at=excluded.updated_at",
            (name, sid, cwd, state, now, now))
        self.conn.commit()

    def set_session(self, name: str, **fields: Any) -> None:
        if not fields:
            return
        if "info" in fields and not isinstance(fields["info"], str):
            fields["info"] = json.dumps(fields["info"], ensure_ascii=False, default=str)
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE sessions SET {cols}, updated_at=? WHERE name=?",
                          (*fields.values(), time.time(), name))
        self.conn.commit()

    def session(self, name: str) -> SessionRow | None:
        r = self.conn.execute("SELECT * FROM sessions WHERE name=?", (name,)).fetchone()
        return self._row(r) if r else None

    def sessions(self, include_closed: bool = False) -> list[SessionRow]:
        q = "SELECT * FROM sessions" + ("" if include_closed else " WHERE state != 'closed'")
        return [self._row(r) for r in self.conn.execute(q + " ORDER BY created_at")]

    # -- meta ----------------------------------------------------------
    def meta_get(self, key: str) -> str | None:
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def meta_set(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value))
        self.conn.commit()
