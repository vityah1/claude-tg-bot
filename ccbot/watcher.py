"""What the bot says without being asked, and the one thing it polls for.

The sessions' own output arrives as events of the session host and is turned
into messages by `bridge.py`; the pieces of that shared with the rest of the
bot live here: sending a block of Claude's output (`_emit`), the usage alerts
(`_check_usage`), the language for messages nobody asked for (`_locale`), and
the registration every outgoing message needs (`_say*`). The loop itself
only asks, now and then, whether a newer Claude Code is on disk.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import time
from typing import Protocol

from aiogram import Bot

from . import account, rich, updates
from . import sessions as sess
from .i18n import _, ngettext, resolve
from .i18n import use as use_locale
from .keyboards import update_notice_kb
from .settings import Settings
from .state import Store
from .util import gauges, split_text, usage_suffix

log = logging.getLogger("ccbot.watcher")

# Alert steps. Each fires once on the way up and re-arms if usage drops
# (a /clear, or a fresh rate-limit window).
_CTX_STEPS = (60, 75, 85, 95)
_LIMIT_STEPS = (80, 90, 95)

# How long the same watcher failure stays quiet after it has been reported.
_FAIL_REPEAT = 3600.0

# How often to ask what version of Claude Code is on disk. It only moves when
# the background updater has been at work, and asking spawns a node process.
_VERSION_CHECK = 600.0
_TICK = 60.0

# A session in one of these states runs no CLI: it wakes on the build on disk.
_RESTING = ("dormant", "offline", "closed")


class _Alerted(Protocol):
    """Whatever keeps a session's announced alert steps (`bridge._Rt`)."""

    alerted: dict[str, int]


