"""The bot's own process: what state it is in, and how it restarts itself.

Restarting is only possible when something is watching the process — the
systemd unit in contrib/ with Restart=always. Under it, exiting is the whole
restart: systemd brings the bot back five seconds later. Started by hand there
is nothing to bring it back, so the restart path refuses instead of leaving a
dead bot and a chat that never answers again.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path

from . import paths
from .i18n import _

log = logging.getLogger("ccbot.service")

STARTED = time.time()
UNIT = "claude-tg-bot.service"
# Written just before exiting, read once on the way back up.
_FLAG = paths.cache_dir() / "restart.json"
# A restart flag older than this belongs to a run that never came back.
_FLAG_TTL = 600.0


def under_systemd() -> bool:
    """Whether this process *is* the unit — so exiting means being restarted.

    Not INVOCATION_ID: the tmux server the bot starts inherits it, so every
    Claude session — and a bot launched by hand from one of them — would claim
    to be supervised and exit into nothing. The cgroup says where the process
    actually lives.
    """
    try:
        cgroup = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        cgroup = ""
    if cgroup:
        return UNIT in cgroup
    return bool(os.getenv("INVOCATION_ID"))


def uptime() -> float:
    return time.time() - STARTED


def human_delta(seconds: float) -> str:
    """Duration in the units a person would actually say out loud."""
    seconds = int(max(0, seconds))
    # The numbers are formatted before translation so that no format spec ever
    # ends up inside a string a translator edits.
    if seconds < 60:
        # TRANSLATORS: a duration under a minute. "s" is the seconds unit.
        return _("{s} s").format(s=seconds)
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        # TRANSLATORS: minutes and seconds, e.g. "7 min 05 s".
        return _("{m} min {s} s").format(m=minutes, s=f"{sec:02d}")
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        # TRANSLATORS: hours and minutes, e.g. "3 h 09 min".
        return _("{h} h {m} min").format(h=hours, m=f"{minutes:02d}")
    days, hours = divmod(hours, 24)
    # TRANSLATORS: days and hours, e.g. "2 d 5 h".
    return _("{d} d {h} h").format(d=days, h=hours)


# (kind, detail) rather than a finished sentence: the language can change
# while the process runs, and a cached translation would not follow it.
_version: tuple[str, str] | None = None


def version() -> str:
    """Commit the bot is running, plus a warning if the tree has moved on."""
    global _version
    if _version is None:
        _version = _read_version()
    kind, detail = _version
    if kind == "unknown":
        return _("unknown")
    if kind == "not-git":
        return _("not a git repository")
    if kind == "dirty":
        return _("{commit} + uncommitted changes").format(commit=detail)
    return detail


def _read_version() -> tuple[str, str]:
    """Ask git once — the code cannot change under a running process.

    A restart is exactly what picks up a new commit, so this is asked once.
    """
    root = Path(__file__).resolve().parent.parent
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("git version unavailable: %s", exc)
        return ("unknown", "")
    if head.returncode != 0:
        return ("not-git", "")
    commit = head.stdout.strip()
    if dirty.returncode == 0 and dirty.stdout.strip():
        return ("dirty", commit)
    return ("clean", commit)


def note_restart(chat_id: int) -> None:
    """Remember who asked, so the bot can report back once it is up again."""
    try:
        paths.ensure(_FLAG.parent)
        _FLAG.write_text(json.dumps({"chat_id": chat_id, "at": time.time()}),
                         encoding="utf-8")
    except OSError:
        log.exception("could not write the restart flag")


def take_restart() -> dict | None:
    """The pending restart request, consumed so it fires only once."""
    try:
        data = json.loads(_FLAG.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.warning("restart flag unreadable — ignoring")
        data = None
    with contextlib.suppress(OSError):
        _FLAG.unlink(missing_ok=True)
    if not isinstance(data, dict) or "chat_id" not in data:
        return None
    if time.time() - float(data.get("at") or 0) > _FLAG_TTL:
        log.info("stale restart flag ignored")
        return None
    return data
