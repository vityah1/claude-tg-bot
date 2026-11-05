"""Which Claude account each session runs under.

A `/login` in one session moves every running session of the same profile:
each sends its next request with the new credentials, no restart (verified
on 2.1.318, 2026-11-05 — finman 0585ce76, started at 11:44, sent its first
request after a login made elsewhere at 13:39 as the new organisation). What a
session has *not* done yet is say so: `~/.claude.json` names the last login,
not what a session that has been quiet since then ran under.

So the transcript is read, and it carries two attachment records:

* `session_context`, with the first request after a start or a resume and
  after the session's own `/login` — its `userEmail` names the address;
* `credential_org`, written beside it and **alone** when the credentials
  changed under a running session — an organisation id, no address.

The newest of the two is the account in force. A `credential_org` newer than
the last address is resolved through the organisations whose address is known:
the pair written together in any transcript read, and the last login's own
(`oauthAccount` in `~/.claude.json`). One that cannot be resolved says nothing
rather than the stale address. Two more rules keep the answer honest:

* a record older than the running process belongs to the previous run (a
  resume appends to the same file, and writes its own record only with the
  first prompt), so it is not trusted — nothing is shown instead;
* the session that has just logged in, or was just restarted by the bot, has
  no new record until its next request, so the bot remembers what it knows
  (`note_login`) until the transcript catches up.

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

from . import paths, status_feed, transcript

log = logging.getLogger("ccbot.account")

_MARK = b'"type":"session_context"'
_ORG_MARK = b'"type":"credential_org"'
# How far after a `session_context` its `credential_org` is looked for; they
# share a timestamp, but other attachments of the same request may sit
# between them (more than ten lines in some transcripts).
_PAIR_LINES = 200
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
    email_org: str = ""      # the organisation written beside that address
    org: str = ""            # the newest `credential_org`
    org_ts: float = 0.0
    override: str = ""       # what the bot knows before the transcript does
    override_at: int = 0     # file size when the override was set


_cache: dict[str, _Entry] = {}
_login_cache: tuple[float, str] = (0.0, "")
# Organisation id -> the address last seen with it.
# Organisation id -> every address seen with it. An organisation can hold
# several accounts (a team), and then the id alone names none of them. Kept on
# disk, so a bot restart does not forget organisations no live session pairs.
_orgs: dict[str, set[str]] | None = None


def _parse(line: bytes, kind: str = "session_context") -> tuple[str, float] | None:
    """The value and the time of one attachment line of type *kind*.

    For `session_context` the value is the address, for `credential_org` the
    organisation id. None when the line is not a readable record of that
    kind — the caller then keeps looking, or keeps what it knew. An empty
    address from a readable record is an answer (an API-key session has none).
    """
    try:
        rec = json.loads(line.decode("utf-8", "replace"))
    except ValueError:
        return None
    att = rec.get("attachment") if isinstance(rec, dict) else None
    if not isinstance(att, dict) or att.get("type") != kind:
        return None
    if kind == "credential_org":
        org = att.get("organizationUuid")
        value = org if isinstance(org, str) else ""
    else:
        ctx = att.get("context")
        said = ctx.get("userEmail") if isinstance(ctx, dict) else None
        m = _EMAIL_RE.search(said) if isinstance(said, str) else None
        value = m.group(0) if m else ""
    ts = 0.0
    raw_ts = rec.get("timestamp")
    if isinstance(raw_ts, str):
        try:
            ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts = 0.0
    return value, ts


def _orgs_file() -> Path:
    return paths.cache_dir() / "account-orgs.json"


def _org_map() -> dict[str, set[str]]:
    global _orgs
    if _orgs is None:
        _orgs = {}
        try:
            raw = json.loads(_orgs_file().read_text())
            for org, emails in raw.items():
                if isinstance(org, str) and isinstance(emails, list):
                    _orgs[org] = {x for x in emails if isinstance(x, str) and x}
        except (OSError, ValueError, AttributeError) as exc:
            log.debug("account orgs not loaded: %s", exc)
    return _orgs


def _learn(org: str, email: str) -> None:
    """Remember that *email* was seen under *org*; saved when it is news."""
    if not org or not email:
        return
    known = _org_map().setdefault(org, set())
    if email.lower() in {x.lower() for x in known}:
        return
    known.add(email)
    try:
        f = _orgs_file()
        paths.ensure(f.parent)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: sorted(v) for k, v in _org_map().items()}))
        tmp.replace(f)
    except OSError as exc:
        log.debug("account orgs not saved: %s", exc)


def _name_of(org: str) -> str:
    """The one address known for *org*, or "" when none or several are."""
    current_login()                           # learns the last login's org
    known = _org_map().get(org, set())
    return next(iter(known)) if len(known) == 1 else ""


def _paired_org(fh, start: int, end: int, ts: float) -> str:
    """The `credential_org` written with the `session_context` at *start*."""
    pos = start
    for _i in range(_PAIR_LINES + 1):
        if pos >= end:
            break
        line = _line_from(fh, pos, end)
        pos += len(line) + 1
        if _ORG_MARK in line:
            got = _parse(line, "credential_org")
            if got is not None and abs(got[1] - ts) <= _SKEW:
                return got[0]
    return ""


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


def _newest(fh, floor: int, end: int, mark: bytes = _MARK,
            kind: str = "session_context") -> tuple[int, tuple[str, float]] | None:
    """The newest readable record in [floor, end), read backwards in chunks.

    A record cut off mid-file (a crash while it was written) does not end the
    search: the one before it still says who the session was.
    """
    pos = end
    overlap = len(mark)
    while pos > floor:
        lo = max(floor, pos - _CHUNK)
        fh.seek(lo)
        buf = fh.read(min(pos + overlap, end) - lo)
        hit = buf.rfind(mark)
        while hit >= 0:
            start = _line_start(fh, lo + hit, floor)
            got = _parse(_line_from(fh, start, end), kind)
            if got is not None:
                return start, got
            # Unreadable: look further back, before this line.
            if start <= lo:
                pos = start
                break
            hit = buf.rfind(mark, 0, start - lo)
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
        e.offset, e.email, e.ts, e.email_org = 0, "", 0.0, ""
        e.org, e.org_ts = "", 0.0
    if size == e.offset:
        return
    with e.path.open("rb") as fh:
        # Only whole lines: a half-written last line is read on the next pass.
        end = _last_newline(fh, size, e.offset)
        if end == e.offset:
            return
        found = _newest(fh, e.offset, end)
        floor = e.offset
        if found is not None:
            start, (e.email, e.ts) = found
            e.email_org = _paired_org(fh, start, end, e.ts)
            _learn(e.email_org, e.email)
            if e.override and start >= e.override_at:
                e.override = ""
            # Its organisation is written beside it or later, never before:
            # no need to read the file a second time back to the start.
            floor = start
        org = _newest(fh, floor, end, _ORG_MARK, "credential_org")
        if org is not None:
            start, (e.org, e.org_ts) = org
            if not e.email_org and abs(e.org_ts - e.ts) <= _SKEW:
                # The pair was split by a pass ending between the two lines.
                e.email_org = e.org
                _learn(e.org, e.email)
            if e.override and start >= e.override_at:
                e.override = ""
        e.offset = end


def _resolve(e: _Entry, since: float) -> str:
    """The address in force: the last one named, unless the org moved since.

    The address counts only if this run wrote it (*since*); otherwise only
    the organisation can name the account.
    """
    fresh = not e.ts or e.ts >= since - _SKEW
    if not e.org or e.org_ts <= e.ts + _SKEW:
        return e.email if fresh else ""
    if fresh and e.org == e.email_org and len(_org_map().get(e.org, ())) <= 1:
        # Moved away and back again (0585ce76 on 2026-11-05).
        return e.email
    return _name_of(e.org)


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
    latest = max(e.ts, e.org_ts)
    if since <= 0 or (latest and latest < since - _SKEW):
        return ""
    return _resolve(e, since)


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
        acct = data.get("oauthAccount") or {}
        email = acct.get("emailAddress") or ""
        org = acct.get("organizationUuid") or ""
    except (OSError, ValueError, AttributeError):
        email, org = "", ""
    email = email if isinstance(email, str) else ""
    if isinstance(org, str):
        _learn(org, email)
    _login_cache = (mtime, email)
    return _login_cache[1]
