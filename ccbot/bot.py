"""Telegram front-end: a dispatcher for Claude Code sessions living in tmux."""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import os
import shlex
import signal
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Message,
    Update,
)

from . import logsetup, media, rich, service, status_feed, tmux, updates, util
from . import screen as screenmod
from . import sessions as sess
from .config import Config
from .i18n import (
    DEFAULT_LOCALE,
    N_,
    LocaleMiddleware,
    _,
    i18n,
    install,
    language_name,
    ngettext,
    offered,
    resolve,
)
from .i18n import use as use_locale
from .keyboards import (
    STATUS_ICON,
    choice_kb,
    confirm_kb,
    dirs_kb,
    history_kb,
    lang_kb,
    project_dirs_kb,
    restart_confirm_kb,
    service_kb,
    session_kb,
    sessions_kb,
    update_kb,
)
from .settings import Settings
from .state import Store
from .util import as_pre, usage_report
from .watcher import Watcher

log = logging.getLogger("ccbot.bot")

# Commands the bot handles itself; anything else starting with "/" is forwarded
# to Claude, so /model, /compact, /cost and friends keep working.
OWN_COMMANDS = {"start", "help", "sessions", "new", "exit", "screen", "esc",
                "usage", "clear", "log", "dirs", "rename", "service", "restart",
                "lang", "update"}

# Telegram's Bot API refuses to serve files larger than this.
_MAX_ATTACHMENT = 20 * 1024 * 1024
# How long the inbox waits for the rest of a burst before sending it on.
# Telegram hands a whole forwarded batch over in a single poll — thirteen
# messages landed within 190 ms on 2026-08-27 — and an album is no slower, so
# a short window catches both while costing a lone message almost nothing.
_INBOX_QUIET = 0.8
# How often the collector looks at a batch it is holding.
_INBOX_TICK = 0.2
# Order Shift+Tab walks through permission modes.
_MODE_CYCLE = ("auto", "manual", "acceptEdits", "plan")
# How long to let Claude wind down after /exit before removing the window.
_EXIT_TIMEOUT = 8.0
# A restart waits longer than that: nothing is killed if it overruns, so there
# is no reason to be hasty, and a busy machine can take a while to unload.
_RESTART_EXIT_TIMEOUT = 20.0
# And how long the relaunched `claude` is given to appear in the window.
_RESTART_START_TIMEOUT = 40.0
# How long to keep reading the version after a restart before settling for
# "restarted" without a number. The payload lands on the first status render.
_RESTART_VERSION_WAIT = 25.0
# A half-finished prompt ("send me a path") expires rather than lingering.
_PENDING_TTL = 180.0


@dataclass
class _Restart:
    """What came of relaunching one session's `claude`."""
    ok: bool
    reason: str = ""     # why not, phrased for the chat
    version: str = ""    # the build it came back as, when it could be read


def restart_ask() -> str:
    return _(
        "🔄 <b>Restart the bot?</b>\n\n"
        "Claude's sessions are unaffected: they live in tmux, and the unit is "
        "set up to stop the bot alone (<code>KillMode=process</code>). Once it "
        "is back, the watcher picks the transcripts up where it left off.\n\n"
        "Only what is in the process's memory is lost: a half-finished request "
        "(\u00absend me a path\u00bb, \u00absend me a name\u00bb), "
        "attachments waiting for a caption, and the note that says \u00abthis "
        "question has already been shown\u00bb — an open dialog may arrive in "
        "the chat a second time.\n\n"
        "It usually takes 5–10 seconds."
    )


def no_supervisor() -> str:
    return _(
        "⚠️ The bot was started by hand rather than by systemd — once it exits "
        "nothing will bring it back, and there will be nothing left to drive "
        "the sessions from a phone.\n\n"
        "Put it under systemd:\n"
        "<code>sed \"s|%INSTALL_DIR%|$PWD|g\" contrib/claude-tg-bot.service \\\n"
        "  &gt; ~/.config/systemd/user/claude-tg-bot.service\n"
        "systemctl --user daemon-reload\n"
        "systemctl --user enable --now claude-tg-bot</code>\n\n"
        "After that <code>/restart</code> will work."
    )


def help_text() -> str:
    return _("""🤖 <b>Claude Code session dispatcher</b>

<b>Sessions</b>
/sessions — the session list, cards and controls
/new — start a session (pick a directory)

<b>Where things go</b>
Without a reply, everything — text and commands — goes to the
<b>active</b> session. Replying to a session's message goes to
<b>that</b> session and makes it active. The commands below
follow the same rule.

<b>Acting on a session</b>
/usage — how much of the 5-hour and 7-day quota is spent, and
   how full this session's context is. Read off the status line,
   so it is instant, costs no tokens and does not disturb the
   session. Not to be confused with «📉 Context breakdown» on the
   card: that one asks Claude itself <i>what</i> is filling the
   context — and that is a real turn in the session.
/screen — a text snapshot of the terminal
/esc — <b>interrupt what Claude is doing</b> (the Esc key).
   The session stays alive and its context is untouched.
/clear — clear the session's context. The session lives on,
   its history is gone.
/rename <i>name</i> — your own name instead of <code>project-1a2b</code>
   (<code>/rename -</code> gives the automatic one back). Until you
   name it, the list shows the topic Claude came up with itself.
/exit — <b>end the session</b>: Claude shuts down on its own, then
   the tmux window closes. The transcript stays — the session can
   be brought back from 🕘 in the list.

Any other <code>/command</code> (<code>/model</code>, <code>/compact</code>,
<code>/cost</code>…) is passed to Claude as it is.

<b>The bot itself</b>
/update — which Claude Code each session runs, which one is
   on disk, and a restart that keeps the context: Claude
   updates itself in the background, but a running session
   stays on the build it started with.
/service — uptime, code version, tmux state, restart button
/restart — restart the bot (Claude's sessions are unaffected)
/log — the last lines of the journal
/dirs — directories offered when starting a session
/lang — interface language

📎 <b>Any file</b> — a photo, a PDF, a JSON dump, a log, an
archive, a voice message — is saved and handed to Claude as a
path, under the name you sent it with. As an album, with a
caption, or with the text in the next message. Telegram lets a
bot download 20 MB at most; for anything bigger, send the path.

📨 Messages that arrive together become <b>one</b> prompt: a
forwarded conversation, an album, a thought written in three
goes. A forwarded message keeps the name of whoever wrote it.

When Claude asks a question, buttons with the options arrive.""")


# The order the states are explained in: from the ones that want something
# from you down to the ones that are merely broken.
_LEGEND_ORDER = ("waiting", "busy", "idle", "starting", "dead", "gone")


def _status_legend(views: list[sess.SessionView],
                   active: str | None) -> list[str]:
    """One line per icon actually present in the list, and nothing more.

    The active session wears ▶️ instead of its own status icon, so it is not
    what puts a state into the legend.
    """
    seen = {v.status for v in views if v.session_id != active}
    return [f"{STATUS_ICON[s]} {sess.status_label(s)}"
            for s in _LEGEND_ORDER if s in seen and s in STATUS_ICON]


def _sessions_text(managed: list[sess.SessionView],
                   foreign: list[sess.SessionView],
                   active: str | None, behind: int = 0) -> str:
    """The list's caption: a legend for exactly the icons on the screen.

    The two kinds of session are named apart — the bot's own tmux windows and
    the terminal's, which it can only watch — because a row of buttons alone
    cannot say which is which.
    """
    if managed:
        waiting = [v for v in managed if v.status == "waiting"]
        head = ngettext("📋 <b>Sessions</b> — {count} managed",
                        "📋 <b>Sessions</b> — {count} managed",
                        len(managed)).format(count=len(managed))
        if waiting:
            head += ngettext(", {count} waiting for you",
                             ", {count} waiting for you",
                             len(waiting)).format(count=len(waiting))
        block = [_("🖥 <b>In tmux</b> — started by the bot, driven from here")]
        cur = next((v for v in managed if v.session_id == active), None)
        if cur:
            block.append(_("▶️ active: <b>{name}</b> — plain text goes here"
                           ).format(name=html.escape(cur.name)))
        block += _status_legend(managed, active)
        if behind:
            block.append(ngettext(
                "⬆️ {count} session runs an older Claude Code — /update",
                "⬆️ {count} sessions run an older Claude Code — /update",
                behind).format(count=behind))
        parts = [head, "\n".join(block)]
    else:
        parts = [_("📋 <b>Sessions</b>\n\nThe bot manages none in tmux yet — "
                   "start one with {button}.").format(
                       button=_("➕ New session"))]
    if foreign:
        block = [_("🔗 <b>In your own terminal</b> — the bot only shows them; "
                   "«{button}» on the card takes one over.").format(
                       button=_("🔗 Move into tmux"))]
        # Only a session stuck on a prompt is worth calling out: that one
        # needs a hand at the desk, where the bot cannot reach.
        stuck = [v for v in foreign if v.status == "waiting"]
        if stuck:
            block.append(_("⏸ waiting for you at the keyboard: {names}"
                           ).format(names=", ".join(
                               html.escape(v.name) for v in stuck[:3])))
        parts.append("\n".join(block))
    # Named through the button's own string so the sentence and the keyboard
    # cannot drift apart in a translation.
    parts.append(_("<b>{button}</b> — sessions closed earlier; resuming "
                   "brings the context back.").format(button=_("🕘 Recent")))
    if managed:
        parts.append("<i>" + _(
            "Tap a session for details — whether to open it is decided there. "
            "Replying to a session's message writes to that session and makes "
            "it active; ✏️ on its card gives it a name of your own.") + "</i>")
    return "\n\n".join(parts)


def _stale_card(command: str) -> str:
    """Telegram forgets a card after 48 hours; say so instead of failing."""
    return _("This card is too old — send {command} again").format(command=command)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _editable(target: Message | CallbackQuery) -> Message | None:
    """The message a card can be edited in, or None when Telegram forbids it.

    A callback on a card older than 48 hours arrives with an
    InaccessibleMessage: it still names its chat, but it can no longer be
    edited or replied to. Touching it raises, and the whole handler dies.
    """
    if isinstance(target, Message):
        return target
    msg = target.message
    return msg if isinstance(msg, Message) else None


