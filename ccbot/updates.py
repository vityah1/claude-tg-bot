"""Which Claude Code a session runs, and which one is waiting on disk.

Claude Code updates itself in the background, but a running process keeps the
build it started with: a session picks the new one up only when its `claude`
is launched again. Nothing in the TUI says that a restart is due, so the two
numbers are read here and compared.

Where each number comes from:

* on disk — `claude --version`, cached: it is a node process, and the answer
  only moves when the background updater has been at work.
* in the session — the ``version`` field of the status-line payload
  (`status_feed`), with the transcript as the fallback for an install without
  the tee. Both are written by the process itself, so they name its build and
  not the file on disk.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from . import sessions as sess
from . import status_feed, transcript

log = logging.getLogger("ccbot.updates")

# `claude --version` prints "2.1.251 (Claude Code)".
_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")
# How long the disk version is trusted without asking again.
_INSTALLED_TTL = 300.0
# How long `claude --version` is given before the answer is written off.
_PROBE_TIMEOUT = 20.0
# After a restart both sources need a moment to name the new build: the
# payload is rewritten on the first status-line render, the transcript on the
# first record. Until then the session must not be flagged as outdated again.
_SETTLE = 90.0

_installed_at = 0.0
_installed = ""
# session_id → when the bot last relaunched it.
_restarted: dict[str, float] = {}


def parse(version: str) -> tuple[int, ...]:
    """"2.1.251 (Claude Code)" → (2, 1, 251). Empty when there is no number."""
    m = _VERSION_RE.search(version or "")
    return tuple(int(p) for p in m.group(0).split(".")) if m else ()


async def installed(force: bool = False) -> str:
    """The version on disk — what the next `claude` start will run."""
    global _installed, _installed_at
    if not force and _installed and time.monotonic() - _installed_at < _INSTALLED_TTL:
        return _installed
    exe = sess.claude_bin()
    if not exe:
        return _installed
    try:
        proc = await asyncio.create_subprocess_exec(
            exe, "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out = (await asyncio.wait_for(proc.communicate(), _PROBE_TIMEOUT))[0]
    except (OSError, TimeoutError) as exc:
        log.warning("claude --version failed: %s", exc)
        return _installed
    m = _VERSION_RE.search(out.decode("utf-8", "replace"))
    if not m:
        log.warning("claude --version said %r", out[:200])
        return _installed
    if m.group(0) != _installed:
        log.info("claude on disk: %s (was %s)", m.group(0), _installed or "unknown")
    _installed, _installed_at = m.group(0), time.monotonic()
    return _installed


def cached_installed() -> str:
    """The last known disk version, without going out to ask for it."""
    return _installed


def running(session_id: str) -> str:
    """The version the session's own process is running, as far as can be seen.

    The payload is preferred over the transcript because it is rewritten on
    every status-line render, while a transcript only grows when something is
    said. Neither is proof against a session that has not drawn or written a
    thing since it started, which is what the settling window below is for.
    """
    usage = status_feed.read(session_id)
    if usage and usage.version:
        return usage.version
    return transcript.last_version(session_id)


def note_restarted(session_id: str) -> None:
    """Remember that this session was just relaunched by the bot."""
    _restarted[session_id] = time.monotonic()


def settled(session_id: str) -> None:
    """Its version has been read since the restart — stop holding off."""
    _restarted.pop(session_id, None)


def settling(session_id: str) -> bool:
    """Whether the session was restarted too recently to judge its version."""
    since = _restarted.get(session_id)
    return since is not None and time.monotonic() - since < _SETTLE


def forget(session_id: str) -> None:
    _restarted.pop(session_id, None)


def outdated(session_version: str, disk_version: str) -> bool:
    """Whether a restart would actually change anything.

    Unknown on either side means "do not claim anything": a missing payload is
    not evidence of an old build, and an upgrade badge nobody can act on is
    worse than no badge at all.
    """
    a, b = parse(session_version), parse(disk_version)
    return bool(a and b and a < b)


def stale(session_id: str, disk_version: str) -> str:
    """The session's version if it is behind *disk_version*, else ""."""
    if settling(session_id):
        return ""
    mine = running(session_id)
    return mine if outdated(mine, disk_version) else ""


async def update_cli(timeout: float = 300.0) -> tuple[bool, str]:
    """Run `claude update` and hand back what it said.

    The updater talks to the network and may download a release, so the
    timeout is generous; the caller is expected to have said it is working.
    """
    exe = sess.claude_bin()
    if not exe:
        return False, "claude CLI not found"
    try:
        proc = await asyncio.create_subprocess_exec(
            exe, "update",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out = (await asyncio.wait_for(proc.communicate(), timeout))[0]
    except TimeoutError:
        log.warning("claude update timed out after %.0f s", timeout)
        return False, f"timed out after {timeout:.0f} s"
    except OSError as exc:
        log.warning("claude update failed: %s", exc)
        return False, str(exc)
    text = out.decode("utf-8", "replace").strip()
    log.info("claude update rc=%s: %s", proc.returncode, text[:300])
    return proc.returncode == 0, text
