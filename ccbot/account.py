"""Which Claude account each session runs under.

`~/.claude.json` names the account of the *last* `/login`, and a session that
was already running keeps the account it started with until it is restarted —
so that file answers for the newest session only, and is exactly wrong for
the ones the user wants to know about after switching accounts.

The session itself says it: Claude Code writes an attachment record of type
`session_context` into the transcript with the first request after a start
(or a resume) and again after the account changed, and its `userEmail` names
the address. The last such record is the account in force. Two rules keep the
answer honest:

* a record older than the running process belongs to the previous run (a
  resume appends to the same file, and writes its own record only with the
  first prompt), so it is not trusted — nothing is shown instead;
* the session that has just logged in, or was just restarted onto the new
  account by the bot, has no new record until its next request, so the bot
  remembers what it knows (`note_login`) until the transcript catches up.

The e-mail is taken by its shape, never by the English sentence around it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import status_feed, transcript

log = logging.getLogger("ccbot.account")

_MARK = b'"type":"session_context"'
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_CHUNK = 1 << 20
_LINE_CAP = 1 << 16
# A record this much older than the process start still counts: the two
# clocks are read by different code a few milliseconds apart.
_SKEW = 5.0
_CACHE_MAX = 128
# A cold scan of more than this goes to a thread: a long transcript is tens
# of megabytes, and the watcher tick must not stall on it.
_INLINE_BYTES = _CHUNK



def _claude_json() -> Path:
    """Where the last login is written: the profile dir, or the home default."""
    conf = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(conf) / ".claude.json" if conf else Path.home() / ".claude.json"


@dataclass
class _Entry:
    path: Path
    offset: int = 0          # bytes already looked at, ends on a newline
    email: str = ""
    ts: float = 0.0          # when the record was written (epoch), 0 if unknown
    override: str = ""       # what the bot knows before the transcript does
    override_at: int = 0     # file size when the override was set


_cache: dict[str, _Entry] = {}
_login_cache: tuple[float, str] = (0.0, "")


def _parse(line: bytes) -> tuple[str, float] | None:
    """The address and the time of one `session_context` line.

    None when the line is not a readable record — the caller then keeps
    looking, or keeps what it knew. An empty address from a readable record
    is an answer (an API-key session has none).
    """
    try:
        rec = json.loads(line.decode("utf-8", "replace"))
    except ValueError:
        return None
    att = rec.get("attachment") if isinstance(rec, dict) else None
    if not isinstance(att, dict) or att.get("type") != "session_context":
        return None
    ctx = att.get("context")
    said = ctx.get("userEmail") if isinstance(ctx, dict) else None
    m = _EMAIL_RE.search(said) if isinstance(said, str) else None
    ts = 0.0
    raw_ts = rec.get("timestamp")
    if isinstance(raw_ts, str):
        try:
            ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts = 0.0
    return (m.group(0) if m else ""), ts


def _line_from(fh, start: int, end: int) -> bytes:
    """The whole line that begins at *start*, however long it is."""
    fh.seek(start)
    parts = []
    pos = start
    while pos < end:
        chunk = fh.read(min(_CHUNK, end - pos))
        if not chunk:
            break
        nl = chunk.find(b"\n")
        if nl >= 0:
            parts.append(chunk[:nl])
            break
        parts.append(chunk)
        pos += len(chunk)
    return b"".join(parts)


def _line_start(fh, pos: int, floor: int) -> int:
    """Where the line holding byte *pos* begins (never before *floor*)."""
    while pos > floor:
        lo = max(floor, pos - _CHUNK)
        fh.seek(lo)
        nl = fh.read(pos - lo).rfind(b"\n")
        if nl >= 0:
            return lo + nl + 1
        pos = lo
    return floor


def _last_newline(fh, size: int, floor: int) -> int:
    """The offset just past the last complete line (*floor* when there is none)."""
    pos = size
    while pos > floor:
        lo = max(floor, pos - _CHUNK)
        fh.seek(lo)
        nl = fh.read(pos - lo).rfind(b"\n")
        if nl >= 0:
            return lo + nl + 1
        pos = lo
    return floor


def _newest(fh, floor: int, end: int) -> tuple[int, tuple[str, float]] | None:
    """The newest readable record in [floor, end), read backwards in chunks.

    A record cut off mid-file (a crash while it was written) does not end the
    search: the one before it still says who the session was.
    """
    pos = end
    overlap = len(_MARK)
    while pos > floor:
        lo = max(floor, pos - _CHUNK)
        fh.seek(lo)
        buf = fh.read(min(pos + overlap, end) - lo)
        hit = buf.rfind(_MARK)
        while hit >= 0:
            start = _line_start(fh, lo + hit, floor)
            got = _parse(_line_from(fh, start, end))
            if got is not None:
                return start, got
            # Unreadable: look further back, before this line.
            if start <= lo:
                pos = start
                break
            hit = buf.rfind(_MARK, 0, start - lo)
        else:
            pos = lo
    return None


def _scan(e: _Entry) -> None:
    """Bring the entry up to the end of its file. Synchronous; may be slow."""
    try:
        size = e.path.stat().st_size
    except OSError:
        return
    if size < e.offset:                       # replaced or truncated
        e.offset, e.email, e.ts = 0, "", 0.0
    if size == e.offset:
        return
    with e.path.open("rb") as fh:
        # Only whole lines: a half-written last line is read on the next pass.
        end = _last_newline(fh, size, e.offset)
        if end == e.offset:
            return
        found = _newest(fh, e.offset, end)
        if found is not None:
            start, (e.email, e.ts) = found
            if e.override and start >= e.override_at:
                e.override = ""
        e.offset = end


def _evict() -> None:
    """Keep the cache bounded, never at the cost of what the bot was told."""
    while len(_cache) >= _CACHE_MAX:
        victim = next((k for k, v in _cache.items() if not v.override), None)
        if victim is None:
            return
        _cache.pop(victim)


def _entry(session_id: str, path_hint: str) -> _Entry | None:
    e = _cache.get(session_id)
    if e is not None and e.path.exists():
        return e
    path = Path(path_hint) if path_hint else None
    if path is None or not path.exists():
        path = transcript.find_transcript(session_id)
    if path is None:
        return e                              # keeps an override alive
    fresh = _Entry(path=path)
    if e is not None and e.override:
        # Only a record written from now on may replace what the bot knows.
        try:
            fresh.override_at = path.stat().st_size
        except OSError:
            fresh.override_at = 0
        fresh.override = e.override
    if e is None:
        _evict()
    _cache[session_id] = fresh
    return fresh


async def email_for(session_id: str, *, path_hint: str = "",
                    since: float = 0.0) -> str:
    """The account this session runs under, or "" when it cannot be told.

    *since* is when the running process started: a record older than that
    was written by a run that has ended. Without it (the process is not in
    the agent list yet) only what the bot knows itself is said. Never raises.
    """
    if not path_hint:
        u = status_feed.read(session_id)
        path_hint = u.transcript_path if u else ""
    e = _entry(session_id, path_hint)
    if e is None:
        return ""
    try:
        pending = e.path.stat().st_size - e.offset
        if pending > _INLINE_BYTES:
            await asyncio.to_thread(_scan, e)
        else:
            _scan(e)
    except OSError as exc:
        log.debug("account scan %s: %s", session_id[:8], exc)
    if e.override:
        return e.override
    if since <= 0 or (e.ts and e.ts < since - _SKEW):
        return ""
    return e.email


async def fill(views) -> None:
    """Set `.account` on managed and foreign views; closed ones stay empty."""
    for v in views:
        if v.kind not in ("managed", "foreign") or v.status in ("gone", "dead"):
            continue
        v.account = await email_for(v.session_id, since=v.started_at)


def note_login(session_id: str, email: str) -> None:
    """The bot knows this session's account before its transcript does.

    Held until the session writes a record of its own after this moment.
    """
    if not email:
        return
    e = _entry(session_id, "")
    if e is None:
        _evict()
        e = _cache[session_id] = _Entry(path=Path("/nonexistent"))
    try:
        size = e.path.stat().st_size
    except OSError:
        size = 0
    e.override, e.override_at = email, size


def current_login() -> str:
    """The account of the last `/login` (default profile) — never per session."""
    global _login_cache
    path = _claude_json()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ""
    if mtime == _login_cache[0]:
        return _login_cache[1]
    try:
        data = json.loads(path.read_text())
        email = (data.get("oauthAccount") or {}).get("emailAddress") or ""
    except (OSError, ValueError, AttributeError):
        email = ""
    _login_cache = (mtime, email if isinstance(email, str) else "")
    return _login_cache[1]


def same(a: str, b: str) -> bool:
    return bool(a) and a.lower() == b.lower()