def _sender_name(m: Message) -> str:
    """Who wrote a forwarded message; empty for one the user wrote here.

    Telegram says this in ``forward_origin``, whose four shapes name the
    author in four different places (a visible user, a hidden one, a group, a
    channel). An empty string is the answer for a message that was not
    forwarded at all, and that is what tells a batch apart from a burst of
    the user's own typing.
    """
    origin = getattr(m, "forward_origin", None)
    if origin is None:
        return ""
    user = getattr(origin, "sender_user", None)
    if user is not None:
        return user.full_name or user.username or _("someone")
    hidden = getattr(origin, "sender_user_name", None)
    if hidden:
        return hidden
    chat = getattr(origin, "sender_chat", None) or getattr(origin, "chat", None)
    if chat is not None:
        return chat.title or chat.full_name or _("someone")
    return _("someone")


@dataclass(frozen=True)
class _Att:
    """One attachment as the downloader needs it, whatever Telegram called it."""

    file_id: str
    size: int
    mime: str
    name: str | None
    fallback: str


def _att(obj: object, mime: str, fallback: str) -> _Att:
    return _Att(
        file_id=str(getattr(obj, "file_id", "")),
        size=int(getattr(obj, "file_size", 0) or 0),
        mime=str(getattr(obj, "mime_type", None) or mime),
        name=getattr(obj, "file_name", None),
        fallback=fallback,
    )


def _attachment(m: Message) -> _Att | None:
    """The file a message carries, of any kind and of any format.

    The format is never judged: Claude reads a JSON, unpacks an archive and
    can transcribe a voice message himself, so a bot that only accepts
    pictures turns a forwarded conversation into a hole. Every kind Telegram
    has is taken — the name is what the sender called it, and where Telegram
    gives none (a photo, a voice note) the fallback suffix names the file.
    """
    # An animation and a video note also fill ``document``/``video`` on some
    # clients, so the specific kinds are asked about first.
    if m.photo:
        return _att(m.photo[-1], "image/jpeg", ".jpg")
    if m.animation:
        return _att(m.animation, "video/mp4", ".mp4")
    if m.video_note:
        return _att(m.video_note, "video/mp4", ".mp4")
    if m.voice:
        return _att(m.voice, "audio/ogg", ".oga")
    if m.sticker:
        return _att(m.sticker, "image/webp", ".webp")
    if m.document:
        return _att(m.document, "", ".bin")
    if m.video:
        return _att(m.video, "video/mp4", ".mp4")
    if m.audio:
        return _att(m.audio, "audio/mpeg", ".mp3")
    return None


@dataclass
class _Item:
    """One queued message: what it said, what it brought, who wrote it."""

    message: Message
    text: str = ""
    path: Path | None = None
    sender: str = ""


@dataclass
class _Inbox:
    """One chat's messages waiting to leave as a single prompt.

    Telegram delivers a forwarded batch in one poll and aiogram runs an update
    per task, so a forwarded conversation used to become one prompt per
    message, in whatever order the event loop scheduled them, each with its
    own Enter — and each racing the others for the same tmux buffer. They are
    gathered here instead and sorted by message_id, which is the only ordering
    that survives concurrent handlers.
    """

    items: dict[int, _Item] = field(default_factory=dict)
    last: float = 0.0
    # Attachments still coming down from Telegram. The batch may not be
    # flushed while any of them is in flight, or the file would arrive after
    # the prompt that mentions it.
    downloads: int = 0
    # Attachments this batch has seen, for naming the files apart.
    files: int = 0
    task: asyncio.Task | None = None
    # Whether the person has already been told the files are waiting.
    announced: bool = False


class CCBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.bot = Bot(token=cfg.token)
        self.dp = Dispatcher()
        self.store = Store()
        self.settings = Settings()
        install()
        self.watcher = Watcher(self.bot, self.store, self.settings,
                               cfg.owner, cfg.poll_interval)
        # chat_id -> ("dir",) | ("dialog", session_id, option_number)
        #          | ("rename", session_id) | ("adddir",)
        self.pending: dict[int, tuple] = {}
        # Incoming messages wait here for the rest of their burst, so a
        # forwarded conversation, an album and the sentence that explains it
        # reach Claude as one prompt.
        self.inbox: dict[int, _Inbox] = {}
        self.dir_choices: list[str] = []
        self.dp.update.outer_middleware(logsetup.UpdateLogMiddleware(cfg.allowed))
        # Per-update language, read from the settings first and from the
        # sender's Telegram profile second.
        LocaleMiddleware(self.settings).setup(self.dp)
        self._register()

    # ------------------------------------------------------------------ infra
    def _register(self) -> None:
        dp, F_ = self.dp, F

        @dp.message(CommandStart())
        async def _start(m: Message):
            if not self._ok(m):
                return
            await m.answer(help_text(), parse_mode="HTML")
            await self._show_sessions(m)

        @dp.message(Command("help"))
        async def _help(m: Message):
            if self._ok(m):
                await m.answer(help_text(), parse_mode="HTML")

        @dp.message(Command("sessions"))
        async def _sessions(m: Message):
            if self._ok(m):
                await self._show_sessions(m)

        @dp.message(Command("new"))
        async def _new(m: Message):
            if self._ok(m):
                await self._ask_dir(m)

        @dp.message(Command("screen"))
        async def _screen(m: Message):
            if not self._ok(m):
                return
            mgd, _note = await self._target(m)
            if mgd:
                await self.watcher.send_screen(mgd.window_id, mgd.full_label,
                                               mgd.session_id)

        @dp.message(Command("esc"))
        async def _esc(m: Message):
            if not self._ok(m):
                return
            mgd, note = await self._target(m)
            if mgd:
                self._typed(mgd)
                await tmux.send_keys(mgd.window_id, "Escape")
                await self._ack(m, _("⏸ Esc sent to <b>{name}</b>{note}").format(
                    name=html.escape(mgd.full_label), note=note), mgd)

        @dp.message(Command("rename"))
        async def _rename(m: Message):
            if not self._ok(m):
                return
            mgd, note = await self._target(m)
            if not mgd:
                return
            arg = (m.text or "").split(maxsplit=1)
            await self._rename_session(m, mgd, arg[1] if len(arg) > 1 else "", note)

        @dp.message(Command("dirs"))
        async def _dirs(m: Message):
            if self._ok(m):
                await self._show_dirs(m)

        @dp.message(Command("lang"))
        async def _lang(m: Message):
            if self._ok(m):
                await self._show_langs(m)

        @dp.message(Command("log"))
        async def _log(m: Message):
            if not self._ok(m):
                return
            body = logsetup.tail(50)
            # Log lines are long by nature; a rich block scrolls where <pre>
            # used to fold every one of them into three.
            if not await rich.send(self.bot, m.chat.id,
                                   blocks=[rich.pre(rich.ascii_frame(body))]):
                await m.answer(as_pre(body), parse_mode="HTML")

        @dp.message(Command("service"))
        async def _service(m: Message):
            if not self._ok(m):
                return
            await m.answer(await self._service_text(), parse_mode="HTML",
                           reply_markup=service_kb(service.under_systemd()))

        @dp.message(Command("restart"))
        async def _restart(m: Message):
            if not self._ok(m):
                return
            if not service.under_systemd():
                await m.answer(no_supervisor(), parse_mode="HTML")
                return
            await m.answer(restart_ask(), parse_mode="HTML",
                           reply_markup=restart_confirm_kb())

        @dp.message(Command("update"))
        async def _update(m: Message):
            if self._ok(m):
                await self._show_update(m)

        @dp.message(Command("clear"))
        async def _clear(m: Message):
            # In the menu for discoverability, but the work is Claude's.
            if not self._ok(m):
                return
            mgd, note = await self._target(m)
            if mgd:
                await self._send_prompt(mgd, "/clear")
                await self._ack(m, _("🧹 /clear sent to <b>{name}</b>{note}").format(
                    name=html.escape(mgd.full_label), note=note), mgd)

        @dp.message(Command("usage"))
        async def _usage(m: Message):
            if not self._ok(m):
                return
            mgd, _note = await self._target(m)
            if mgd:
                await self._ack(
                    m,
                    usage_report(mgd.full_label, status_feed.read(mgd.session_id),
                                 await self._status(mgd)),
                    mgd,
                )

        @dp.message(Command("exit"))
        async def _exit(m: Message):
            if not self._ok(m):
                return
            mgd, _note = await self._target(m)
            if mgd:
                note = await m.answer(
                    _("⏳ Ending {name} — sending /exit…").format(name=mgd.full_label))
                await self._close(mgd.session_id)
                await note.edit_text(
                    _("❌ Session <b>{name}</b> has ended.\n"
                      "The transcript stays — it can be brought back from 🕘 in "
                      "the list.").format(name=html.escape(mgd.full_label)),
                    parse_mode="HTML",
                )
                await self._show_sessions(m)

        # Every kind of attachment, not a chosen few: an unhandled kind falls
        # through to no handler at all, and a voice message inside a forwarded
        # conversation would silently disappear from the batch.
        @dp.message(F_.photo | F_.document | F_.video | F_.audio | F_.voice
                    | F_.video_note | F_.animation | F_.sticker)
        async def _media(m: Message):
            if self._ok(m):
                await self._on_media(m)

        @dp.message(F_.text)
        async def _text(m: Message):
            if self._ok(m):
                await self._on_text(m)

        @dp.errors()
        async def _on_error(event: ErrorEvent) -> bool:
            """Last line of defence: no failure may end in silence.

            A handler that raises leaves the person staring at a message that
            was apparently ignored — the worst possible feedback, because it
            looks exactly like the bot being asleep. Say what broke, where to
            read more, and swallow the event so polling carries on.
            """
            log.exception("handler failed: %r", event.exception,
                          exc_info=event.exception)
            chat = self._chat_of(event.update)
            if chat is None:
                return True
            detail = f"{type(event.exception).__name__}: {event.exception}"
            try:
                # The error observer runs after the locale middleware has
                # already unwound, so the language is chosen again here.
                with use_locale(self._locale(self._user_of(event.update))):
                    text = _("⚠️ <b>Could not handle this message</b>\n"
                             "<code>{error}</code>\n\n"
                             "Your sessions are unaffected. Details in /log."
                             ).format(error=html.escape(detail[:300]))
                await self.bot.send_message(chat, text, parse_mode="HTML")
            except Exception:
                log.exception("could not report the failure to chat=%s", chat)
            return True

        @dp.callback_query()
        async def _cb(c: CallbackQuery):
            if c.from_user.id not in self.cfg.allowed:
                await c.answer(_("Not allowed"), show_alert=True)
                return
            log.info("callback chat=%s data=%s",
                     c.message.chat.id if c.message else "—", c.data)
            try:
                await self._on_callback(c)
            except Exception as exc:
                log.exception("callback failed: data=%s", c.data)
                await c.answer(
                    _("Error: {error} — see /log").format(error=type(exc).__name__),
                    show_alert=True)

    @staticmethod
    async def _safe_edit(message: Message, text: str, **kw) -> None:
        """Edit a message, tolerating an unchanged body.

        Telegram treats "same content" as an error; pressing 🔄 when nothing
        moved is normal, not a failure the user should see.
        """
        try:
            await message.edit_text(text, **kw)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                raise
            log.debug("edit skipped: content unchanged")

    def _locale(self, user=None) -> str:
        """Language to speak on a path no middleware wrapped."""
        code = getattr(user, "language_code", None) or self.settings.telegram_language
        return resolve(self.settings.language, code)

    @staticmethod
    def _user_of(update: Update | None):
        """Whoever sent the update, for guessing a language from their client."""
        if update is None:
            return None
        for event in (update.message, update.edited_message,
                      update.channel_post, update.callback_query):
            if event is not None:
                return event.from_user
        return None

    @staticmethod
    def _chat_of(update: Update | None) -> int | None:
        """Where to answer for any update shape the bot can receive."""
        if update is None:
            return None
        for event in (update.message, update.edited_message,
                      update.channel_post):
            if event is not None:
                return event.chat.id
        if update.callback_query and update.callback_query.message:
            return update.callback_query.message.chat.id
        return None

    def _ok(self, m: Message) -> bool:
        ok = bool(m.from_user and m.from_user.id in self.cfg.allowed)
        if not ok and m.from_user:
            log.warning("rejected user id=%s username=%s",
                        m.from_user.id, m.from_user.username)
        return ok

    # --------------------------------------------------------------- helpers
    @property
    def roots(self) -> tuple[str, ...]:
        """Directory shortlist: .env wins, then the bot's own settings.

        Empty means no shortlist at all — directories are then discovered from
        the user's Claude history, which is what a fresh install should do.
        """
        return self.cfg.dirs or tuple(self.settings.dirs)

    def _resolve(self, short: str):
        for mgd in self.store.all_managed():
            if mgd.session_id.startswith(short):
                return mgd
        return None

    async def _active(self, chat_id: int):
        sid = self.store.get_active(chat_id)
        mgd = self.store.get(sid) if sid else None
        if not mgd:
            await self.bot.send_message(
                chat_id, _("No active session — /sessions"))
            return None
        return mgd

    async def _target(self, m: Message) -> tuple:
        """Which session a message is aimed at, and a note on how it got there.

        Replying names a session explicitly, so it beats whichever one is
        active — for commands just as much as for text, since answering a
        message from one session and having /clear hit another is the worst
        possible surprise. The reply also makes that session active: otherwise
        the next message, sent without a reply, would quietly go elsewhere.
        """
        replied = getattr(m, "reply_to_message", None)
        sid = self.store.session_of_message(replied.message_id) if replied else None
        if not sid:
            return await self._active(m.chat.id), ""
        mgd = self.store.get(sid)
        if not mgd:
            await m.answer(_("That session has already ended — see /sessions"))
            return None, ""
        if self.store.get_active(m.chat.id) == sid:
            return mgd, " · " + _("in reply")
        self.store.set_active(m.chat.id, sid)
        log.info("active switched by reply: chat=%s -> %s (%s)",
                 m.chat.id, mgd.full_label, sid[:8])
        return mgd, " · " + _("in reply, now active")

    async def _ack(self, m: Message, text: str, mgd) -> None:
        """Confirm an action, and let a reply to that confirmation land back.

        The last message in the chat is the one a phone replies to fastest, so
        the bot's own acknowledgements route like the session's output does.
        """
        try:
            msg = await m.answer(text, parse_mode="HTML")
        except TelegramBadRequest:
            log.exception("ack failed: %r", text[:120])
            return
        log.info("out msg id=%s -> %s: %r",
                 getattr(msg, "message_id", "—"), mgd.session_id[:8], text[:120])
        if msg:
            self.store.remember_message(msg.message_id, mgd.session_id)

    async def _service_text(self) -> str:
        """State of the bot's own process — the one thing /sessions cannot show."""
        started = datetime.fromtimestamp(service.STARTED)
        managed = self.store.all_managed()
        lines = [
            _("🤖 <b>Bot service</b>"), "",
            _("⏱ Up for {duration} (started at {time})").format(
                duration=service.human_delta(service.uptime()),
                time=f"{started:%H:%M}"),
            _("🧩 Code: <code>{version}</code>").format(
                version=html.escape(service.version())),
        ]
        disk = await updates.installed()
        if disk:
            behind = sum(1 for m in managed
                         if updates.stale(m.session_id, disk))
            line = _("🧰 Claude Code on disk: <code>{version}</code>").format(
                version=html.escape(disk))
            if behind:
                line += " · " + ngettext(
                    "{count} session is still on an older one — /update",
                    "{count} sessions are still on an older one — /update",
                    behind).format(count=behind)
            lines.append(line)
        if service.under_systemd():
            lines.append(
                _("⚙️ PID {pid} · systemd <code>{unit}</code> — a crash brings "
                  "it back in 5 s").format(pid=os.getpid(), unit=service.UNIT))
        else:
            lines.append(
                _("⚙️ PID {pid} · started by hand — a crash will not bring it "
                  "back").format(pid=os.getpid()))
        pid = await tmux.server_pid()
        if pid:
            lines.append(_("🖥 tmux server: PID {pid} — sessions survive a bot "
                           "restart").format(pid=pid))
        else:
            lines.append(_("🖥 the tmux server is not answering — there are no "
                           "sessions right now"))
        lines.append(_("📡 Polling every {seconds} s · managed sessions: {count}")
                     .format(seconds=self.cfg.poll_interval, count=len(managed)))
        lines.append(_("📓 Journal: <code>{path}</code> — /log").format(
            path=logsetup.LOG_PATH))
        return "\n".join(lines)

    async def _do_restart(self, chat_id: int) -> None:
        """Exit so systemd starts a fresh process a few seconds later."""
        service.note_restart(chat_id)
        log.info("restart requested from chat=%s", chat_id)
        try:
            await self.bot.send_message(
                chat_id, _("⏳ Restarting — back in a few seconds…"))
        except Exception:
            log.exception("could not announce the restart")
        # Stopping the poller unwinds run(), which closes the session cleanly;
        # systemd's Restart=always does the rest.
        await self.dp.stop_polling()

    async def _rename_session(self, m: Message, mgd, raw: str, note: str = "") -> None:
        """Give a session a name a human can recognise in a list."""
        auto = util.default_name(mgd.cwd, mgd.session_id)
        wanted = raw.strip()
        # "-" is the documented way back to the automatic name; the word is
        # accepted too, in whatever language the interface is speaking.
        reset_words = {"-", "auto", _("auto")}
        name = auto if wanted.lower() in reset_words else util.clean_name(wanted)
        if not name:
            await m.answer(
                _("✏️ Send the name together with the command:\n"
                  "<code>/rename Billing audit</code>\n\n"
                  "Replying to a session's message renames that session, "
                  "otherwise it is the active one. <code>/rename -</code> gives "
                  "the automatic name back."),
                parse_mode="HTML",
            )
            return
        # Only the display name changes: `name` stays the one Claude was
        # launched with, which is how the session is found again after /clear.
        self.store.set_custom_name(mgd.session_id, None if name == auto else name)
        # Keep the tmux window in step, so `tmux attach` shows the same name.
        await tmux.rename_window(mgd.window_id, util.window_name(name))
        log.info("session renamed id=%s %r -> %r",
                 mgd.session_id[:8], mgd.full_label, name)
        fresh = self.store.get(mgd.session_id) or mgd
        tail = ("\n<i>" + _("your own name is gone — I will show Claude's "
                              "own topic instead") + "</i>"
                if name == auto else "")
        await self._ack(
            m,
            _("✏️ It is now <b>{name}</b>{note}").format(
                name=html.escape(fresh.full_label), note=note) + tail,
            mgd,
        )

    async def _show_sessions(self, target: Message | CallbackQuery) -> None:
        managed = await sess.managed_views(self.store)
        foreign = await sess.foreign_views(self.store)
        here = _editable(target)
        if here is None:
            await target.answer(_stale_card("/sessions"), show_alert=True)
            return
        active = self.store.get_active(here.chat.id)
        disk = await updates.installed()
        behind = sum(1 for v in managed if updates.stale(v.session_id, disk))
        text = _sessions_text(managed, foreign, active, behind)
        kb = sessions_kb(managed, foreign, active)
        if isinstance(target, CallbackQuery):
            await self._safe_edit(here, text, reply_markup=kb, parse_mode="HTML")
        else:
            await here.answer(text, reply_markup=kb, parse_mode="HTML")

    async def _show_dirs(self, target: Message | CallbackQuery) -> None:
        dirs = list(self.roots)
        fixed = bool(self.cfg.dirs)
        if dirs:
            # The button is named through a placeholder so the sentence and the
            # keyboard can never drift apart in a translation.
            head = _("📁 <b>Project directories</b>\n\nThese are the "
                     "directories offered under {button}.").format(
                         button=_("➕ New session"))
            if fixed:
                head += "\n\n" + _(
                    "⚠️ The list comes from <code>CCBOT_DIRS</code> in .env — "
                    "the buttons below will not change it.")
        else:
            head = _("📁 <b>Project directories</b>\n\nThe list is empty — "
                     "directories are discovered from your own Claude history.\n"
                     "Add some if you would rather see only certain projects.")
        kb = project_dirs_kb(dirs, not dirs)
        here = _editable(target)
        if here is None:
            await target.answer(_stale_card("/dirs"), show_alert=True)
        elif isinstance(target, CallbackQuery):
            await self._safe_edit(here, head, reply_markup=kb, parse_mode="HTML")
        else:
            await here.answer(head, reply_markup=kb, parse_mode="HTML")

    async def _ask_dir(self, target: Message | CallbackQuery) -> None:
        self.dir_choices = await sess.recent_dirs(self.store, 10, roots=self.roots)
        text = _("📁 Which directory should the session start in?")
        kb = dirs_kb(self.dir_choices)
        here = _editable(target)
        if here is None:
            await target.answer(_stale_card("/new"), show_alert=True)
        elif isinstance(target, CallbackQuery):
            await self._safe_edit(here, text, reply_markup=kb)
        else:
            await here.answer(text, reply_markup=kb)

    # ---------------------------------------------------------- session ops
    async def _create(self, chat_id: int, cwd: str, resume: str | None = None) -> None:
        cwd = str(Path(cwd).expanduser())
        if not Path(cwd).is_dir():
            log.warning("create rejected: no such directory %r", cwd)
            await self.bot.send_message(
                chat_id,
                _("❌ No such directory:\n<code>{path}</code>\n\n"
                  "If that was an ordinary message — I was waiting for a path "
                  "after {button}. Press /sessions and try again.").format(
                      path=html.escape(cwd[:200]), button=_("✏️ Another path")),
                parse_mode="HTML",
            )
            return
        session_id = resume or str(uuid.uuid4())
        name = util.default_name(cwd, session_id)
        wid = await tmux.create_window(name, cwd)
        flag = f"--resume {session_id}" if resume else f"--session-id {session_id}"
        await tmux.run_in_window(wid, f"claude {flag} -n {name}")
        log.info("session created id=%s name=%s cwd=%s window=%s resume=%s",
                 session_id[:8], name, cwd, wid, bool(resume))
        self.store.add(session_id, wid, cwd, name)
        self.store.set_active(chat_id, session_id)
        # Skip transcript written before now, or a resume would replay history.
        self.watcher.adopt(session_id, skip_existing=True)
        head = (_("✅ Resumed: <b>{name}</b>") if resume
                else _("✅ Created: <b>{name}</b>")).format(name=name)
        await self.bot.send_message(
            chat_id,
            f"{head}\n<code>{cwd}</code>\n\n"
            + _("Locally: <code>{command}</code>").format(
                command=tmux.attach_hint(wid)),
            parse_mode="HTML",
        )

    async def _close(self, session_id: str, graceful: bool = True) -> None:
        """End a session and drop its window.

        `/exit` lets Claude shut down on its own terms (Ctrl+D is ignored by the
        TUI, so it is not an option); only then is the window removed. Killing
        the window outright would cut the process off mid-write.
        """
        mgd = self.store.get(session_id)
        if not mgd:
            return
        log.info("session closing id=%s name=%s graceful=%s",
                 session_id[:8], mgd.full_label, graceful)
        if graceful and await tmux.window_exists(mgd.window_id):
            try:
                await tmux.send_keys(mgd.window_id, "Escape")
                await asyncio.sleep(0.3)
                await self._send_prompt(mgd, "/exit")
                for _tick in range(int(_EXIT_TIMEOUT / 0.5)):
                    await asyncio.sleep(0.5)
                    if not await tmux.claude_running(mgd.window_id):
                        break
            except tmux.TmuxError:
                pass
        await tmux.kill_window(mgd.window_id)
        self.store.remove(session_id)
        self.watcher.forget(session_id)

    # --------------------------------------------------- claude code updates
    async def _await_claude(self, window_id: str, want: bool,
                            timeout: float) -> bool:
        """Wait for `claude` to be gone from a window, or to be back in it."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await tmux.claude_running(window_id) is want:
                return True
            await asyncio.sleep(0.5)
        return await tmux.claude_running(window_id) is want

    @staticmethod
    async def _await_version(session_id: str, want: str) -> str:
        """Read the restarted session's version, once it says what it is."""
        deadline = time.monotonic() + _RESTART_VERSION_WAIT
        seen = ""
        while time.monotonic() < deadline:
            seen = updates.running(session_id)
            if seen == want:
                return seen
            await asyncio.sleep(1.0)
        return seen

    async def _restart_session(self, mgd) -> _Restart:
        """Relaunch a session's `claude` in place, context and all.

        `/exit`, then `claude --resume <id> -n <name>` in the same window. The
        session id survives a resume, so the transcript keeps growing in the
        same file and the reader's offset stays valid — nothing is replayed
        into the chat. The launch name has to come back exactly as it was: it
        is the only thread back to a session that is later cleared.

        Unlike `_close`, this never kills the window. A restart that goes
        wrong must leave the session where it stood, not take it down.
        """
        if not await tmux.window_exists(mgd.window_id):
            return _Restart(False, _("its tmux window is gone"))
        # force: the cached answer is up to five seconds old, and a session
        # that has just finished would be refused on the strength of a reading
        # taken while it was still working.
        agents = await sess.live_agents(force=True)
        status = next((a.get("status", "") for a in agents
                       if a.get("sessionId") == mgd.session_id), "")
        if status == "busy":
            return _Restart(False, _("it is working right now"))
        if status == "waiting":
            return _Restart(False, _("it is waiting for an answer"))
        # The agent list is not enough on its own: a session that has just been
        # relaunched is missing from it for a few seconds, and a second restart
        # would then walk straight over whatever it had been asked meanwhile.
        # The screen answers immediately and needs nothing to be registered.
        try:
            raw = await tmux.capture(mgd.window_id)
        except tmux.TmuxError:
            raw = ""
        if screenmod.is_busy(raw):
            return _Restart(False, _("it is working right now"))
        if screenmod.find_dialog(raw) is not None:
            return _Restart(False, _("it is waiting for an answer"))

        log.info("restarting session id=%s name=%s window=%s status=%s",
                 mgd.session_id[:8], mgd.name, mgd.window_id, status or "-")
        updates.note_restarted(mgd.session_id)
        self._typed(mgd)
        if await tmux.claude_running(mgd.window_id):
            await tmux.send_keys(mgd.window_id, "Escape")
            await asyncio.sleep(0.3)
            await self._send_prompt(mgd, "/exit")
            if not await self._await_claude(mgd.window_id, False,
                                            _RESTART_EXIT_TIMEOUT):
                updates.forget(mgd.session_id)
                log.warning("restart aborted: %s did not exit", mgd.session_id[:8])
                return _Restart(False, _("it did not shut down within {n} s")
                                .format(n=int(_RESTART_EXIT_TIMEOUT)))
        await tmux.run_in_window(
            mgd.window_id,
            f"claude --resume {shlex.quote(mgd.session_id)} "
            f"-n {shlex.quote(mgd.name)}",
        )
        if not await self._await_claude(mgd.window_id, True,
                                        _RESTART_START_TIMEOUT):
            log.error("restart failed: %s did not come back", mgd.session_id[:8])
            return _Restart(False, _("it did not come back within {n} s")
                            .format(n=int(_RESTART_START_TIMEOUT)))
        self._typed(mgd)
        version = await self._await_version(mgd.session_id,
                                            await updates.installed())
        if version:
            updates.settled(mgd.session_id)
        log.info("session restarted id=%s version=%s",
                 mgd.session_id[:8], version or "unknown")
        return _Restart(True, version=version)

    async def _outdated(self) -> tuple[str, list[tuple]]:
        """The disk version, and (session, status, its version) for each one."""
        disk = await updates.installed()
        views = {v.session_id: v for v in await sess.managed_views(self.store)}
        rows = []
        for m in self.store.all_managed():
            view = views.get(m.session_id)
            rows.append((m, view.status if view else "",
                         updates.stale(m.session_id, disk)))
        return disk, rows

    async def _update_text(self, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
        """The version card: what is on disk, what each session runs."""
        disk, rows = await self._outdated()
        behind = [(m, st) for m, st, old in rows if old]
        idle = [m for m, st in behind if st in ("idle", "starting")]
        active = await self._active(chat_id)
        # "This session" means the active one and nothing else: offering the
        # button for whichever session happened to be first in the list would
        # restart something the caption never named.
        one = next((m for m in idle
                    if active and m.session_id == active.session_id), None)
        lines = [_("⬆️ <b>Claude Code</b>"), ""]
        if disk:
            lines.append(_("On disk: <code>{version}</code> — what a session "
                           "gets the moment it is started again.").format(
                               version=html.escape(disk)))
        else:
            lines.append(_("The version on disk could not be read — is the "
                           "<code>claude</code> CLI on the bot's PATH?"))
        if rows:
            lines.append("")
            for m, st, old in rows:
                icon = STATUS_ICON.get(st, "❔")
                here = "▶️" if active and m.session_id == active.session_id else ""
                mine = old or updates.running(m.session_id)
                mark = " ⬆️" if old else ""
                lines.append(f"{here}{icon} <b>{html.escape(m.full_label)}</b> — "
                             f"<code>{html.escape(mine or '?')}</code>{mark}")
        if not behind:
            lines += ["", _("✅ Every session is already running the version "
                            "that is on disk.")]
        else:
            skipped = [m for m, st in behind if st not in ("idle", "starting")]
            lines += ["", ngettext("{count} session is behind.",
                                   "{count} sessions are behind.",
                                   len(behind)).format(count=len(behind))]
            if skipped:
                lines.append(_("Busy or waiting, so left alone: {names}").format(
                    names=", ".join(html.escape(m.full_label) for m in skipped)))
            lines += ["", _(
                "A restart is <code>/exit</code> followed by "
                "<code>claude --resume</code> in the same window: the session "
                "id, the transcript and the context all survive. What does not: "
                "anything half-typed into its input line, and the prompt cache — "
                "so the first reply after a restart takes a little longer.")]
        return "\n".join(lines), update_kb(
            one.session_id if one else None, len(idle))

    async def _show_update(self, target: Message | CallbackQuery) -> None:
        here = _editable(target)
        if here is None:
            await target.answer(_stale_card("/update"), show_alert=True)
            return
        text, kb = await self._update_text(here.chat.id)
        if isinstance(target, CallbackQuery):
            await self._safe_edit(here, text, reply_markup=kb, parse_mode="HTML")
        else:
            await here.answer(text, reply_markup=kb, parse_mode="HTML")

    async def _restart_one(self, chat_id: int, mgd) -> None:
        """Restart a single session, saying so before, during and after."""
        note = await self.bot.send_message(
            chat_id,
            _("⏳ Restarting <b>{name}</b> — /exit, then resume…").format(
                name=html.escape(mgd.full_label)),
            parse_mode="HTML")
        res = await self._restart_session(mgd)
        if res.ok and res.version:
            text = _("✅ <b>{name}</b> now runs <code>{version}</code> — the "
                     "context came back with it.").format(
                         name=html.escape(mgd.full_label),
                         version=html.escape(res.version))
        elif res.ok:
            text = _("✅ <b>{name}</b> has been restarted; it has not said "
                     "which version it is yet.").format(
                         name=html.escape(mgd.full_label))
        else:
            text = _("⚠️ <b>{name}</b> was not restarted — {reason}. Nothing "
                     "was changed.").format(name=html.escape(mgd.full_label),
                                            reason=res.reason)
        with contextlib.suppress(Exception):
            await note.edit_text(text, parse_mode="HTML")

    async def _restart_all(self, chat_id: int) -> None:
        """Restart every idle session that is behind, one after another.

        In sequence rather than at once: each relaunch reads a whole
        transcript back, and several doing that together is how a laptop
        starts swapping.
        """
        _disk, rows = await self._outdated()
        queue = [m for m, st, old in rows if old and st in ("idle", "starting")]
        if not queue:
            await self.bot.send_message(chat_id, _(
                "Nothing to restart: no idle session is behind."))
            return
        note = await self.bot.send_message(
            chat_id, ngettext("⏳ Restarting {count} session…",
                              "⏳ Restarting {count} sessions…",
                              len(queue)).format(count=len(queue)))
        done, failed = [], []
        for i, mgd in enumerate(queue, 1):
            with contextlib.suppress(Exception):
                await note.edit_text(_("⏳ {index}/{total}: <b>{name}</b>…")
                                     .format(index=i, total=len(queue),
                                             name=html.escape(mgd.full_label)),
                                     parse_mode="HTML")
            res = await self._restart_session(mgd)
            if res.ok:
                done.append((mgd, res.version))
            else:
                failed.append((mgd, res.reason))
        lines = []
        if done:
            lines.append(ngettext("✅ Restarted {count} session:",
                                  "✅ Restarted {count} sessions:",
                                  len(done)).format(count=len(done)))
            lines += [f"• <b>{html.escape(m.full_label)}</b> — "
                      f"<code>{html.escape(v or '?')}</code>" for m, v in done]
        if failed:
            lines.append(_("⚠️ Left as they were:"))
            lines += [f"• <b>{html.escape(m.full_label)}</b> — {r}"
                      for m, r in failed]
        with contextlib.suppress(Exception):
            await note.edit_text("\n".join(lines), parse_mode="HTML")

    async def _check_updates(self, chat_id: int) -> None:
        """Ask the CLI to fetch a new release, then say what changed."""
        before = await updates.installed()
        note = await self.bot.send_message(
            chat_id, _("🔄 Running <code>claude update</code> — this can take "
                       "a minute…"), parse_mode="HTML")
        ok, output = await updates.update_cli()
        after = await updates.installed(force=True)
        if not ok:
            text = _("⚠️ <code>claude update</code> failed:\n<code>{output}"
                     "</code>").format(output=html.escape(output[:500] or "—"))
        elif after and before and after != before:
            text = _("⬆️ Updated: <code>{before}</code> → <code>{after}</code>. "
                     "Restart the sessions to put it to work — /update.").format(
                         before=html.escape(before), after=html.escape(after))
        else:
            text = _("✅ Already on the latest release: <code>{version}</code>."
                     ).format(version=html.escape(after or before or "?"))
        with contextlib.suppress(Exception):
            await note.edit_text(text, parse_mode="HTML")

    def _typed(self, mgd) -> None:
        """Tell the watcher the bot has just typed into this session.

        Input takes effect on the screen at once and in `claude agents --json`
        seconds later. In between, a session that has just been answered looks
        like one waiting on an unparsed question — see Watcher.note_input.
        """
        self.watcher.note_input(mgd.session_id)

    async def _send_prompt(self, mgd, text: str) -> None:
        self._typed(mgd)
        log.info("prompt -> %s (%s): %r", mgd.full_label, mgd.session_id[:8], text[:120])
        await tmux.submit_text(mgd.window_id, text)

    # ----------------------------------------------------------------- inbox
    def _inbox_of(self, chat_id: int) -> _Inbox:
        box = self.inbox.get(chat_id)
        if box is None:
            box = self.inbox[chat_id] = _Inbox()
        return box

    def _hold(self, m: Message, text: str = "", download: bool = False) -> _Item:
        """Queue one incoming message and keep the collection window open.

        An attachment is registered here *before* it is downloaded: the batch
        must not leave while a file is still on its way, and the message keeps
        its place in the order either way.
        """
        box = self._inbox_of(m.chat.id)
        item = _Item(message=m, text=text, sender=_sender_name(m))
        box.items[m.message_id] = item
        box.last = time.monotonic()
        box.announced = False
        if download:
            box.downloads += 1
            box.files += 1
        if box.task is None or box.task.done():
            box.task = asyncio.create_task(self._inbox_loop(m.chat.id))
        return item

    async def _inbox_loop(self, chat_id: int) -> None:
        """Hold the batch until the burst is over, then send it as one prompt.

        This runs outside a handler, where @dp.errors cannot see it, so a
        failure has to speak for itself — otherwise a message the person sent
        simply never arrives anywhere.
        """
        try:
            while True:
                await asyncio.sleep(_INBOX_TICK)
                box = self.inbox.get(chat_id)
                if box is None or not box.items:
                    return
                if box.downloads:
                    continue
                if time.monotonic() - box.last >= _INBOX_QUIET:
                    break
            await self._flush_inbox(chat_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("inbox flush failed: chat=%s", chat_id)
            with contextlib.suppress(Exception):
                await self.bot.send_message(
                    chat_id, _("❌ Could not pass that on — see /log"))

    async def _flush_inbox(self, chat_id: int) -> None:
        """Everything the chat has queued leaves as a single prompt."""
        box = self.inbox.get(chat_id)
        if box is None or not box.items:
            return
        items = [box.items[mid] for mid in sorted(box.items)]
        paths = [i.path for i in items if i.path]
        if paths and not any(i.text.strip() for i in items):
            # Attachments with nothing said about them wait for the words:
            # a photo now and the question about it a minute later is a normal
            # way to use a phone.
            box.task = None
            if not box.announced:
                box.announced = True
                await self.bot.send_message(chat_id, ngettext(
                    "📎 Saved {count} file. Write something and I will send "
                    "them together.",
                    "📎 Saved {count} files. Write something and I will send "
                    "them together.", len(paths)).format(count=len(paths)))
            return
        # Nothing was awaited since the batch was read, so this pop makes the
        # flush single-winner: a second caller finds an empty inbox.
        self.inbox.pop(chat_id, None)
        mgd, note = await self._batch_target(items)
        if not mgd:
            return
        pieces = [media.Piece(text=i.text, path=i.path, sender=i.sender)
                  for i in items]
        await self._send_prompt(mgd, media.build_batch_prompt(pieces))
        parts = []
        if len(items) > 1:
            parts.append(ngettext("{count} message", "{count} messages",
                                  len(items)).format(count=len(items)))
        if paths:
            parts.append(ngettext("{count} attachment", "{count} attachments",
                                  len(paths)).format(count=len(paths)))
        await self._ack(
            items[-1].message,
            _("➡️ Sent to <b>{name}</b>{extra}{note}").format(
                name=html.escape(mgd.full_label),
                extra=(" · " + " · ".join(parts)) if parts else "",
                note=note),
            mgd,
        )

    async def _batch_target(self, items: list[_Item]) -> tuple:
        """Where a whole batch goes: the first reply naming a session wins.

        A forwarded message often replies to another forwarded message, which
        is no route at all — hence the check that the reply is one of the
        bot's own cards before it decides anything.
        """
        for item in items:
            replied = getattr(item.message, "reply_to_message", None)
            if replied and self.store.session_of_message(replied.message_id):
                return await self._target(item.message)
        return await self._target(items[-1].message)

    # ----------------------------------------------------------------- media
    async def _on_media(self, m: Message) -> None:
        """Save an attachment into the batch, keeping its place in the order."""
        box = self._inbox_of(m.chat.id)
        item = self._hold(m, text=(m.caption or ""), download=True)
        try:
            item.path = await self._download(m, index=box.files - 1)
        finally:
            box.downloads = max(0, box.downloads - 1)
            box.last = time.monotonic()
        if item.path is None:
            box.items.pop(m.message_id, None)

    async def _download(self, m: Message, index: int) -> Path | None:
        """Fetch the file behind *m*; say what went wrong and return None."""
        # A photo sent as a reply switches the active session too, so the
        # caption that follows it lands in the same place.
        mgd, _note = await self._target(m)
        if not mgd:
            return None

        att = _attachment(m)
        if att is None or not att.file_id:
            return None

        size = att.size
        if size > _MAX_ATTACHMENT:
            # Telegram's own ceiling for a bot download, not a rule of ours.
            await m.answer(_("That file is too big ({size} MB). Telegram lets "
                             "a bot download 20 MB at most — put it somewhere "
                             "on disk and send me the path instead.").format(
                                 size=size // 1024 // 1024))
            return None

        try:
            tg_file = await self.bot.get_file(att.file_id)
            if not tg_file.file_path:
                raise RuntimeError(f"Telegram returned no path for {att.file_id}")
            suffix = media.guess_suffix(tg_file.file_path, att.mime, att.fallback)
            path = media.new_path(mgd.session_id, suffix, index, att.name)
            await self.bot.download_file(tg_file.file_path, destination=path)
        except Exception:
            log.exception("attachment download failed")
            await m.answer(_("❌ Could not download the file"))
            return None
        log.info("attachment saved: %s (%s, %s bytes)", path, att.mime or "?", size)
        return path

    # -------------------------------------------------------------- handlers
    async def _on_text(self, m: Message) -> None:
        chat_id, text = m.chat.id, (m.text or "")
        pend_kind = self.pending.get(chat_id, ("—",))[0]
        active = self.store.get_active(chat_id)
        log.info(
            "text chat=%s pending=%s active=%s reply_to=%s text=%r",
            chat_id, pend_kind, (active or "—")[:8],
            getattr(getattr(m, "reply_to_message", None), "message_id", None),
            text[:120],
        )

        pend = self.pending.pop(chat_id, None)
        if pend and time.time() - pend[-1] > _PENDING_TTL:
            await m.answer(_("⌛️ That request has expired — treating this as "
                             "an ordinary message."))
            pend = None
        if pend and pend[0] == "adddir":
            candidate = text.strip()
            path = Path(candidate).expanduser()
            if not path.is_dir():
                await m.answer(
                    _("❌ No such directory: <code>{path}</code>").format(
                        path=html.escape(candidate)),
                    parse_mode="HTML")
            elif self.settings.add_dir(candidate):
                log.info("dir added: %s", path)
                await m.answer(
                    _("✅ Added: <code>{path}</code>").format(
                        path=html.escape(str(path.resolve()))),
                    parse_mode="HTML")
            else:
                await m.answer(_("That directory is already on the list"))
            return
        if pend and pend[0] == "rename":
            _kind, session_id, _ts = pend
            mgd = self.store.get(session_id)
            if not mgd:
                await m.answer(_("That session is gone"))
                return
            await self._rename_session(m, mgd, text)
            return
        if pend and pend[0] == "dir":
            candidate = text.strip()
            if not candidate.startswith(("/", "~")):
                # Almost certainly a message meant for Claude, not a path.
                await m.answer(_(
                    "That does not look like a path (one starts with / or ~), "
                    "so I am sending it to the session."
                ))
            else:
                await self._create(chat_id, candidate)
                return
        elif pend and pend[0] == "dialog":
            _kind, session_id, number, _ts = pend
            mgd = self.store.get(session_id)
            if not mgd:
                await m.answer(_("That session is gone"))
                return
            self._typed(mgd)
            await tmux.send_keys(mgd.window_id, str(number))
            await asyncio.sleep(0.4)
            await self._send_prompt(mgd, text)
            await self._ack(m, _("✏️ Answer sent to <b>{name}</b>").format(
                name=html.escape(mgd.full_label)), mgd)
            return

        if text.startswith("/"):
            cmd = text[1:].split()[0].split("@")[0].lower()
            if cmd in OWN_COMMANDS:
                return          # handled by the dedicated handlers
            # A command Claude parses has to arrive on a line of its own, so
            # whatever is queued goes first and the command follows alone.
            await self._flush_inbox(chat_id)
            self._hold(m, text)
            await self._flush_inbox(chat_id)
            return

        # Ordinary text joins the queue instead of going straight through:
        # a forwarded conversation, an album and the words that explain it
        # belong in one prompt, and the target is worked out at that point
        # (a reply still beats the active session — see _batch_target).
        self._hold(m, text)

    async def _on_callback(self, c: CallbackQuery) -> None:
        data = c.data or ""
        msg = _editable(c)
        if msg is None:
            # Telegram hands out an InaccessibleMessage once a card is older
            # than 48 hours: nothing on it can be edited or answered, so say so
            # instead of dying inside the handler.
            await c.answer(_stale_card("/sessions"), show_alert=True)
            return
        chat_id = msg.chat.id
        # Pressing anything else abandons a half-finished prompt, so a later
        # message is not swallowed as a directory path.
        if not data.startswith("nd:manual") and not data.startswith("dt:"):
            self.pending.pop(chat_id, None)

        if data.startswith("grp:"):
            # A divider row still has to answer a tap: it says what the group
            # below it is, which is exactly what the icons cannot say alone.
            if data == "grp:term":
                await c.answer(_(
                    "Sessions from your own terminal. The bot can only watch "
                    "them — their input is out of reach. «Move into tmux» "
                    "restarts one under the bot, context and all."),
                    show_alert=True)
            else:
                await c.answer(_(
                    "Sessions the bot started in tmux windows. Full control "
                    "from the chat: text, dialog buttons, Esc, /clear."),
                    show_alert=True)
            return

        if data == "ls":
            await c.answer()
            await self._show_sessions(c)
            return

        if data.startswith("upd:"):
            action = data[4:]
            if action == "show":
                await c.answer()
                await self._show_update(c)
            elif action == "check":
                await c.answer(_("Checking…"))
                await self._check_updates(chat_id)
                await self._show_update(c)
            elif action == "all":
                await c.answer(_("Restarting…"))
                await self._restart_all(chat_id)
                await self._show_update(c)
            elif action.startswith("one:"):
                mgd = self._resolve(action[4:])
                if not mgd:
                    await c.answer(_stale_card("/update"), show_alert=True)
                    return
                await c.answer(_("Restarting…"))
                await self._restart_one(chat_id, mgd)
                await self._show_update(c)
            else:
                await c.answer()
            return

        if data == "new":
            await c.answer()
            await self._ask_dir(c)
            return

        if data == "hist":
            await c.answer(_("Looking…"))
            views = await sess.closed_views(self.store, limit=12, roots=self.roots)
            if not views:
                await msg.edit_text(
                    _("🕘 No closed sessions found in the project directories."),
                    reply_markup=history_kb([]),
                )
                return
            await msg.edit_text(
                _("🕘 <b>Recent sessions</b> — closed earlier, context and "
                  "all.\nTap one for details — whether to resume it is decided "
                  "there.\n<i>time · directory · topic · live ones are in "
                  "/sessions</i>"),
                parse_mode="HTML", reply_markup=history_kb(views),
            )
            return

        if data == "foreign":   # legacy entry point, kept harmless
            await c.answer()
            views = await sess.foreign_views(self.store)
            lines = [_("🔒 <b>Terminal sessions</b> (driven only from the "
                       "computer)"), ""]
            for v in views:
                extra = f" — {v.waiting_for}" if v.waiting_for else ""
                lines.append(f"• <b>{v.name}</b> [{v.status}{extra}]\n  <code>{v.short_cwd}</code>")
            lines.append("")
            lines.append(_("To drive a session from a phone, start it with "
                           "{button}.").format(button=_("➕ New session")))
            await msg.edit_text("\n".join(lines), parse_mode="HTML",
                                      reply_markup=sessions_kb([], views))
            return

        if data == "adddir":
            self.pending[chat_id] = ("adddir", time.time())
            await c.answer()
            await msg.answer(_("Send the absolute path of the project "
                               "directory:"))
            return

        if data.startswith("rmdir:"):
            idx = int(data[6:])
            dirs = list(self.roots)
            if 0 <= idx < len(dirs) and not self.cfg.dirs:
                self.settings.remove_dir(dirs[idx])
                log.info("dir removed: %s", dirs[idx])
            await c.answer(_("Removed"))
            await self._show_dirs(c)
            return

        if data == "cleardirs":
            if not self.cfg.dirs:
                self.settings.data["dirs"] = []
                self.settings.save()
                log.info("dirs reset to autodiscovery")
            await c.answer(_("Reset"))
            await self._show_dirs(c)
            return

        if data.startswith("nd:"):
            arg = data[3:]
            if arg == "manual":
                self.pending[chat_id] = ("dir", time.time())
                await c.answer()
                await msg.answer(
                    _("Send the absolute path of a directory (it starts "
                      "with / or ~).\nOr just press any button to cancel.")
                )
                return
            await c.answer(_("Starting…"))
            idx = int(arg)
            if 0 <= idx < len(self.dir_choices):
                await self._create(chat_id, self.dir_choices[idx])
            return

        if data.startswith("res:"):
            v = await self._find_closed(data[4:])
            if not v:
                await c.answer(_("Session not found"), show_alert=True)
                return
            await c.answer()
            await self._safe_edit(
                msg,
                self._closed_details(v), parse_mode="HTML",
                reply_markup=confirm_kb(v.session_id, "closed"),
            )
            return

        if data.startswith("dores:"):
            v = await self._find_closed(data[6:])
            if not v:
                await c.answer(_("Session not found"), show_alert=True)
                return
            await c.answer(_("Bringing it up…"))
            await self._create(chat_id, v.cwd, resume=v.session_id)
            return

        if data.startswith("s:"):
            mgd = self._resolve(data[2:])
            if not mgd:
                await c.answer(_("That session is gone"), show_alert=True)
                return
            await c.answer()
            await self._safe_edit(
                msg,
                await self._managed_details(mgd), parse_mode="HTML",
                reply_markup=confirm_kb(mgd.session_id, "managed"),
            )
            return

        if data.startswith("open:"):
            mgd = self._resolve(data[5:])
            if not mgd:
                await c.answer(_("That session is gone"), show_alert=True)
                return
            previous = self.store.get_active(chat_id)
            self.store.set_active(chat_id, mgd.session_id)
            if previous and previous != mgd.session_id:
                prev = self.store.get(previous)
                if prev:
                    await msg.answer(
                        _("▶️ Text now goes to <b>{name}</b>.\n"
                          "<b>{previous}</b> keeps working — to write to it, "
                          "reply to one of its messages.").format(
                              name=html.escape(mgd.full_label),
                              previous=html.escape(prev.full_label)),
                        parse_mode="HTML",
                    )
            await c.answer(_("Active: {name}").format(name=mgd.full_label))
            await self._show_session_card(c, mgd)
            return

        if data.startswith("d:") or data.startswith("dt:"):
            free = data.startswith("dt:")
            _kind, short, num = data.split(":", 2)
            mgd = self._resolve(short)
            if not mgd:
                await c.answer(_("That session is gone"), show_alert=True)
                return
            if free:
                self.pending[chat_id] = ("dialog", mgd.session_id, int(num), time.time())
                await c.answer()
                await msg.answer(_("Write your own answer:"))
                return
            await self._answer_dialog(mgd, int(num))
            await c.answer(_("Picked {n}").format(n=num))
            with_kb = msg.reply_markup
            if with_kb:
                await msg.edit_reply_markup(reply_markup=None)
            return

        if data.startswith("f:"):
            v = await self._find_foreign(data[2:])
            if not v:
                await c.answer(_("Session not found"), show_alert=True)
                return
            await c.answer()
            await self._safe_edit(
                msg,
                self._foreign_details(v), parse_mode="HTML",
                reply_markup=confirm_kb(v.session_id, "foreign"),
            )
            return

        if data.startswith("grab:"):
            v = await self._find_foreign(data[5:])
            if not v:
                await c.answer(_("Session not found"), show_alert=True)
                return
            await c.answer(_("Moving it over…"))
            await self._adopt_foreign(chat_id, v)
            return

        if data.startswith("cfg:"):
            _kind, short, kind = data.split(":", 2)
            mgd = self._resolve(short)
            if not mgd:
                await c.answer(_("That session is gone"), show_alert=True)
                return
            st = await self._status(mgd)
            u = status_feed.read(mgd.session_id)
            current = {
                "model": (u.model_id if u else "") or st.model,
                "effort": (u.effort if u else "") or st.effort,
                "mode": st.mode,
            }[kind]
            titles = {"model": _("🧠 Model"), "effort": _("◉ Reasoning effort"),
                      "mode": _("🔐 Permission mode")}
            note = ""
            if kind in ("model", "effort"):
                # Claude Code persists these to ~/.claude/settings.json, so the
                # change outlives the session it was made in.
                note = "\n\n" + _("⚠️ Claude keeps this as the default for "
                                   "<b>new</b> sessions too.")
            await c.answer()
            await msg.edit_text(
                _("{title} for <b>{name}</b>\nCurrently: <code>{value}</code>"
                  ).format(title=titles[kind],
                           name=html.escape(mgd.full_label),
                           value=current or "—") + note,
                parse_mode="HTML", reply_markup=choice_kb(mgd.session_id, kind, current),
            )
            return

        if data.startswith("set:"):
            _kind, short, kind, value = data.split(":", 3)
            mgd = self._resolve(short)
            if not mgd:
                await c.answer(_("That session is gone"), show_alert=True)
                return
            ok = await self._apply_setting(mgd, kind, value)
            await c.answer(_("Done") if ok else _("Could not switch that"))
            await self._show_session_card(c, mgd)
            return

        if data.startswith("pic:"):
            mgd = self._resolve(data[4:])
            if not mgd:
                await c.answer(_("That session is gone"), show_alert=True)
                return
            await c.answer(_("Drawing…"))
            dialog = screenmod.find_dialog(await tmux.capture(mgd.window_id))
            if dialog and dialog.preview:
                await self.watcher.send_preview(mgd.session_id, dialog)
            else:
                await msg.answer(_("The diagram has left the screen"))
            return

        if data.startswith("nav:"):
            _kind, short, direction = data.split(":", 2)
            mgd = self._resolve(short)
            if not mgd:
                await c.answer(_("That session is gone"), show_alert=True)
                return
            key = {"up": "Up", "down": "Down", "enter": "Enter"}[direction]
            self._typed(mgd)
            await tmux.send_keys(mgd.window_id, key)
            await c.answer("⬆️" if key == "Up" else
                           ("⬇️" if key == "Down" else _("Selected")))
            return

        if data.startswith("svc:"):
            action = data[4:]
            if action == "show":
                await c.answer()
                await self._safe_edit(
                    msg, await self._service_text(), parse_mode="HTML",
                    reply_markup=service_kb(service.under_systemd()),
                )
            elif action == "ask":
                if not service.under_systemd():
                    await c.answer(_("The bot was started by hand"), show_alert=True)
                    await msg.answer(no_supervisor(), parse_mode="HTML")
                    return
                await c.answer()
                await self._safe_edit(msg, restart_ask(), parse_mode="HTML",
                                      reply_markup=restart_confirm_kb())
            elif action == "go":
                await c.answer(_("Restarting…"))
                await self._do_restart(chat_id)
            return

        if data.startswith("ren:"):
            mgd = self._resolve(data[4:])
            if not mgd:
                await c.answer(_("That session is gone"), show_alert=True)
                return
            self.pending[chat_id] = ("rename", mgd.session_id, time.time())
            await c.answer()
            await msg.answer(
                _("✏️ What should <b>{name}</b> be called?\n"
                  "Send the name in your next message (up to {limit} "
                  "characters).\n<code>-</code> gives the automatic name "
                  "back.").format(name=html.escape(mgd.full_label),
                                  limit=util.NAME_LIMIT),
                parse_mode="HTML",
            )
            return

        if data.startswith("lang:"):
            await self._set_language(c, data[5:])
            return

        if data.startswith("k:"):
            _kind, short, action = data.split(":", 2)
            mgd = self._resolve(short)
            if not mgd:
                await c.answer(_("That session is gone"), show_alert=True)
                return
            await self._session_action(c, mgd, action)
            return

        await c.answer()

    async def _show_langs(self, target: Message | CallbackQuery) -> None:
        """Offer the languages that are actually compiled and installed."""
        following = self.settings.language is None
        current = self._locale(getattr(target, "from_user", None))
        text = _("🌐 <b>Interface language</b>\n\nSpeaking: <b>{name}</b>"
                 ).format(name=language_name(current))
        if following:
            text += "\n<i>" + _("following your Telegram profile") + "</i>"
        here = _editable(target)
        if here is None:
            await target.answer(_stale_card("/lang"), show_alert=True)
            return
        kb = lang_kb(current, following)
        if isinstance(target, CallbackQuery):
            await self._safe_edit(here, text, reply_markup=kb, parse_mode="HTML")
        else:
            await here.answer(text, reply_markup=kb, parse_mode="HTML")

    async def _set_language(self, c: CallbackQuery, code: str) -> None:
        """Switch the interface language, and prove it switched.

        The whole reply — the confirmation, the card, the "/" menu — is built
        inside the new locale, so the answer to a language change is already
        written in that language. Nothing is restarted: the watcher reads the
        setting afresh on its next tick.
        """
        if code == "auto":
            self.settings.language = None
        elif code in offered():
            self.settings.language = code
        else:
            await c.answer(_("I do not know that language"), show_alert=True)
            return
        speaking = self._locale(c.from_user)
        log.info("interface language set to %s (speaking %s)", code, speaking)
        with use_locale(speaking):
            await c.answer(_("Language: {name}").format(
                name=language_name(speaking)))
            # Telegram caches the command list per bot, so it has to be
            # rewritten — otherwise the "/" menu keeps the old language.
            await self._publish_menu()
            await self._show_langs(c)

    async def _find_foreign(self, short: str):
        for v in await sess.foreign_views(self.store):
            if v.session_id.startswith(short):
                return v
        return None

    def _foreign_details(self, v) -> str:
        lines = [f"🔗 <b>{html.escape(v.name)}</b>", ""]
        if v.work_dir:
            lines.append(_("📁 Working in <code>{path}</code>").format(
                path=html.escape(v.work_dir)))
            lines.append(_("🚀 Launched from <code>{path}</code>").format(
                path=html.escape(v.cwd)))
        else:
            lines.append(f"📁 <code>{html.escape(v.cwd)}</code>")
        status = sess.status_label(v.status) + (
            f" — {v.waiting_for}" if v.waiting_for else "")
        lines.append(_("📊 Status: <b>{status}</b>").format(status=status))
        if v.started_at:
            started = datetime.fromtimestamp(v.started_at)
            lines.append(_("🕘 Started: {when}").format(
                when=started.strftime(_("%d.%m %H:%M"))))
        if v.pid:
            lines.append(f"⚙️ PID: <code>{v.pid}</code>")
        lines.append(f"🔑 <code>{v.session_id}</code>")
        lines.append("")
        lines.append(_("⚠️ This session belongs to your terminal — it cannot "
                       "be driven from here."))
        lines.append(_(
            "{button} sends the process a <b>termination signal</b> (SIGTERM) "
            "and brings <code>claude --resume</code> up in a tmux window — from "
            "the directory the session started in, so the project context stays "
            "the same. The terminal window goes dark, the history survives.\n"
            "A terminal session's stdin cannot be reached from outside, so a "
            "clean <code>/exit</code> is impossible here — which is exactly why "
            "the signal is needed."
        ).format(button="«" + _("🔗 Move into tmux") + "»"))
        if v.status == "busy":
            lines.append("")
            lines.append(_("🔴 The session is <b>working</b> right now — better "
                           "to wait until it finishes."))
        return "\n".join(lines)

    async def _adopt_foreign(self, chat_id: int, v) -> None:
        """Stop a terminal session and bring it back up inside tmux."""
        if not v.pid:
            await self.bot.send_message(
                chat_id, _("❌ Unknown PID — I cannot move this one"))
            return
        note = await self.bot.send_message(
            chat_id,
            _("⏳ Stopping {name} (PID {pid})…").format(name=v.name, pid=v.pid))
        try:
            os.kill(v.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            await note.edit_text(_("❌ Not allowed to stop that process"))
            return
        for _tick in range(int(_EXIT_TIMEOUT / 0.5)):
            await asyncio.sleep(0.5)
            if not _pid_alive(v.pid):
                break
        if _pid_alive(v.pid):
            await note.edit_text(
                _("⚠️ Process {pid} did not exit within {seconds} s. End the "
                  "session in the terminal and try again.").format(
                      pid=v.pid, seconds=f"{_EXIT_TIMEOUT:.0f}")
            )
            return
        await note.edit_text(_("✅ {name} stopped — bringing it up in tmux…"
                              ).format(name=v.name))
        await self._create(chat_id, v.cwd, resume=v.session_id)

    async def _find_closed(self, short: str):
        for v in await sess.closed_views(self.store, limit=60, roots=self.roots):
            if v.session_id.startswith(short):
                return v
        return None

    async def _managed_details(self, mgd) -> str:
        views = await sess.managed_views(self.store)
        view = next((v for v in views if v.session_id == mgd.session_id), None)
        st = await self._status(mgd)
        u = status_feed.read(mgd.session_id)

        lines = [f"▶️ <b>{html.escape(mgd.full_label)}</b>", ""]
        lines.append(f"📁 <code>{html.escape(mgd.cwd)}</code>")
        if view:
            status = sess.status_label(view.status) + (
                f" — {view.waiting_for}" if view.waiting_for else "")
            lines.append(_("📊 Status: <b>{status}</b>").format(status=status))
        model = (u.model if u else "") or st.model
        bits = [b for b in (model, f"◉ {u.effort}" if u and u.effort else st.effort,
                            f"🔐 {st.mode_label}" if st.mode else "") if b]
        if bits:
            lines.append("🧠 " + " · ".join(bits))
        behind = updates.stale(mgd.session_id, await updates.installed())
        if behind:
            lines.append(_("⬆️ Claude Code <code>{old}</code> — "
                           "<code>{new}</code> is on disk; /update restarts it "
                           "without losing the context.").format(
                               old=html.escape(behind),
                               new=html.escape(updates.cached_installed())))
        if u and u.ctx_pct is not None:
            tok = f"{u.ctx_tokens:,}".replace(",", " ")
            lines.append(ngettext("📈 Context: {pct}% ({tokens} token)",
                                  "📈 Context: {pct}% ({tokens} tokens)",
                                  u.ctx_tokens).format(pct=u.ctx_pct, tokens=tok))
        elif view and view.status in ("starting", "idle"):
            # A session that has not sent a request yet reports no usage at all.
            lines.append(_("📈 Context: metrics appear after the first request"))
        for lim in (u.limits if u else []):
            lines.append(f"    {lim.label}: {lim.pct}%"
                         + (" · " + _("resets {when}").format(when=lim.reset_text)
                            if lim.reset_text else ""))
        if u and u.cost_usd:
            lines.append(_("💵 Session cost: ${amount}").format(
                amount=f"{u.cost_usd:.2f}"))
        started = datetime.fromtimestamp(mgd.created_at)
        lines.append(_("🕘 Created: {when}").format(
            when=started.strftime(_("%d.%m %H:%M"))))
        lines.append(f"🖥 tmux: <code>{mgd.window_id}</code>")
        lines.append(f"🔑 <code>{mgd.session_id}</code>")
        return "\n".join(lines)

    def _closed_details(self, v) -> str:
        lines = [f"🕘 <b>{html.escape(v.name)}</b>", ""]
        lines.append(f"📁 <code>{html.escape(v.cwd)}</code>")
        lines.append(_("🕘 Last active: {when}").format(when=v.when))
        if v.title and v.opening:
            lines.append(_("💬 It started with: {text}").format(
                text=html.escape(v.opening)))
        if v.size:
            lines.append(
                _("📦 Transcript: {size} MB").format(
                    size=f"{v.size / 1024 / 1024:.1f}")
                if v.size >= 1024 * 1024 else
                _("📦 Transcript: {size} KB").format(size=f"{v.size / 1024:.0f}"))
        lines.append(f"🔑 <code>{v.session_id}</code>")
        lines.append("")
        lines.append(_("{button} runs <code>claude --resume</code> in a new "
                       "tmux window — the session picks up all of its "
                       "context.").format(button="«" + _("▶️ Resume") + "»"))
        return "\n".join(lines)

    async def _status(self, mgd):
        """Model / effort / permission mode as shown in the status line."""
        try:
            return screenmod.read_status(await tmux.capture(mgd.window_id))
        except tmux.TmuxError:
            return screenmod.Status()

    async def _show_session_card(self, c: CallbackQuery, mgd) -> None:
        msg = _editable(c)
        if msg is None:
            await c.answer(_stale_card("/sessions"), show_alert=True)
            return
        views = await sess.managed_views(self.store)
        view = next((v for v in views if v.session_id == mgd.session_id), None)
        if not view:
            return
        st = await self._status(mgd)
        # Permission mode lives only on screen; everything else is exact.
        u = status_feed.read(mgd.session_id)
        bits = []
        model = (u.model if u else "") or st.model
        effort = (u.effort if u else "") or st.effort
        ctx = f"{u.ctx_pct}%" if u and u.ctx_pct is not None else (
            f"{st.context_pct}%" if st.context_pct else "")
        if model:
            bits.append(f"🧠 {model}")
        if effort:
            bits.append(f"◉ {effort}")
        if st.mode:
            bits.append(f"🔐 {st.mode_label}")
        if ctx:
            bits.append(f"ctx {ctx}")
        behind = updates.stale(mgd.session_id, await updates.installed())
        if behind:
            bits.append(f"⬆️ {behind} → {updates.cached_installed()}")
        text = (_("▶️ Active session: <b>{name}</b>").format(
                    name=html.escape(view.name))
                + f"\n<code>{view.short_cwd}</code>\n"
                + _("Status: {status}").format(
                    status=sess.status_label(view.status)))
        if bits:
            text += "\n" + " · ".join(bits)
        try:
            await self._safe_edit(msg, text, reply_markup=session_kb(view),
                                  parse_mode="HTML")
        except Exception:
            await msg.answer(text, reply_markup=session_kb(view), parse_mode="HTML")

    async def _apply_setting(self, mgd, kind: str, value: str) -> bool:
        if kind == "model":
            await self._send_prompt(mgd, f"/model {value}")
            await asyncio.sleep(2.0)
            return True
        if kind == "effort":
            await self._send_prompt(mgd, f"/effort {value}")
            await asyncio.sleep(2.0)
            return True
        # Permission mode has no slash command — Shift+Tab cycles
        # auto → manual → accept edits → plan, so press until it matches.
        for _press in range(len(_MODE_CYCLE) + 1):
            st = await self._status(mgd)
            if st.mode == value:
                return True
            self._typed(mgd)
            await tmux.send_keys(mgd.window_id, "BTab")
            await asyncio.sleep(0.7)
        return (await self._status(mgd)).mode == value

    async def _answer_dialog(self, mgd, number: int) -> None:
        """Pick option *number*.

        Digits select directly, which is immune to where the cursor happens to
        be. Beyond 9 there is no digit shortcut, so walk the list explicitly.
        """
        self._typed(mgd)
        if 1 <= number <= 9:
            await tmux.send_keys(mgd.window_id, str(number))
            return
        for _step in range(30):
            await tmux.send_keys(mgd.window_id, "Up")
        for _step in range(number - 1):
            await tmux.send_keys(mgd.window_id, "Down")
        await tmux.send_keys(mgd.window_id, "Enter")

    async def _session_action(self, c: CallbackQuery, mgd, action: str) -> None:
        if action == "esc":
            self._typed(mgd)
            await tmux.send_keys(mgd.window_id, "Escape")
            await c.answer(_("Esc sent"))
        elif action == "screen":
            await c.answer(_("Taking a snapshot…"))
            await self.watcher.send_screen(mgd.window_id, mgd.full_label, mgd.session_id)
        elif action == "usage":
            await c.answer()
            await self.bot.send_message(
                c.message.chat.id if c.message else self.cfg.owner,
                usage_report(mgd.full_label, status_feed.read(mgd.session_id),
                             await self._status(mgd)),
                parse_mode="HTML",
            )
        elif action == "ctx":
            await self._send_prompt(mgd, "/context")
            await c.answer(_("/context sent"))
        elif action == "clear":
            await self._send_prompt(mgd, "/clear")
            await c.answer(_("/clear sent"))
        elif action == "close":
            await self._close(mgd.session_id)
            await c.answer(_("Closed"))
            await self._show_sessions(c)
        else:
            await c.answer()

    # ------------------------------------------------------------------- run
    # Shown in Telegram's "/" menu, so the commands are discoverable.
    # Marked here, translated in _publish_menu: a class body runs at import
    # time, when no language has been chosen yet.
    MENU = [
        ("sessions", N_("Session list and controls")),
        ("new", N_("New session in a chosen directory")),
        ("usage", N_("Quota and context, without asking Claude")),
        ("screen", N_("Snapshot of the active session's terminal")),
        ("esc", N_("Interrupt what Claude is doing")),
        ("clear", N_("Clear the active session's context")),
        ("rename", N_("Give a session your own name")),
        ("exit", N_("End the active session cleanly")),
        ("dirs", N_("Project directories for new sessions")),
        ("lang", N_("Interface language")),
        ("log", N_("Last entries from the bot's journal")),
        ("update", N_("Claude Code version and session restarts")),
        ("service", N_("Bot state: uptime, version, restart")),
        ("help", N_("Help")),
    ]

    async def _publish_menu(self) -> None:
        """Register the command list and make the "/" menu button appear.

        Two separate things: set_my_commands fills the list, set_chat_menu_button
        decides whether the button beside the input box shows it. A chat left on
        "default" may show the attachment icon instead, so it is set explicitly.
        The commands are published to the private-chat scope as well — the
        default scope alone is not always picked up by clients.

        Language works the same way here as it does for messages. Telegram
        serves a language-tagged list to clients set to that language, which is
        exactly the guess to make while nobody has chosen one. Once somebody
        has, those tags have to go: otherwise a Ukrainian client would keep the
        Ukrainian menu after /lang en, and the menu would contradict every
        other word the bot says.
        """
        chosen = self.settings.language
        # The untagged list is what a client of any other language gets, so it
        # stays English unless a language was explicitly chosen.
        await self._push_menu(None, chosen or DEFAULT_LOCALE)
        for locale in offered():
            if chosen:
                await self._drop_menu(locale)
            else:
                await self._push_menu(locale, locale)
        for chat_id in sorted(self.cfg.allowed):
            try:
                await self.bot.set_chat_menu_button(
                    chat_id=chat_id, menu_button=MenuButtonCommands(),
                )
            except Exception:
                log.exception("set_chat_menu_button failed for %s", chat_id)

    async def _push_menu(self, code: str | None, locale: str) -> None:
        """Publish the command list, in *locale*, tagged for *code* clients."""
        with use_locale(locale):
            cmds = [BotCommand(command=c, description=_(d)) for c, d in self.MENU]
        for scope in (None, BotCommandScopeAllPrivateChats()):
            try:
                await self.bot.set_my_commands(cmds, scope=scope,
                                               language_code=code)
            except Exception:
                log.exception("set_my_commands failed for %s", code or "default")

    async def _drop_menu(self, code: str) -> None:
        """Forget a language-tagged list, so the untagged one is served again."""
        for scope in (None, BotCommandScopeAllPrivateChats()):
            try:
                await self.bot.delete_my_commands(scope=scope, language_code=code)
            except Exception:
                log.exception("delete_my_commands failed for %s", code)

    async def run(self) -> None:
        # Everything sent from here on — startup notices, and any task started
        # below — inherits this locale; per-update handlers override it.
        i18n.current_locale = self._locale()
        await self._publish_menu()
        # A restart asked for from the chat has to report back in the chat —
        # otherwise "restarting…" is the last thing the user ever sees.
        pending = service.take_restart()
        if pending:
            took = service.human_delta(time.time() - float(pending["at"]))
            log.info("came back from a requested restart in %s", took)
            await self.bot.send_message(
                pending["chat_id"],
                _("✅ The bot is back — the restart took {duration}.\n"
                  "Code: <code>{version}</code>").format(
                      duration=took, version=html.escape(service.version())),
                parse_mode="HTML",
            )
        # Resolve the CLI up front: if it is missing, every session list would
        # silently come back empty, which is far harder to notice later.
        if not sess.claude_bin():
            await self.bot.send_message(
                self.cfg.owner,
                _("⚠️ I could not find the <code>claude</code> executable. "
                  "Lists of live sessions will be empty.\n"
                  "Set the path in .env: "
                  "<code>CCBOT_CLAUDE_BIN=/path/to/claude</code>"),
                parse_mode="HTML",
            )
        await tmux.ensure_session()
        if not status_feed.available():
            log.warning(
                "statusline tee is not active — metrics will be scraped off "
                "the screen; see the README section on exact metrics"
            )
        dropped = media.cleanup()
        if dropped:
            log.info("removed %d stale attachments", dropped)
        stale = status_feed.cleanup()
        if stale:
            log.info("removed %d stale status payloads", stale)
        self.watcher.start()
        log.info("polling started")
        try:
            await self.dp.start_polling(self.bot, handle_signals=False)
        finally:
            await self.watcher.stop()
            await self.bot.session.close()
