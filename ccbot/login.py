"""Driving Claude Code's `/login` from the chat.

The login panel is read by `screen.login_screen`; this module types into it.
Every key goes through the window's input lock with the panel's step checked
first, because each one means something else once the panel has moved on:
a digit or a code pasted into the ordinary input line becomes a prompt, an
Esc interrupts a turn, an Enter submits whatever is typed.

No state lives here. Where the login stands is whatever the screen says, so
a bot restarted halfway through picks the flow up from the next frame.
"""

from __future__ import annotations

import asyncio
import re
import time

from . import screen as screenmod
from . import tmux

# What the sign-in page hands over: the code, "#", the state. Both halves are
# base64url, the state 43 characters of it (the code's length was never
# measured). Long and dotless, so "README.md#installation" is not one.
_CODE_TOKEN = r"[A-Za-z0-9_-]{20,}#[A-Za-z0-9_-]{20,}"
_CODE_RE = re.compile(rf"^{_CODE_TOKEN}$")
_CODE_ANYWHERE_RE = re.compile(rf"(?<![A-Za-z0-9_-]){_CODE_TOKEN}(?![A-Za-z0-9_-])")
# How long the panel gets to reach the next step after a key.
_STEP_WAIT = 8.0
_STEP_TICK = 0.3


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


def _state(raw: str) -> str:
    ls = screenmod.login_screen(raw)
    return ls.state if ls else ""


def _is(*states: str):
    return lambda raw: _state(raw) in states


def _not_ready(raw: str) -> bool:
    return screenmod.find_dialog(raw) is not None or not screenmod.is_ready(raw)


def _question_shown(raw: str) -> bool:
    # The paste of "/login" opens the command list over the bottom, which
    # is no reason to leave it unsent; a question that appeared meanwhile is.
    return screenmod.find_dialog(raw) is not None


def _not_awaiting_code(raw: str) -> bool:
    ls = screenmod.login_screen(raw)
    return ls is None or ls.state != "code" or ls.entered


def _not_code_step(raw: str) -> bool:
    return _state(raw) != "code"


async def wait_for(window_id: str, *states: str,
                   timeout: float = _STEP_WAIT) -> str:
    """Wait until the panel shows one of *states*; the state seen, or ""."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            state = _state(await tmux.capture(window_id))
        except tmux.TmuxError:
            return ""
        if state in states:
            return state
        if time.monotonic() >= deadline:
            return ""
        await asyncio.sleep(_STEP_TICK)


async def choose(window_id: str, digit: int) -> bool:
    """Pick a login method on the menu."""
    return await tmux.press_if(window_id, str(digit), _is("menu"))


async def start(window_id: str, digit: int = 1) -> bool:
    """Type `/login` into an idle session and pick method *digit*.

    False when the session would not take it or the menu never came.
    """
    try:
        await tmux.submit_text(window_id, "/login", refuse_if=_not_ready,
                               hold_if=_question_shown)
    except tmux.Refused:
        return False
    if not await wait_for(window_id, "menu"):
        return False
    return await choose(window_id, digit)


async def paste_code(window_id: str, code: str) -> None:
    """Type the sign-in code into the panel and submit it.

    Raises `tmux.Refused` when the panel is not waiting for a code — gone,
    moved on, or holding one already. The second test, before the Enter,
    is what keeps a code from becoming a prompt when the panel was closed
    in the terminal during the paste.
    """
    try:
        await tmux.submit_text(window_id, code, refuse_if=_not_awaiting_code,
                               hold_if=_not_code_step)
    except tmux.Refused:
        # Pasted, then the panel went: the code now sits in the ordinary
        # input line, where the next prompt would carry it to Claude.
        await tmux.press_if(window_id, "C-u",
                            lambda raw: screenmod.is_ready(raw) and code in raw)
        raise


async def retry(window_id: str, digit: int = 1) -> bool:
    """From an OAuth error back to a fresh link: Enter, then the method."""
    if not await tmux.press_if(window_id, "Enter", _is("error")):
        return False
    if not await wait_for(window_id, "menu"):
        return False
    return await choose(window_id, digit)


async def cancel(window_id: str) -> bool:
    """Esc, while the login panel still stands."""
    return await tmux.press_if(window_id, "Escape", screenmod.login_open)


async def proceed(window_id: str) -> bool:
    """The Enter the last step asks for ("Press Enter to continue")."""
    return await tmux.press_if(window_id, "Enter", _is("success", "warning"))
