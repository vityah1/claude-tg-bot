"""SQLite-backed state: which sessions the bot owns, and per-chat selection."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, fields
from pathlib import Path

from . import paths
from .util import default_name

DB_PATH = paths.STATE_DB
# Claude's own titles are a sentence long; a chat header is not.
_LABEL_LIMIT = 48
_LEGACY_DB = Path(__file__).resolve().parent.parent / "state.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS managed (
    session_id TEXT PRIMARY KEY,
    window_id  TEXT NOT NULL,
    cwd        TEXT NOT NULL,
    name       TEXT NOT NULL,
    created_at REAL NOT NULL,
    offset     INTEGER NOT NULL DEFAULT 0,
    title      TEXT
);
-- The name Claude was launched with (`claude -n`) never changes, which is what
-- lets a session be found again after /clear hands it a new id. A name the
-- user picks is therefore kept apart, in custom_name.
CREATE TABLE IF NOT EXISTS active (
    chat_id    INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL
);
-- v2: keeps the AI title and the opening prompt apart, so the detail card
-- can show both. v3: scans now reach past the file-history preamble, so the
-- misses cached by v2 have to go. Renamed rather than migrated — it is only
-- a cache, and its rows are keyed by a path that never changes.
-- v4: the scan now also reads the launch name and the last prompt, and a
-- row cached without them would keep answering "nothing there".
DROP TABLE IF EXISTS history_cache;
DROP TABLE IF EXISTS history_cache2;
DROP TABLE IF EXISTS history_cache3;
-- Which session each outgoing message came from, so a Telegram reply can be
-- routed back to it regardless of which session is currently active.
CREATE TABLE IF NOT EXISTS msg_routes (
    message_id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS history_cache4 (
    path       TEXT PRIMARY KEY,
    mtime      REAL NOT NULL,
    size       INTEGER NOT NULL,
    session_id TEXT,
    cwd        TEXT,
    title      TEXT,
    opening    TEXT,
    launched   TEXT,
    last       TEXT
);
-- A name the user gave a session outlives the session. `managed` is emptied
-- when a session ends, so a name kept only there is lost exactly when it
-- starts being useful — in the list of closed sessions one has to be found in.
CREATE TABLE IF NOT EXISTS session_names (
    session_id TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    cwd        TEXT,
    updated_at REAL NOT NULL
);
"""


@dataclass
class Managed:
    session_id: str
    window_id: str
    cwd: str
    name: str
    created_at: float
    offset: int
    title: str | None
    custom_name: str | None = None

    @property
    def is_auto_named(self) -> bool:
        """True while nothing better than the launch name is known."""
        return not (self.custom_name or "").strip()

    @property
    def folder(self) -> str:
        return Path(self.cwd).name or "/"

    @property
    def label(self) -> str:
        """What to call this session where the folder is shown separately.

        A name the user picked always wins. Otherwise the launch name
        ("folder-1a2b") says nothing the folder does not, so Claude's own
        title — or, failing that, the session's opening prompt — takes over.
        """
        custom = (self.custom_name or "").strip()
        if custom:
            return custom
        title = (self.title or "").strip()
        if not title:
            return self.name
        return title if len(title) <= _LABEL_LIMIT else title[:_LABEL_LIMIT].rstrip() + "…"

    @property
    def full_label(self) -> str:
        """Label with the launch folder in front of it.

        Messages arrive in one chat from several projects at once, so "fix
        the replay bug" alone is not enough to know where the work is —
        unless the name already starts with the folder.
        """
        folder, label = self.folder, self.label
        if not folder or label.lower().startswith(folder.lower()):
            return label
        return f"{folder} · {label}"


