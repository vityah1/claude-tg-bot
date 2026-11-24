"""`/login` without a terminal: `claude auth login` driven under a pty.

The sessions have no TUI to open `/login` in ("isn't available in this
environment"), so the bot runs the CLI's own login command. Verified on
2026-11-24: it prints the claude.ai sign-in link and "Paste code here if
prompted >", and the current login stays in force until a new one succeeds.
The code the user sends from the chat is written to the pty; the process
exits with 0 once the login has taken. Credentials are shared by every
session of the machine, so one login serves all of them.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import pty
import re
import struct
import termios
import time

from .host.actor import claude_bin

log = logging.getLogger("ccbot.authlogin")

_URL_RE = re.compile(r"https://claude\.ai/oauth/authorize\S+")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\r")
_URL_WAIT = 30.0
_CODE_WAIT = 90.0
# A link nobody used is dropped after this long.
_STALE = 15 * 60.0
# What the sign-in page hands over: the code, "#", the state. Both halves are
# base64url, the state 43 characters of it (the code's length was never
# measured). Long and dotless, so "README.md#installation" is not one.
_CODE_TOKEN = r"[A-Za-z0-9_-]{20,}#[A-Za-z0-9_-]{20,}"
_CODE_RE = re.compile(rf"^{_CODE_TOKEN}$")
_CODE_ANYWHERE_RE = re.compile(rf"(?<![A-Za-z0-9_-]){_CODE_TOKEN}(?![A-Za-z0-9_-])")


def code_of(text: str) -> str:
    """The sign-in code *text* is, cleaned of what a copy drags along, or ""."""
    code = text.strip().strip("`'\"").strip()
    return code if _CODE_RE.match(code) else ""


def code_in(text: str) -> str:
    """A sign-in code anywhere in *text* ("code: …#…"), or ""."""
    m = _CODE_ANYWHERE_RE.search(text)
    return m.group(0) if m else ""


def mask(text: str) -> str:
    """*text* for a log line: a sign-in code is a credential while it lives."""
    return _CODE_ANYWHERE_RE.sub(lambda m: f"<login code, {len(m.group(0))} chars>",
                                 text)


class LoginError(Exception):
    pass


class AuthLogin:
    """One `claude auth login` at a time, from link to result."""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.master: int | None = None
        self.url = ""
        self.started = 0.0
        self._buf = ""
        self._changed = asyncio.Event()
        # Which login a card belongs to: an old card cannot cancel a new one.
        self.serial = 0
        # Start, enter and cancel never overlap: each holds the process it began.
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task | None = None

    @property
    def waiting(self) -> bool:
        """A link is out and the process waits for the code."""
        if self.proc is None or self.proc.returncode is not None:
            return False
        if time.monotonic() - self.started > _STALE:
            # Nobody used the link: the process and its pty go.
            if self._reaper is None or self._reaper.done():
                self._reaper = asyncio.get_running_loop().create_task(self.cancel())
            return False
        return bool(self.url)

    def _read(self) -> None:
        if self.master is None:
            return
        try:
            data = os.read(self.master, 65536)
        except OSError:
            data = b""
        if not data:
            asyncio.get_running_loop().remove_reader(self.master)
            self._changed.set()
            return
        self._buf += _ANSI_RE.sub("", data.decode("utf-8", errors="replace"))
        self._changed.set()

    async def _wait_for(self, pred, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not pred():
            left = deadline - time.monotonic()
            if left <= 0 or (self.proc is not None and self.proc.returncode is not None
                             and not self._changed.is_set()):
                return pred()
            self._changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), min(left, 1.0))
        return True

    async def start(self) -> str:
        """Start a login and return the sign-in link."""
        async with self._lock:
            return await self._start()

    async def _start(self) -> str:
        await self._cancel()
        exe = claude_bin() or "claude"
        master, slave = pty.openpty()
        try:
            # Wide enough that the CLI never wraps the sign-in link.
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 4000, 0, 0))
            self.proc = await asyncio.create_subprocess_exec(
                exe, "auth", "login", "--claudeai",
                stdin=slave, stdout=slave, stderr=slave,
                env={**os.environ, "TERM": "dumb", "NO_COLOR": "1"},
                start_new_session=True)
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self.serial += 1
        self.master, self._buf, self.url = master, "", ""
        self.started = time.monotonic()
        asyncio.get_running_loop().add_reader(master, self._read)
        if not await self._wait_for(lambda: bool(_URL_RE.search(self._buf)), _URL_WAIT):
            tail = self._buf.strip()[-300:]
            await self._cancel()
            raise LoginError(tail or "no sign-in link appeared")
        m = _URL_RE.search(self._buf)
        self.url = m.group(0) if m else ""
        log.info("auth login started, link of %d chars", len(self.url))
        return self.url

    async def enter(self, code: str) -> tuple[bool, str]:
        """Type the code in; (logged in, what the CLI said at the end)."""
        async with self._lock:
            proc, master = self.proc, self.master
            if proc is None or master is None or proc.returncode is not None:
                return False, "no login is waiting"
            secret = code.strip()
            mark = len(self._buf)
            os.write(master, secret.encode() + b"\r")
            try:
                await asyncio.wait_for(proc.wait(), _CODE_WAIT)
            except TimeoutError:
                said = self._buf[mark:].replace(secret, "<code>").strip()[-300:]
                await self._cancel()
                return False, said or "no answer to the code"
            await asyncio.sleep(0.2)
            said = self._buf[mark:].replace(secret, "<code>").strip()[-300:]
            ok = proc.returncode == 0
            log.info("auth login finished rc=%s", proc.returncode)
            if self.proc is proc:
                self._close_pty()
                self.proc, self.url = None, ""
            return ok, said

    def _close_pty(self) -> None:
        if self.master is not None:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(self.master)
            with contextlib.suppress(OSError):
                os.close(self.master)
            self.master = None

    async def cancel(self) -> None:
        async with self._lock:
            await self._cancel()

    async def _cancel(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.proc.wait(), 5)
        self._close_pty()
        self.proc, self.url = None, ""


async def status() -> dict:
    """`claude auth status --json`: who the machine is logged in as."""
    proc = await asyncio.create_subprocess_exec(
        claude_bin() or "claude", "auth", "status", "--json",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), 30)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return {}
    try:
        data = json.loads(out or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
