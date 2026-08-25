"""Thin async wrapper around the tmux CLI.

All Claude sessions the bot manages live as windows inside a single tmux
session (see SESSION). Windows are addressed by tmux window id (``@12``),
which is stable and unique — unlike names or indexes, which shift or collide.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass

log = logging.getLogger("ccbot.tmux")

SESSION = "ccbot"

# Wide, fixed geometry so the TUI renders predictably while detached.
WIDTH = 200
HEIGHT = 50


class TmuxError(RuntimeError):
    pass


@dataclass(frozen=True)
class Window:
    id: str  # tmux window id, e.g. "@12"
    name: str
    cwd: str
    pane_pid: int
    alive: bool  # False once the process exited (remain-on-exit)


async def _run(*args: str, stdin: bytes | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        "tmux", *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(stdin)
    if proc.returncode != 0:
        detail = err.decode().strip()
        log.debug("tmux %s -> rc=%s %s", " ".join(args), proc.returncode, detail)
        raise TmuxError(f"tmux {' '.join(args)}: {detail}")
    log.debug("tmux %s", " ".join(args))
    return out.decode()


async def _ok(*args: str) -> bool:
    try:
        await _run(*args)
        return True
    except TmuxError:
        return False


async def ensure_session() -> None:
    """Create the bot's tmux session if it is not there yet."""
    if await _ok("has-session", "-t", f"={SESSION}"):
        return
    await _run(
        "new-session", "-d", "-s", SESSION, "-n", "control",
        "-x", str(WIDTH), "-y", str(HEIGHT),
    )


async def create_window(name: str, cwd: str) -> str:
    """Open a new window in *cwd* and return its tmux window id."""
    await ensure_session()
    out = await _run(
        "new-window", "-t", f"={SESSION}", "-c", cwd, "-n", name,
        "-P", "-F", "#{window_id}",
    )
    wid = out.strip()
    # Per-window, never global: the tmux server is shared with the user's own
    # sessions and must not be reconfigured underneath them.
    #   remain-on-exit  — a crashed `claude` leaves its error visible
    #   aggressive-resize — attaching from a phone must not shrink the pane
    await _ok("set-window-option", "-t", wid, "remain-on-exit", "on")
    await _ok("set-window-option", "-t", wid, "aggressive-resize", "off")
    return wid


async def rename_window(window_id: str, name: str) -> bool:
    """Relabel a window so `tmux attach` shows the same name as Telegram."""
    return await _ok("rename-window", "-t", window_id, name)


async def run_in_window(window_id: str, command: str) -> None:
    """Type a shell command into the window and press Enter."""
    await _run("send-keys", "-t", window_id, "-l", command)
    await _run("send-keys", "-t", window_id, "Enter")


async def list_windows() -> list[Window]:
    if not await _ok("has-session", "-t", f"={SESSION}"):
        return []
    fmt = "#{window_id}\t#{window_name}\t#{pane_current_path}\t#{pane_pid}\t#{pane_dead}"
    out = await _run("list-windows", "-t", f"={SESSION}", "-F", fmt)
    windows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        wid, name, cwd, pid, dead = line.split("\t")
        windows.append(Window(
            id=wid, name=name, cwd=cwd,
            pane_pid=int(pid or 0), alive=(dead != "1"),
        ))
    return windows


async def paste_text(window_id: str, text: str) -> None:
    """Insert *text* into the window using bracketed paste.

    Going through a buffer (rather than ``send-keys -l``) keeps quotes,
    semicolons and newlines intact, and bracketed paste stops Claude from
    submitting the prompt at the first newline of a multi-line message.
    """
    buf = f"ccbot-{window_id.lstrip('@')}"
    await _run("load-buffer", "-b", buf, "-", stdin=text.encode())
    await _run("paste-buffer", "-p", "-d", "-b", buf, "-t", window_id)


async def send_keys(window_id: str, *keys: str) -> None:
    """Send tmux key names (``Enter``, ``Escape``, ``C-c``, ``Down``…)."""
    await _run("send-keys", "-t", window_id, *keys)


async def send_literal(window_id: str, text: str) -> None:
    """Send text as literal keystrokes, without bracketed paste."""
    await _run("send-keys", "-t", window_id, "-l", text)


async def capture(window_id: str, history: int = 0) -> str:
    """Return the visible pane content as plain text (no escape sequences)."""
    args = ["capture-pane", "-p", "-t", window_id]
    if history:
        args += ["-S", f"-{history}"]
    return await _run(*args)


async def kill_window(window_id: str) -> None:
    await _ok("kill-window", "-t", window_id)


async def window_exists(window_id: str) -> bool:
    """Whether *window_id* is still a live window.

    `display-message -t <id>` falls back to the active window instead of
    failing when the target is gone, so the answer has to be verified against
    the id we asked about.
    """
    try:
        out = await _run("display-message", "-p", "-t", window_id, "#{window_id}")
    except TmuxError:
        return False
    return out.strip() == window_id


def attach_hint(window_id: str) -> str:
    """Command the user can run locally to take over a session."""
    return f"tmux attach -t {shlex.quote(SESSION)} \\; select-window -t {window_id}"


async def server_pid() -> int:
    """PID of the tmux server holding the sessions, or 0 if it is not up."""
    try:
        out = await _run("display-message", "-p", "-t", f"={SESSION}", "#{pid}")
    except TmuxError:
        return 0
    return int(out.strip() or 0)


async def claude_running(window_id: str) -> bool:
    """Whether a `claude` process still lives inside the window."""
    try:
        out = await _run("list-panes", "-t", window_id, "-F", "#{pane_current_command}")
    except TmuxError:
        return False
    return "claude" in out