class Watcher:
    def __init__(self, bot: Bot, store: Store, settings: Settings, chat_id: int):
        self.bot = bot
        self.store = store
        self.settings = settings
        self.chat_id = chat_id
        self._task: asyncio.Task | None = None
        # Last failure announced in the chat, so a fault that repeats every
        # tick does not become a message per tick.
        self.failure_sig: str | None = None
        self.failure_told = 0.0
        # Last time the disk was asked which Claude Code is installed.
        self.version_checked = 0.0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            # No update to read a language off, so the locale is opened here.
            # A task only inherits context set before it was created, and
            # /lang may change the answer while the loop is already running.
            with use_locale(self._locale()):
                try:
                    await self._check_version()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:           # never let the loop die
                    log.exception("watcher tick failed")
                    await self._report_failure(exc)
                else:
                    await self._clear_failure()
            await asyncio.sleep(_TICK)

    def _locale(self) -> str:
        """Language for messages nobody asked for.

        There is no Telegram user on this path: the bot speaks first. So it
        falls back to the language last seen on the profile — otherwise
        Claude's answers would arrive in English while every reply to a typed
        message came back in the user's own language.
        """
        return resolve(self.settings.language, self.settings.telegram_language)

    async def _report_failure(self, exc: Exception) -> None:
        """Say in the chat that the version check is broken, once an hour."""
        signature = f"{type(exc).__name__}: {exc}"
        now = time.time()
        if (signature == self.failure_sig
                and now - self.failure_told < _FAIL_REPEAT):
            return
        self.failure_sig = signature
        self.failure_told = now
        await self._say(
            _("⚠️ <b>The Claude Code version check has crashed</b>\n"
              "<code>{error}</code>\n\nThe sessions are not affected. "
              "Details in /log.").format(error=html.escape(signature[:300])),
            parse_mode="HTML",
        )

    async def _clear_failure(self) -> None:
        """Report that the loop is healthy again, once."""
        if not self.failure_sig:
            return
        self.failure_sig = None
        self.failure_told = 0.0
        await self._say(_("✅ The Claude Code version check works again."))

    async def _check_version(self) -> None:
        """Say once when a newer Claude Code is waiting on disk.

        A running session keeps the build it started with, and nothing tells
        it about the file that has meanwhile been replaced. Announced once per
        release — the note is kept in the settings file, so a bot restart does
        not turn one release into a second notification.
        """
        now = time.time()
        if now - self.version_checked < _VERSION_CHECK:
            return
        self.version_checked = now
        disk = await updates.installed()
        if not disk or self.settings.notified_version == disk:
            return
        behind = [m for m in self.store.all_managed()
                  if (sess.sdk_state is None or sess.sdk_state(m) not in _RESTING)
                  and updates.stale(m.session_id, disk)]
        if not behind:
            return
        self.settings.notified_version = disk
        log.info("announcing claude %s; %d session(s) behind", disk, len(behind))
        head = _("⬆️ <b>Claude Code {version} is on disk</b>").format(
            version=html.escape(disk))
        body = ngettext(
            "{count} session is still running an older build — a session only "
            "picks a new one up when it is started again.",
            "{count} sessions are still running an older build — a session "
            "only picks a new one up when it is started again.",
            len(behind)).format(count=len(behind))
        tail = _("A restart keeps the context: the same session id, the same "
                 "transcript, the same history. /update")
        await self._say_html(f"{head}\n\n{body}\n\n{tail}",
                             reply_markup=update_notice_kb())

    async def _emit(self, session_id: str, name: str, body: str, usage) -> None:
        """Send one block of Claude's output.

        Claude writes Markdown, and since Bot API 10.1 Telegram renders it —
        headings, tables and fenced code arrive as themselves rather than as
        the punctuation around them, and 32 768 characters usually mean no
        split at all. The chunked plain-text path stays underneath for when
        the API will not take the document.
        """
        tail = usage_suffix(usage)
        doc = f"**💬 {rich.md_escape(name)}**\n\n{body}{tail}"
        if len(doc) <= rich.DOC_LIMIT and await self._say_rich(
                session_id, markdown=doc):
            return
        chunks = split_text(f"💬 {name}\n\n{body}")
        for i, chunk in enumerate(chunks):
            # Usage tail rides along only on the last chunk, and only once
            # consumption is worth noticing.
            await self._say(chunk + (tail if i == len(chunks) - 1 else ""),
                            session_id=session_id)

    async def _check_usage(self, name: str, session_id: str,
                           rt: _Alerted, usage) -> None:
        """Notify when context or a rate limit crosses a step.

        A rate limit belongs to the account, not the session, and an alert
        about one is the moment to switch — so it names the account.
        """
        for key, pct, note in gauges(usage):
            steps = _CTX_STEPS if key == "ctx" else _LIMIT_STEPS
            reached = max([t for t in steps if pct >= t], default=0)
            previous = rt.alerted.get(key, 0)
            if reached > previous:
                log.info("usage alert %s=%d%% (step %d)", key, pct, reached)
                icon = "🔴" if reached >= 90 else ("🟠" if reached >= 75 else "🟡")
                who = ""
                if key != "ctx":
                    # The host knows the session's account; the transcript
                    # is the fallback.
                    email = getattr(rt, "account", "") or await account.email_for(
                        session_id, since=await sess.started_at(session_id))
                    who = f" · {email}" if email else ""
                await self._say(f"{icon} {name}: {note} — {pct}%{who}",
                                session_id=session_id)
            if reached != previous:
                rt.alerted[key] = reached

    async def _say(self, text: str, session_id: str | None = None, **kw) -> None:
        try:
            msg = await self.bot.send_message(self.chat_id, text, **kw)
        except Exception:
            log.exception("send_message failed")
            return
        log.info("out msg id=%s -> %s: %d chars",
                 getattr(msg, "message_id", "—"), (session_id or "—")[:8], len(text))
        if session_id and msg:
            self.store.remember_message(msg.message_id, session_id)

    async def _say_rich(self, session_id: str | None = None, *, blocks=None,
                        markdown: str | None = None, **kw):
        """Rich-message twin of _say — None means "fall back to plain text".

        A message the store never saw is a message a reply cannot address, so
        the bookkeeping is identical to _say's; only the wire format differs.
        """
        msg = await rich.send(self.bot, self.chat_id, blocks=blocks,
                              markdown=markdown, **kw)
        if msg is None:
            return None
        log.info("out rich id=%s -> %s", msg.message_id, (session_id or "—")[:8])
        if session_id:
            self.store.remember_message(msg.message_id, session_id)
        return msg

    async def _say_html(self, text: str, session_id: str | None = None, **kw):
        try:
            msg = await self.bot.send_message(
                self.chat_id, text, parse_mode="HTML", **kw
            )
        except Exception:
            log.exception("send_message failed")
            return None
        if session_id and msg:
            self.store.remember_message(msg.message_id, session_id)
        return msg