class Store:
    def __init__(self, path: Path | None = None):
        path = path or DB_PATH
        paths.ensure(path.parent)
        # Older versions kept the database beside the code; move it once so
        # managed sessions survive the upgrade.
        if not path.exists() and _LEGACY_DB.exists():
            _LEGACY_DB.replace(path)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self._add_custom_name()
        self.conn.commit()

    def _add_custom_name(self) -> None:
        """Split a hand-picked name out of `name`, which must stay the launch one.

        Earlier versions renamed in place, so a session renamed back then has
        lost the name Claude knows it by — but that name is derivable, and
        putting it back is what lets the session be found again after /clear.
        """
        columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(managed)")}
        if "custom_name" in columns:
            return
        self.conn.execute("ALTER TABLE managed ADD COLUMN custom_name TEXT")
        for r in self.conn.execute(
            "SELECT session_id, cwd, name FROM managed"
        ).fetchall():
            launch = default_name(r["cwd"], r["session_id"])
            if r["name"] != launch:
                self.conn.execute(
                    "UPDATE managed SET custom_name=?, name=? WHERE session_id=?",
                    (r["name"], launch, r["session_id"]),
                )

    # -- managed sessions -------------------------------------------------
    def add(self, session_id: str, window_id: str, cwd: str, name: str) -> None:
        # A resumed session brings its name back with it: the row in `managed`
        # is new, but the one in `session_names` was never deleted.
        self.conn.execute(
            "INSERT OR REPLACE INTO managed"
            " (session_id, window_id, cwd, name, created_at, offset, title,"
            "  custom_name)"
            " VALUES (?,?,?,?,?,0,NULL,?)",
            (session_id, window_id, cwd, name, time.time(),
             self.saved_name(session_id)),
        )
        self.conn.commit()

    @staticmethod
    def _row_to_managed(row: sqlite3.Row) -> Managed:
        """Build a Managed from a row, ignoring columns this build lacks.

        The database outlives any single process: a newer build adds a column
        and every older one still running would then feed an unexpected keyword
        to the dataclass and die on every tick. Dropping what we do not know
        keeps a schema upgrade from taking a live bot down with it.
        """
        known = {f.name for f in fields(Managed)}
        return Managed(**{k: v for k, v in dict(row).items() if k in known})

    def all_managed(self) -> list[Managed]:
        rows = self.conn.execute(
            "SELECT * FROM managed ORDER BY created_at DESC"
        ).fetchall()
        return [self._row_to_managed(r) for r in rows]

    def get(self, session_id: str) -> Managed | None:
        r = self.conn.execute(
            "SELECT * FROM managed WHERE session_id=?", (session_id,)
        ).fetchone()
        return self._row_to_managed(r) if r else None

    def set_offset(self, session_id: str, offset: int) -> None:
        self.conn.execute(
            "UPDATE managed SET offset=? WHERE session_id=?", (offset, session_id)
        )
        self.conn.commit()

    def set_custom_name(self, session_id: str, name: str | None) -> None:
        """Set (or clear, with None) the name the user picked for a session."""
        mgd = self.get(session_id)
        with self.conn:
            self.conn.execute(
                "UPDATE managed SET custom_name=? WHERE session_id=?",
                (name, session_id),
            )
            if name:
                self.conn.execute(
                    "INSERT OR REPLACE INTO session_names"
                    " (session_id, name, cwd, updated_at) VALUES (?,?,?,?)",
                    (session_id, name, mgd.cwd if mgd else None, time.time()),
                )
            else:
                self.conn.execute(
                    "DELETE FROM session_names WHERE session_id=?", (session_id,)
                )

    # -- names that outlive their session ----------------------------------
    def saved_name(self, session_id: str) -> str | None:
        r = self.conn.execute(
            "SELECT name FROM session_names WHERE session_id=?", (session_id,)
        ).fetchone()
        return r["name"] if r else None

    def saved_names(self) -> dict[str, str]:
        """Every name the user has given, by session id.

        Read in one go: the history list asks about a couple of hundred
        sessions at a time, and a query each would be a query each tick.
        """
        return {r["session_id"]: r["name"]
                for r in self.conn.execute(
                    "SELECT session_id, name FROM session_names")}

    def rebind(self, old_id: str, new_id: str) -> None:
        """Follow a session that got a new id — /clear starts a fresh transcript.

        Everything else about it is unchanged: same window, same process, same
        name, same chat pointing at it. Only the transcript to read is new, so
        the offset restarts from zero.
        """
        with self.conn:
            self.conn.execute(
                "UPDATE managed SET session_id=?, offset=0, title=NULL"
                " WHERE session_id=?",
                (new_id, old_id),
            )
            self.conn.execute(
                "UPDATE active SET session_id=? WHERE session_id=?", (new_id, old_id)
            )
            self.conn.execute(
                "UPDATE msg_routes SET session_id=? WHERE session_id=?",
                (new_id, old_id),
            )
            # The name follows the session, not the transcript: after /clear
            # the work carries on under the new id, and that is what the
            # history list will show. The old transcript keeps no name — it is
            # a different, earlier stretch of work.
            self.conn.execute(
                "UPDATE OR REPLACE session_names SET session_id=?"
                " WHERE session_id=?",
                (new_id, old_id),
            )

    def set_title(self, session_id: str, title: str) -> None:
        self.conn.execute(
            "UPDATE managed SET title=? WHERE session_id=?", (title, session_id)
        )
        self.conn.commit()

    def remove(self, session_id: str) -> None:
        self.conn.execute("DELETE FROM managed WHERE session_id=?", (session_id,))
        self.conn.execute("DELETE FROM active WHERE session_id=?", (session_id,))
        self.conn.commit()

    # -- per-chat active session ------------------------------------------
    def set_active(self, chat_id: int, session_id: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO active (chat_id, session_id) VALUES (?,?)",
            (chat_id, session_id),
        )
        self.conn.commit()

    def get_active(self, chat_id: int) -> str | None:
        r = self.conn.execute(
            "SELECT session_id FROM active WHERE chat_id=?", (chat_id,)
        ).fetchone()
        return r["session_id"] if r else None

    def clear_active(self, chat_id: int) -> None:
        self.conn.execute("DELETE FROM active WHERE chat_id=?", (chat_id,))
        self.conn.commit()

    # -- message → session routing ----------------------------------------
    def remember_message(self, message_id: int, session_id: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO msg_routes (message_id, session_id, created_at)"
            " VALUES (?,?,?)",
            (message_id, session_id, time.time()),
        )
        self.conn.execute(
            "DELETE FROM msg_routes WHERE message_id NOT IN"
            " (SELECT message_id FROM msg_routes ORDER BY created_at DESC LIMIT 500)"
        )
        self.conn.commit()

    def session_of_message(self, message_id: int) -> str | None:
        r = self.conn.execute(
            "SELECT session_id FROM msg_routes WHERE message_id=?", (message_id,)
        ).fetchone()
        return r["session_id"] if r else None

    # -- history scan cache ------------------------------------------------
    def cached_history(self, path: str, mtime: float, size: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM history_cache4 WHERE path=? AND mtime=? AND size=?",
            (path, mtime, size),
        ).fetchone()

    def cache_history(self, path: str, mtime: float, size: int,
                      session_id: str | None, cwd: str | None,
                      title: str | None, opening: str | None,
                      launched: str | None = None,
                      last: str | None = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO history_cache4"
            " (path, mtime, size, session_id, cwd, title, opening, launched, last)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (path, mtime, size, session_id, cwd, title, opening, launched, last),
        )
        self.conn.commit()
