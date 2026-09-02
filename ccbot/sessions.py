"""Unified view over Claude Code sessions.

Three kinds are surfaced:

* ``managed``  — created by the bot inside tmux; fully controllable.
* ``foreign``  — the user's own terminal sessions. Read-only by design: their
                 stdin is unreachable from outside, and hijacking them would
                 disturb work in progress.
* ``closed``   — historical transcripts that can be resumed into a new window.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import status_feed, tmux, transcript
from .i18n import N_, _
from .state import Store

log = logging.getLogger("ccbot.sessions")

HOME = str(Path.home()).rstrip("/")
PROJECTS_DIR = Path.home() / ".claude" / "projects"
CLAUDE_JSON = Path.home() / ".claude.json"

_LIVE_TTL = 5.0            # seconds; `claude agents --json` spawns a node process
_HISTORY_LIMIT = 25
_HEAD_BYTES = 32 * 1024
_TAIL_BYTES = 64 * 1024
# Below this, and with no prompt of its own, a transcript holds nothing useful.
_EMPTY_SESSION_BYTES = 20 * 1024
# How much of a button caption the path may take.
_DIR_BUDGET = 22


# What each status means, in words. The keys come from `claude agents --json`
# and from tmux, and are never shown raw: "gone" is a state, not a sentence.
_STATUS_LABELS = {
    "busy": N_("working"),
    "idle": N_("idle"),
    "waiting": N_("waiting for you"),
    "starting": N_("starting"),
    "dead": N_("dead"),
    "gone": N_("window is gone"),
    "closed": N_("closed"),
}


def status_label(status: str) -> str:
    msgid = _STATUS_LABELS.get(status)
    return _(msgid) if msgid else status


def short_when(mtime: float) -> str:
    """Timestamp trimmed for a button, where every character counts."""
    if not mtime:
        return "—"
    ts = datetime.fromtimestamp(mtime)
    delta = (datetime.now().date() - ts.date()).days
    if delta == 0:
        return f"{ts:%H:%M}"
    if delta == 1:
        # TRANSLATORS: "yesterday" abbreviated to fit a button caption.
        return _("yst {time}").format(time=f"{ts:%H:%M}")
    # TRANSLATORS: strftime format — day and month, on a button.
    return ts.strftime(_("%d.%m"))


def short_dir(path: str, budget: int = _DIR_BUDGET) -> str:
    """Path trimmed for a button, but still recognisable as a path.

    The bare folder name reads like part of the session title; keeping the
    leading ~ and an ellipsis makes it obvious this is where it runs.
    """
    if not path:
        return "?"
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + "/"):
        path = "~/" + path[len(home) + 1:]
    if len(path) <= budget:
        return path
    parts = path.split("/")
    tail = parts[-1]
    # Keep the parent when it still fits — "…/backend/pay4say" says more
    # than "…/pay4say".
    if len(parts) > 1 and len("…/" + parts[-2] + "/" + tail) <= budget:
        return "…/" + parts[-2] + "/" + tail
    return "…/" + tail


_PATH_LINE = re.compile(r"^(?:\d+\.\s*)?[~/][^\s]*$")
# What `util.default_name` produces: the folder plus four hex characters.
_AUTO_NAME = re.compile(r"^.+-[0-9a-f]{4}$")


def clean_prompt(text: str) -> str:
    """The user's own words, with the wrapping the bot itself added removed.

    An attachment reaches Claude as "Take a look at these 3 images:" followed
    by the paths and only then the caption, and a forwarded batch as a heading
    followed by "who: what" lines. Taken as a caption, both say nothing about
    the session — on 2026-09-01 six of the newest twenty-four rows in pay4say
    read as a media path. The headings are matched structurally, never by
    their wording: they are translated, and a Ukrainian install would slip
    straight past an English pattern.
    """
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    i = 0
    # A heading that introduces a list of paths, and the paths themselves.
    while i < len(lines):
        ln = lines[i].strip()
        if not ln:
            i += 1
            continue
        if _PATH_LINE.match(ln):
            i += 1
            continue
        if ln.endswith(":") and any(
                _PATH_LINE.match(x.strip())
                for x in lines[i + 1:i + 4] if x.strip()):
            i += 1
            continue
        break
    rest = [ln for ln in lines[i:] if ln.strip()]
    if not rest:
        return (text or "").strip()
    # A forwarded dump: drop its heading, keep what people actually wrote.
    if rest[0].strip().endswith(":") and len(rest) > 1 and ": " in rest[1]:
        rest = rest[1:]
    # Everything that is left, not just its first line: a prompt that opens
    # with "у нас есть в АПИ" and explains itself below would otherwise be
    # captioned by the half that says nothing.
    return "\n".join(rest).strip() or (text or "").strip()


def hand_written(name: str | None, cwd: str | None) -> str:
    """The launch name (`claude -n`), unless the bot generated it itself."""
    n = (name or "").strip()
    if not n or _AUTO_NAME.match(n):
        return ""
    # A name the bot made for another directory is still no use as a label.
    return "" if n == Path(cwd or "").name else n


def dir_key(path: str) -> str:
    """Stable short id for a directory, for use in callback_data.

    A position in a list would be shorter, but it moves as soon as a session
    is opened or closed: an older card would then quietly page through a
    different project. A hash of the path stays valid across restarts.
    """
    return hashlib.sha1(path.rstrip("/").encode()).hexdigest()[:8]


def under_roots(path: str | None, roots: tuple[str, ...]) -> bool:
    """True if *path* is one of *roots* or nested inside one of them.

    The home directory passes as well, but only as an exact match. Sessions
    do get started in `~`, and it is the one directory that cannot be put on
    the shortlist: as a root it would swallow every project below it and
    leave no shortlist at all. Without this its transcripts were invisible —
    the history offered every project directory except the one they were all
    inside.
    """
    if not path:
        return False
    if not roots:
        return True
    p = path.rstrip("/")
    if p == HOME:
        return True
    for root in roots:
        r = root.rstrip("/")
        if p == r or p.startswith(r + "/"):
            return True
    return False


@dataclass
class SessionView:
    session_id: str
    name: str
    cwd: str
    kind: str                     # managed | foreign | closed
    status: str = ""              # busy | idle | waiting | dead
    waiting_for: str = ""
    window_id: str | None = None
    title: str | None = None
    mtime: float = 0.0
    size: int = 0
    opening: str = ""
    started_at: float = 0.0
    pid: int = 0
    work_dir: str = ""   # where it is working now, if that differs
    saved_name: str = ""  # what the user called it, kept past the session
    launched: str = ""    # `claude -n`, when a human wrote it
    last: str = ""        # the prompt it stopped on

    @property
    def when(self) -> str:
        """Last activity, phrased for a glance rather than for precision."""
        if not self.mtime:
            return "—"
        ts = datetime.fromtimestamp(self.mtime)
        today = datetime.now().date()
        delta = (today - ts.date()).days
        if delta == 0:
            return _("today {time}").format(time=f"{ts:%H:%M}")
        if delta == 1:
            return _("yesterday {time}").format(time=f"{ts:%H:%M}")
        if ts.year == today.year:
            # TRANSLATORS: strftime format — a date this year, plus the time.
            return ts.strftime(_("%d.%m %H:%M"))
        # TRANSLATORS: strftime format — a date in an earlier year.
        return ts.strftime(_("%d.%m.%y"))

    @property
    def when_short(self) -> str:
        return short_when(self.mtime)

    @property
    def dir_name(self) -> str:
        # The launch directory, not the current one: it decides which CLAUDE.md
        # and permissions the session got, so it is what identifies it in a list.
        return short_dir(self.cwd)

    @property
    def short_cwd(self) -> str:
        home = str(Path.home())
        c = self.cwd
        if c.startswith(home):
            c = "~" + c[len(home):]
        return c if len(c) <= 44 else "…" + c[-43:]


@dataclass
class DirStat:
    """A directory as a row in the history list."""
    cwd: str
    count: int
    mtime: float

    @property
    def key(self) -> str:
        return dir_key(self.cwd)

    @property
    def dir_name(self) -> str:
        return short_dir(self.cwd)

    @property
    def when_short(self) -> str:
        return short_when(self.mtime)


_claude_path: str | None = None


def claude_bin() -> str | None:
    """Absolute path to the `claude` CLI.

    systemd user units do not read the login shell profile, so nvm's bin
    directory is absent from PATH and a bare "claude" fails to launch. Resolve
    it once, explicitly.
    """
    global _claude_path
    if _claude_path:
        return _claude_path
    candidates = []
    override = os.getenv("CCBOT_CLAUDE_BIN")
    if override:
        candidates.append(Path(override))
    found = shutil.which("claude")
    if found:
        candidates.append(Path(found))
    candidates += sorted(
        (Path.home() / ".nvm" / "versions" / "node").glob("*/bin/claude"),
        reverse=True,
    )
    candidates += [
        Path.home() / ".local" / "bin" / "claude",
        Path("/usr/local/bin/claude"),
        Path("/usr/bin/claude"),
    ]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            _claude_path = str(c)
            log.info("claude CLI: %s", _claude_path)
            return _claude_path
    log.error("claude CLI not found — live session lists will be empty")
    return None


_live_cache: tuple[float, list[dict]] = (0.0, [])


async def live_agents(force: bool = False) -> list[dict]:
    """Active sessions per `claude agents --json` (TTY-independent)."""
    global _live_cache, _claude_path
    ts, cached = _live_cache
    if not force and time.time() - ts < _LIVE_TTL:
        return cached
    exe = claude_bin()
    if not exe:
        return cached
    try:
        proc = await asyncio.create_subprocess_exec(
            exe, "agents", "--json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            log.warning("claude agents --json rc=%s: %s",
                        proc.returncode, err.decode()[:200])
            return cached
        data = json.loads(out.decode() or "[]")
        if not isinstance(data, list):
            data = []
    except (TimeoutError, OSError, json.JSONDecodeError, ValueError) as exc:
        log.warning("claude agents --json failed: %s", exc)
        if isinstance(exc, OSError):
            # Claude Code updating itself moves the binary out from under the
            # path resolved at startup. Forget it, so the next call looks again.
            _claude_path = None
        # The timestamp is deliberately left alone: the caller still gets the
        # last known answer, but `live_age` now shows how old that answer is.
        # Refreshing it here is what let a stale "waiting" look current.
        return cached
    _live_cache = (time.time(), data)
    return data


def live_age() -> float:
    """Seconds since `claude agents --json` last actually answered."""
    ts, _data = _live_cache
    return time.time() - ts if ts else float("inf")


async def managed_views(store: Store) -> list[SessionView]:
    live = {a.get("sessionId"): a for a in await live_agents()}
    windows = {w.id: w for w in await tmux.list_windows()}
    views = []
    for m in store.all_managed():
        w = windows.get(m.window_id)
        agent = live.get(m.session_id)
        if w is None:
            status = "gone"
        elif not w.alive:
            status = "dead"
        elif agent:
            status = agent.get("status", "idle")
        else:
            status = "starting"
        views.append(SessionView(
            session_id=m.session_id, name=m.label, cwd=m.cwd, kind="managed",
            status=status, waiting_for=(agent or {}).get("waitingFor", ""),
            window_id=m.window_id, title=m.title, mtime=m.created_at,
        ))
    return views


async def foreign_views(store: Store) -> list[SessionView]:
    mine = {m.session_id for m in store.all_managed()}
    out = []
    for a in await live_agents():
        sid = a.get("sessionId")
        if not sid or sid in mine:
            continue
        started = float(a.get("startedAt") or 0) / 1000
        # The status-line payload knows the current directory; agents --json
        # only reports the one the session was launched from.
        usage = status_feed.read(sid)
        work = (usage.cwd if usage else "") or ""
        out.append(SessionView(
            session_id=sid, name=a.get("name") or sid[:8], cwd=a.get("cwd", ""),
            kind="foreign", status=a.get("status", ""),
            waiting_for=a.get("waitingFor", ""), mtime=started,
            started_at=started, pid=int(a.get("pid") or 0),
            work_dir=work if work != a.get("cwd", "") else "",
        ))
    return out


@dataclass
class Scan:
    """What one transcript says about itself."""
    session_id: str | None = None
    cwd: str | None = None
    title: str | None = None        # the AI-generated one, when there is one
    opening: str | None = None      # first prompt, cleaned
    launched: str | None = None     # `claude -n`, only when a human wrote it
    last: str | None = None         # the prompt the session stopped on


# Long enough for the detail card to say something; a button trims further.
_CAPTION_LIMIT = 200


def _caption(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = " ".join(clean_prompt(text).split())
    return cleaned[:_CAPTION_LIMIT] or None


def _json_field(line: str, key: str) -> str | None:
    try:
        value = json.loads(line).get(key)
    except ValueError:
        return None
    return value if isinstance(value, str) else None


def _scan_file(path: Path, store: Store) -> Scan:
    """Everything a list row and a detail card need, cached per (path, mtime, size)."""
    try:
        st = path.stat()
    except OSError:
        return Scan()
    hit = store.cached_history(str(path), st.st_mtime, st.st_size)
    if hit:
        return Scan(hit["session_id"], hit["cwd"], hit["title"], hit["opening"],
                    hit["launched"], hit["last"])

    sid = path.stem
    cwd = title = opening = launched = last = None
    try:
        with path.open("rb") as fh:
            # Streamed rather than slurped: the opening prompt sits behind a
            # file-history snapshot that can run to hundreds of kilobytes.
            read = 0
            for raw in fh:
                read += len(raw)
                if read > transcript.PROMPT_SCAN_BYTES:
                    break
                line = raw.decode("utf-8", "replace")
                if cwd is None and '"cwd"' in line:
                    cwd = _json_field(line, "cwd")
                if opening is None and '"type":"user"' in line.replace(" ", ""):
                    opening = transcript.prompt_text(line)
                if launched is None and '"custom-title"' in line:
                    launched = _json_field(line, "customTitle")
                # Claude Code writes the title it invents early in the file,
                # not at the end: looking only at the tail found it in a
                # handful of transcripts and missed it in the rest.
                if '"ai-title"' in line:
                    title = _json_field(line, "aiTitle") or title
                if cwd and opening and launched and title:
                    break
            if st.st_size > _TAIL_BYTES:
                fh.seek(-_TAIL_BYTES, 2)
                fh.readline()          # drop the partial line
            else:
                fh.seek(0)
            for line in fh.read().decode("utf-8", "replace").splitlines():
                if '"ai-title"' in line:
                    title = _json_field(line, "aiTitle") or title
                if '"type":"user"' in line.replace(" ", ""):
                    # The prompt it stopped on says more about a session than
                    # the one it started with — that is what "where was I"
                    # actually means.
                    last = transcript.prompt_text(line) or last
    except OSError:
        pass
    # Cleaned while the line breaks are still there, then flattened: the
    # caption is one line, but the layout is what tells a path from a caption.
    opening = _caption(opening)
    last = _caption(last)
    launched = hand_written(launched, cwd) or None
    store.cache_history(str(path), st.st_mtime, st.st_size, sid, cwd, title,
                        opening, launched, last)
    return Scan(sid, cwd, title, opening, launched, last)


def _closed_view(scan: Scan, mtime: float, size: int, saved: str) -> SessionView:
    """A closed session as a row, named by the best of what is known.

    The order is what the reader recognises soonest: the name they gave it
    themselves, then a name a human typed at `claude -n`, then the title
    Claude Code invents, and only then the first thing that was asked. The
    last is the weakest — Claude Code stopped writing `ai-title` for most
    sessions in late August 2026, so it is also the most common.
    """
    name = (saved or scan.launched or scan.title or scan.opening
            or _("untitled"))
    return SessionView(
        session_id=scan.session_id or "", name=name, cwd=scan.cwd or "",
        kind="closed", status="closed", title=scan.title, mtime=mtime,
        size=size, opening=scan.opening or "", saved_name=saved,
        launched=scan.launched or "", last=scan.last or "",
    )


async def closed_all(store: Store, roots: tuple[str, ...] = (),
                     cwd: str | None = None,
                     query: str = "") -> list[SessionView]:
    """Every resumable transcript, newest first.

    The whole list, not a page of it: the caller needs the total to page
    through it, and a directory with eighty sessions in it is the normal case.
    Rescanning is cheap because `_scan_file` caches per (path, mtime, size) —
    a full pass over two hundred transcripts costs milliseconds once warm.
    """
    live_ids = {a.get("sessionId") for a in await live_agents()}
    # A managed session is off the list even when the CLI has stopped naming
    # it: after `/clear` the live process writes under a new id, leaving the
    # old transcript to look closed. Resuming that one would take over the
    # database row of a session that is still in a tmux window.
    live_ids |= {m.session_id for m in store.all_managed()}
    saved_names = store.saved_names()
    want = cwd.rstrip("/") if cwd else None
    needle = query.strip().casefold()
    files = []
    for p in PROJECTS_DIR.glob("*/*.jsonl"):
        try:
            files.append((p.stat().st_mtime, p))
        except OSError:
            continue
    files.sort(reverse=True)

    views: list[SessionView] = []
    for mtime, path in files:
        if path.stem in live_ids:
            continue
        scan = _scan_file(path, store)
        if not scan.session_id or not scan.cwd:
            continue
        if not under_roots(scan.cwd, roots):
            continue
        if want is not None and scan.cwd.rstrip("/") != want:
            continue
        # Judge by content, not size: a session that only ran /exit still
        # weighs hundreds of KB because of its file-history snapshot.
        if not scan.title and not scan.opening:
            continue
        saved = saved_names.get(scan.session_id, "")
        if needle and not _matches(scan, saved, needle):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        views.append(_closed_view(scan, mtime, size, saved))
    return views


def _matches(scan: Scan, saved: str, needle: str) -> bool:
    """Whether a session answers a search.

    Everything the scan already holds is searched — the names, the first
    prompt and the last one. Not the transcript itself: reading two hundred
    files of a megabyte each on every keystroke-sized query is the difference
    between a search and a wait.
    """
    haystack = " ".join(filter(None, (
        saved, scan.launched, scan.title, scan.opening, scan.last,
        scan.session_id)))
    return needle in haystack.casefold()


async def closed_views(store: Store, limit: int = _HISTORY_LIMIT,
                       roots: tuple[str, ...] = (), cwd: str | None = None,
                       offset: int = 0) -> list[SessionView]:
    """One page of `closed_all`."""
    views = await closed_all(store, roots, cwd)
    return views[offset:offset + limit] if limit else views[offset:]


async def closed_dirs(store: Store,
                      roots: tuple[str, ...] = ()) -> list[DirStat]:
    """Directories that have resumable sessions, busiest activity first.

    Sorted by recency rather than by count: the project worked on an hour ago
    is the one being looked for, however few sessions it holds.
    """
    stats: dict[str, DirStat] = {}
    for v in await closed_all(store, roots):
        st = stats.get(v.cwd)
        if st is None:
            stats[v.cwd] = DirStat(cwd=v.cwd, count=1, mtime=v.mtime)
        else:
            st.count += 1
            st.mtime = max(st.mtime, v.mtime)
    return sorted(stats.values(), key=lambda s: s.mtime, reverse=True)


def find_transcript(short: str) -> Path | None:
    """The transcript whose session id starts with *short*.

    A session id is the file name, so this is a direct lookup — no scanning a
    window of recent sessions, which is what used to make a button on a later
    page resolve to nothing.
    """
    # Nothing but an id ever reaches the glob: "*" or "?" in a callback would
    # otherwise match a transcript nobody asked for.
    if not short or not all(ch.isalnum() or ch == "-" for ch in short):
        return None
    matches = []
    for path in PROJECTS_DIR.glob(f"*/{short}*.jsonl"):
        try:
            matches.append((path.stat().st_mtime, path))
        except OSError:
            continue
    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][1]


async def closed_view(store: Store, short: str,
                      roots: tuple[str, ...] = ()) -> SessionView | None:
    """A single closed session, found by the first characters of its id."""
    path = find_transcript(short)
    if path is None:
        return None
    scan = _scan_file(path, store)
    if not scan.session_id or not scan.cwd or not under_roots(scan.cwd, roots):
        return None
    if (store.get(scan.session_id)
            or scan.session_id in {a.get("sessionId") for a in await live_agents()}):
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    return _closed_view(scan, st.st_mtime, st.st_size,
                        store.saved_name(scan.session_id) or "")


async def recent_dirs(store: Store, limit: int = 12,
                      roots: tuple[str, ...] = ()) -> list[str]:
    """Directories worth offering when starting a session.

    Configured roots come first and are always present — they are the explicit
    entry points. Below them come sub-directories that were actually worked in
    recently, newest first, so live projects stay one tap away.
    """
    seen: list[str] = []

    def push(path: str | None) -> None:
        if not path or path in seen:
            return
        if not under_roots(path, roots):
            return
        if Path(path).is_dir():
            seen.append(path)

    for root in roots:
        push(root)

    for a in await live_agents():
        push(a.get("cwd"))
    for v in await closed_all(store, roots):
        push(v.cwd)
        if len(seen) >= limit:
            break
    return seen[:limit]
