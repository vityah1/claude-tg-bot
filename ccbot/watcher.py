"""Background polling: pushes Claude's output and blocking dialogs to Telegram.

Two independent signals are watched per managed session:

* the transcript — what Claude *said* (clean text, no ANSI);
* the terminal   — whether a dialog is *blocking* it (the only thing the
  transcript cannot tell us in time).
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import re
import tempfile
import time
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile

from . import render, rich, status_feed, tmux, transcript, updates
from . import screen as screenmod
from . import sessions as sess
from .i18n import _, ngettext, resolve
from .i18n import use as use_locale
from .keyboards import blocked_kb, dialog_kb, update_notice_kb
from .settings import Settings
from .state import Store
from .transcript import TranscriptReader
from .util import as_pre, as_pre_lines, gauges, split_text, usage_suffix

log = logging.getLogger("ccbot.watcher")

# Wait for a lull before flushing, so one reply is not split across messages.
# A screen promised for later (`screen_when_idle`): how long to hold off
# before the first look — a command needs a moment to start, and a session
# read too early looks idle because nothing has begun yet — and when to give
# up on a session that never goes quiet.
_SCREEN_DELAY = 4.0
_SCREEN_GIVEUP = 600.0
_FLUSH_IDLE = 2.0
_MAX_BUFFER_AGE = 25.0

# Alert steps. Each fires once on the way up and re-arms if usage drops
# (a /clear, or a fresh rate-limit window).
_CTX_STEPS = (60, 75, 85, 95)
_LIMIT_STEPS = (80, 90, 95)

# How often to go looking for a session's opening prompt while it has no title.
_TITLE_RETRY = 30.0

# How long the same watcher failure stays quiet after it has been reported.
_FAIL_REPEAT = 3600.0

# How often to ask what version of Claude Code is on disk. It only moves when
# the background updater has been at work, and asking spawns a node process.
_VERSION_CHECK = 600.0

# Silence while Claude works reads as a broken bot, so a working session gets a
# heartbeat: one message, edited in place, never a stream of them.
_PULSE_AFTER = 75.0
_PULSE_EDIT = 45.0

# Telegram's mobile clients wrap code blocks instead of scrolling them, so a
# drawing only survives inline if it fits a phone-width column.
_PHONE_COLS = 36

# How long an unparsed-question card may go stale before it is redrawn in
# place. An edit costs the reader nothing; a new message costs a notification.
_BLOCKED_EDIT = 30.0

# How long a key the bot pressed is allowed to take effect before the session
# is judged to be waiting on anything. The screen reacts at once; the agent
# list does not, and that gap is not a question nobody answered.
_ACT_GRACE = 8.0
# And how long "waiting, with no dialog I understand" has to hold before it is
# announced. Every turn passes through that state on its way somewhere else.
_BLOCKED_SETTLE = 5.0
# Older than this, an agent-list reading says nothing about right now.
_STATUS_MAX_AGE = 10.0
# What heads a dialog card. The settings dialogs are not questions Claude is
# asking — showing them under a "❓" read as one.
_DIALOG_ICON = {"model": "🧠 ", "effort": "◉ "}


def _dialog_title(dialog) -> str:
    """What heads the card. A settings dialog gets a translated name.

    Its own is the terminal's ("Select model", "Effort"), and a card is read
    by the user rather than by the parser.
    """
    if dialog.kind == "model":
        return _("Model")
    if dialog.kind == "effort":
        return _("Reasoning effort")
    return dialog.title or ""

# A dialog redraws in stages once a key lands: the chosen row disappears
# before the rest of it does. Reporting a frame from inside that redraw is how
# an answered question came back with only "Cancel" left on it.
_DIALOG_SETTLE = 3.0

# How much scrollback to read for the reasoning above a dialog: a long answer
# has usually pushed its own beginning off the top of the pane by then.
_SAID_HISTORY = 400
# When the transcript finally delivers that same text, it is recognised by how
# much of it the screen already showed. Short blocks are not worth judging —
# a duplicated line costs less than a swallowed answer.
_ECHO_RATIO = 0.75
_ECHO_MIN_WORDS = 20

_WORD_RE = re.compile(r"\w+")


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


class SessionRuntime:
    """Per-session bookkeeping that lives only in memory."""

    def __init__(self, session_id: str, offset: int):
        self.reader = TranscriptReader(session_id, offset)
        self.buffer: list[str] = []
        self.buffer_since = 0.0
        self.last_dialog_sig: str | None = None
        self.last_dialog_state: str | None = None
        self.dialog_msg_id: int | None = None
        self.reported_gone = False
        self.alerted: dict[str, int] = {}
        self.title_tried = 0.0
        # None means "this session is not waiting on anything I failed to
        # parse". Anything else means the card has already been sent.
        self.blocked_sig: str | None = None
        self.blocked_msg_id: int | None = None
        self.blocked_edited = 0.0
        # When the state was first seen, and when the bot last typed here.
        self.blocked_since = 0.0
        self.acted_at = 0.0
        # What was read off the screen ahead of a dialog: the words, to spot the
        # transcript's own copy of it later, and a signature, so walking a
        # multi-part question does not resend it once per section.
        self.said_words: set[str] = set()
        self.said_sig: str | None = None
        self.work_since = 0.0
        self.pulse_msg_id: int | None = None
        self.pulse_edited = 0.0

    def remember_said(self, text: str) -> None:
        self.said_words = _words(text)
        self.said_sig = f"{len(text)}:{text[:120]}"

    def echoes_screen(self, text: str) -> bool:
        """Is this the transcript repeating what was already read off screen?

        Judged once per block that was read: a second one is new by then. The
        two are never byte-identical — the terminal renders markdown and wraps
        to its width — so the test is how much of the vocabulary matches.
        """
        known, self.said_words = self.said_words, set()
        if not known:
            return False
        words = _words(text)
        if len(words) < _ECHO_MIN_WORDS:
            return False
        return len(words & known) / len(words) >= _ECHO_RATIO

    def clear_blocked(self) -> None:
        """Forget the unparsed-question card: the wait it belonged to is over."""
        self.blocked_sig = None
        self.blocked_msg_id = None
        self.blocked_edited = 0.0
        self.blocked_since = 0.0


class Watcher:
    def __init__(self, bot: Bot, store: Store, settings: Settings,
                 chat_id: int, interval: float = 1.5):
        self.bot = bot
        self.store = store
        self.settings = settings
        self.chat_id = chat_id
        self.interval = interval
        self.runtimes: dict[str, SessionRuntime] = {}
        # Sessions owed a screen once they go quiet, and the earliest moment
        # each is worth looking at.
        self.pending_screen: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        # Last failure announced in the chat, so a fault that repeats every
        # 1.5 seconds does not become 2400 messages an hour.
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

    def note_input(self, session_id: str) -> None:
        """Remember that the bot has just typed into this session.

        A keypress lands long before `claude agents --json` admits it: the
        dialog leaves the screen immediately, while the agent list still reads
        "waiting" for a few seconds. Without this mark the watcher took that
        gap for a question it had failed to parse and posted the fallback
        screen on top of an answer that had already worked.
        """
        rt = self.runtimes.get(session_id)
        if rt is not None:
            rt.acted_at = time.time()
            rt.blocked_since = 0.0

    def screen_when_idle(self, session_id: str) -> None:
        """Promise a screen snapshot once this session has gone quiet.

        `/context` and `/compact` are Claude Code's own commands: they print
        their answer on the terminal and write nothing to the transcript that
        reaches the chat (a local command is a `user` record, and only
        assistant records are forwarded). A button that sends one and says
        nothing else is a button that appears to do nothing — which is exactly
        how «📉 Context breakdown» read. Immediately is no good either: a
        compaction takes tens of seconds, and a screen captured while it runs
        shows the spinner instead of the result.
        """
        self.pending_screen[session_id] = time.time() + _SCREEN_DELAY

    async def _pending_screen(self, session_id: str, window_id: str, name: str,
                              busy: bool, dialog: bool) -> None:
        due = self.pending_screen.get(session_id)
        if due is None:
            return
        if time.time() < due or busy or dialog:
            if time.time() - due > _SCREEN_GIVEUP:
                del self.pending_screen[session_id]
                log.info("promised screen dropped, never idle id=%s",
                         session_id[:8])
            return
        del self.pending_screen[session_id]
        log.info("promised screen id=%s name=%s", session_id[:8], name)
        await self.send_screen(window_id, name, session_id)

    def forget_dialog(self, session_id: str) -> None:
        """Let go of the dialog card, so the next tick never edits it again.

        The bot answers a settings dialog by walking the cursor there, and
        every move changes the state the card is drawn from — a tick that
        captured the screen one move before the commit would then edit the
        confirmation that has since replaced the card.
        """
        rt = self.runtimes.get(session_id)
        if rt is not None:
            rt.last_dialog_sig = None
            rt.last_dialog_state = None
            rt.dialog_msg_id = None

    def forget(self, session_id: str) -> None:
        self.runtimes.pop(session_id, None)
        self.pending_screen.pop(session_id, None)
        updates.forget(session_id)

    def adopt(self, session_id: str, skip_existing: bool = True) -> None:
        """Track a session; by default ignore transcript written before now."""
        rt = SessionRuntime(session_id, 0)
        if skip_existing:
            rt.reader.seek_to_end()
        self.runtimes[session_id] = rt
        self.store.set_offset(session_id, rt.reader.offset)

    async def _loop(self) -> None:
        while True:
            # No update to read a language off, so the locale is opened here.
            # A task only inherits context set before it was created, and
            # /lang may change the answer while the loop is already running.
            with use_locale(self._locale()):
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:           # never let the loop die
                    log.exception("watcher tick failed")
                    await self._report_failure(exc)
                else:
                    await self._clear_failure()
            await asyncio.sleep(self.interval)

    def _locale(self) -> str:
        """Language for messages nobody asked for.

        There is no Telegram user on this path: the watcher speaks first. So
        it falls back to the language last seen on the profile — otherwise
        Claude's answers would arrive in English while every reply to a typed
        message came back in the user's own language.
        """
        return resolve(self.settings.language, self.settings.telegram_language)

    async def _report_failure(self, exc: Exception) -> None:
        """Say in the chat that the watcher is broken.

        A tick that raises stops every session's output from reaching Telegram,
        and from the phone that is indistinguishable from Claude simply being
        quiet — the failure has to announce itself. It repeats every poll, so
        the same fault is reported once and then held back for an hour.
        """
        signature = f"{type(exc).__name__}: {exc}"
        now = time.time()
        if (signature == self.failure_sig
                and now - self.failure_told < _FAIL_REPEAT):
            return
        self.failure_sig = signature
        self.failure_told = now
        await self._say(
            _("⚠️ <b>The session watcher has crashed</b>\n"
              "<code>{error}</code>\n\n"
              "While this lasts, Claude's replies do not reach the chat — "
              "prompts you send still get through to the session. Details in "
              "/log; /restart usually cures it.").format(
                  error=html.escape(signature[:300])),
            parse_mode="HTML",
        )

    async def _clear_failure(self) -> None:
        """Report that the loop is healthy again, once."""
        if not self.failure_sig:
            return
        self.failure_sig = None
        self.failure_told = 0.0
        await self._say(_("✅ The watcher is back — following the sessions again."))

    async def _rebind_cleared(self) -> None:
        """Follow sessions whose id changed under us.

        `/clear` does not restart Claude — it keeps the process, the window and
        the launch name, and starts a brand new transcript under a brand new
        session id. Nothing tells the bot about it: the old transcript simply
        stops growing, and every answer after that is written where nobody is
        reading. The launch name (`claude -n`) is the thread back: it survives
        the clear, and `claude agents --json` reports it next to the new id.
        """
        managed = self.store.all_managed()
        if not managed:
            return
        agents = await sess.live_agents()
        live_ids = {a.get("sessionId") for a in agents}
        by_name: dict[tuple[str, str], str] = {}
        for a in agents:
            sid, name, cwd = a.get("sessionId"), a.get("name"), a.get("cwd") or ""
            if sid and name:
                by_name[(name, cwd)] = sid
        for m in managed:
            if m.session_id in live_ids:
                continue
            fresh = by_name.get((m.name, m.cwd))
            if not fresh or fresh == m.session_id:
                continue
            log.info("session rebound %s -> %s (name=%s)",
                     m.session_id[:8], fresh[:8], m.name)
            self.store.rebind(m.session_id, fresh)
            self.runtimes.pop(m.session_id, None)
            self.runtimes[fresh] = SessionRuntime(fresh, 0)
            await self._say(
                _("🧹 <b>{name}</b>: context cleared — the session was given a "
                  "new id, and I am following it there.").format(
                      name=html.escape(m.full_label)),
                session_id=fresh, parse_mode="HTML",
            )

    async def _check_version(self) -> None:
        """Say once when a newer Claude Code is waiting on disk.

        The sessions cannot notice this themselves: a running process keeps
        the build it started with, and nothing in the TUI mentions the file
        that has meanwhile been replaced. Announced once per release — the
        note is kept in the settings file, so a bot restart does not turn one
        release into a second notification.
        """
        now = time.time()
        if now - self.version_checked < _VERSION_CHECK:
            return
        self.version_checked = now
        disk = await updates.installed()
        if not disk or self.settings.notified_version == disk:
            return
        behind = [m for m in self.store.all_managed()
                  if updates.stale(m.session_id, disk)]
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

    async def _tick(self) -> None:
        await self._check_version()
        await self._rebind_cleared()
        live = {a.get("sessionId"): a for a in await sess.live_agents()}
        for m in self.store.all_managed():
            rt = self.runtimes.get(m.session_id)
            if rt is None:
                rt = SessionRuntime(m.session_id, m.offset)
                self.runtimes[m.session_id] = rt
            agent = live.get(m.session_id) or {}
            await self._tick_session(m.session_id, m.window_id,
                                     self._label(m, rt), rt,
                                     agent.get("status", ""))

    def _label(self, m, rt: SessionRuntime) -> str:
        """The session's display name, filling in a missing title.

        Claude writes an "ai-title" record only for sessions it named itself,
        and the bot always launches with -n, so a managed session never gets
        one. What it was asked first is the next best thing to know it by —
        "pay4say-0e5e" is not something anyone remembers.
        """
        if m.title or not m.is_auto_named:
            return m.full_label
        if time.time() - rt.title_tried < _TITLE_RETRY:
            return m.full_label
        rt.title_tried = time.time()
        opening = transcript.first_prompt(m.session_id)
        if not opening:
            return m.full_label
        log.info("title from first prompt id=%s: %r", m.session_id[:8], opening)
        self.store.set_title(m.session_id, opening)
        return (self.store.get(m.session_id) or m).full_label

    async def _tick_session(self, session_id: str, window_id: str,
                            name: str, rt: SessionRuntime,
                            agent_status: str = "") -> None:
        if not await tmux.window_exists(window_id):
            if not rt.reported_gone:
                rt.reported_gone = True
                log.info("session gone id=%s name=%s window=%s",
                         session_id[:8], name, window_id)
                await self._say(_("💀 Session {name} is gone (its tmux window "
                                  "was closed).").format(name=name))
                self.store.remove(session_id)
                self.forget(session_id)
            return

        # 1. transcript → what Claude said
        events = rt.reader.read_new()
        if events:
            self.store.set_offset(session_id, rt.reader.offset)
            for ev in events:
                if ev.kind == "title":
                    self.store.set_title(session_id, ev.text)
                elif ev.kind == "tool":
                    rt.buffer.append(f"🔧 {ev.text}")
                elif rt.echoes_screen(ev.text):
                    log.info("dropping the transcript copy of text already sent "
                             "from the screen id=%s chars=%d",
                             session_id[:8], len(ev.text))
                else:
                    rt.buffer.append(ev.text)
            if rt.buffer:
                rt.buffer_since = rt.buffer_since or time.time()

        # 2. terminal → is anything blocking?
        try:
            raw = await tmux.capture(window_id)
        except tmux.TmuxError:
            return
        dialog = screenmod.find_dialog(raw)
        busy = screenmod.is_busy(raw)
        status = screenmod.read_status(raw)
        usage = status_feed.read(session_id)
        await self._check_usage(name, rt, usage, status)

        # Flush buffered output once Claude goes quiet, or a dialog appears.
        age = time.time() - rt.buffer_since if rt.buffer_since else 0
        should_flush = rt.buffer and (
            dialog is not None
            or (not busy and age >= _FLUSH_IDLE)
            or age >= _MAX_BUFFER_AGE
        )
        if should_flush:
            body = "\n\n".join(rt.buffer).strip()
            rt.buffer.clear()
            rt.buffer_since = 0.0
            log.info("flush id=%s name=%s chars=%d", session_id[:8], name, len(body))
            await self._drop_pulse(rt)
            await self._emit(session_id, name, body, usage, status)

        await self._pending_screen(session_id, window_id, name, busy,
                                   dialog is not None)
        await self._pulse(session_id, name, raw, rt, dialog is not None)

        if dialog is None:
            rt.last_dialog_sig = None
            rt.last_dialog_state = None
            rt.dialog_msg_id = None
            await self._report_blocked(session_id, name, raw, rt,
                                       agent_status, busy)
            return
        rt.clear_blocked()

        # Which question this is, versus which row is highlighted. The first
        # warrants a new message; the second only edits the existing one, so
        # walking a list with the arrow buttons does not spam the chat.
        sig = dialog.question + "|" + "|".join(o.label for o in dialog.options)
        # Ticks belong to the state, not to the question: a checkbox changes
        # with every press, and reading that as a new question would answer
        # one multi-select list with a chat full of copies of it.
        state = str([(o.number, o.selected, o.checked, o.current)
                     for o in dialog.options])
        if sig == rt.last_dialog_sig and state == rt.last_dialog_state:
            return
        same_question = sig == rt.last_dialog_sig
        if not same_question and time.time() - rt.acted_at < _DIALOG_SETTLE:
            return          # mid-redraw: wait for the screen to settle
        if not same_question:
            log.info("dialog id=%s title=%r options=%d preview=%s",
                     session_id[:8], dialog.title, len(dialog.options),
                     bool(dialog.preview))
            if not should_flush and dialog.kind == "choice":
                # The transcript said nothing this tick, which for a question
                # is the normal case rather than silence — see _preface. The
                # settings pickers are the exception: nothing is said above
                # them (the chat opened them), and they draw no rule the
                # reading could stop at, so it used to send a line of the
                # picker's own state as the preamble to it.
                await self._preface(session_id, window_id, name, rt, usage, status)
        rt.last_dialog_sig = sig
        rt.last_dialog_state = state

        blocks, kb = self._dialog_blocks(name, session_id, dialog)
        if same_question and rt.dialog_msg_id:
            if await rich.edit(self.bot, self.chat_id, rt.dialog_msg_id,
                               blocks=blocks, reply_markup=kb):
                return
            try:
                await self.bot.edit_message_text(
                    self._render_dialog(name, session_id, dialog)[0],
                    chat_id=self.chat_id, message_id=rt.dialog_msg_id,
                    reply_markup=kb, parse_mode="HTML",
                )
                return
            except Exception:
                pass          # message too old or unchanged — fall through
        msg = await self._say_rich(session_id, blocks=blocks, reply_markup=kb)
        if msg is None:
            # No rich messages here: the old card, and a wide drawing goes
            # back to being a picture because a <pre> would wrap it.
            text, kb = self._render_dialog(name, session_id, dialog)
            msg = await self._say_html(text, session_id=session_id, reply_markup=kb)
            if dialog.preview and render.max_line_width(dialog.preview) > _PHONE_COLS:
                await self.send_preview(session_id, dialog)
        rt.dialog_msg_id = getattr(msg, "message_id", None)

    async def _emit(self, session_id: str, name: str, body: str,
                    usage, status) -> None:
        """Send one block of Claude's output.

        Claude writes Markdown, and since Bot API 10.1 Telegram renders it —
        headings, tables and fenced code arrive as themselves rather than as
        the punctuation around them, and 32 768 characters usually mean no
        split at all. The chunked plain-text path stays underneath for when
        the API will not take the document.
        """
        tail = usage_suffix(usage, status)
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

    async def _preface(self, session_id: str, window_id: str, name: str,
                       rt: SessionRuntime, usage, status) -> None:
        """Send the reasoning that leads into a question, before the question.

        Claude lays out the options and then asks about them, so the answer
        depends on the text — and that text used to arrive *after* the choice
        had been made. Not a bug in the flush: Claude Code writes an assistant
        record only when the tool call inside it returns, and AskUserQuestion
        returns on a human. Minutes of waiting are minutes the transcript
        holds nothing at all.

        The terminal has it, though, so it is read from there — and the
        transcript's own copy, when it does arrive, is dropped by
        `SessionRuntime.echoes_screen`.
        """
        try:
            deep = await tmux.capture(window_id, history=_SAID_HISTORY)
        except tmux.TmuxError:
            return
        said = screenmod.said_above_dialog(deep)
        if not said or rt.said_sig == f"{len(said)}:{said[:120]}":
            return          # nothing said, or already sent for this question
        log.info("preface id=%s name=%s chars=%d", session_id[:8], name, len(said))
        rt.remember_said(said)
        await self._drop_pulse(rt)
        await self._emit(session_id, name, said, usage, status)

    async def _report_blocked(self, session_id: str, name: str, raw: str,
                              rt: SessionRuntime, agent_status: str,
                              busy: bool) -> None:
        """Speak up when a session waits on a question we could not parse.

        `find_dialog` only knows the prompt shapes it has been taught, and a
        shape it misses leaves the session blocked with nothing in the chat —
        exactly the silence this bot exists to prevent. `claude agents --json`
        says "waiting" regardless of the shape, so it is the honest signal:
        forward the screen and the keys that work without understanding it.

        Once per wait, though — not once per redraw. A blocked session keeps
        drawing: the spinner turns, a tool prints, the status line re-renders.
        Deciding by what is on screen therefore announced the same question
        every couple of seconds until the chat was unreadable. The honest
        boundary is the wait itself, so the screen only ever edits the message
        that already exists, and 🖥 fetches a fresh one on demand.

        And only when the wait is real. Answering a dialog from a button used
        to produce this card: the option was taken, the dialog vanished from
        the screen, and the agent list — which is polled, cached, and briefly
        unreadable while Claude Code updates itself — went on saying "waiting"
        for a couple of seconds. Three things have to agree before a card is
        worth a notification: the terminal is not visibly working, nothing was
        typed here a moment ago, and the state held still long enough to be
        confirmed by a reading taken just now.
        """
        now = time.time()
        if busy or agent_status != "waiting":
            rt.clear_blocked()
            return
        if now - rt.acted_at < _ACT_GRACE:
            rt.blocked_since = 0.0
            return
        signature = screenmod.quiet_signature(raw)
        if rt.blocked_sig is not None:
            await self._refresh_blocked(session_id, name, raw, rt, signature)
            return
        if not rt.blocked_since:
            rt.blocked_since = now
            return
        if now - rt.blocked_since < _BLOCKED_SETTLE:
            return
        if not await self._still_waiting(session_id):
            # Restart the clock rather than clearing: if the reading was simply
            # unavailable, this retries in _BLOCKED_SETTLE instead of every tick.
            rt.blocked_since = now
            return
        # Set before sending: a send that fails must not become a retry every
        # 1.5 seconds. _say_html has already logged the failure.
        rt.blocked_sig = signature
        rt.blocked_edited = time.time()
        log.info("blocked without a parsed dialog id=%s name=%s",
                 session_id[:8], name)
        msg = await self._say_rich(
            session_id, blocks=self._blocked_blocks(name, raw),
            reply_markup=blocked_kb(session_id),
        )
        if msg is None:
            msg = await self._say_html(
                self._blocked_text(name, raw), session_id=session_id,
                reply_markup=blocked_kb(session_id),
            )
        rt.blocked_msg_id = getattr(msg, "message_id", None)

    async def _still_waiting(self, session_id: str) -> bool:
        """Ask the agent list again, and believe it only if it just answered.

        `live_agents` falls back to its last reply when the CLI cannot be run,
        which is exactly what happens for a second or two while Claude Code
        replaces its own binary. A "waiting" served from that cache described
        a question the user had already answered.
        """
        agents = await sess.live_agents(force=True)
        if sess.live_age() > _STATUS_MAX_AGE:
            log.debug("agent list too stale to call %s blocked", session_id[:8])
            return False
        return any(a.get("sessionId") == session_id and a.get("status") == "waiting"
                   for a in agents)

    async def _refresh_blocked(self, session_id: str, name: str, raw: str,
                               rt: SessionRuntime, signature: str) -> None:
        """Keep the already-sent card current, in place and without a ping."""
        now = time.time()
        if (rt.blocked_msg_id is None or signature == rt.blocked_sig
                or now - rt.blocked_edited < _BLOCKED_EDIT):
            return
        rt.blocked_sig = signature
        rt.blocked_edited = now
        if await rich.edit(self.bot, self.chat_id, rt.blocked_msg_id,
                           blocks=self._blocked_blocks(name, raw),
                           reply_markup=blocked_kb(session_id)):
            return
        try:
            await self.bot.edit_message_text(
                self._blocked_text(name, raw), chat_id=self.chat_id,
                message_id=rt.blocked_msg_id,
                reply_markup=blocked_kb(session_id), parse_mode="HTML",
            )
        except Exception:
            # Too old to edit, or unchanged after trimming — neither is worth
            # a word to the user, who already has the card and its buttons.
            log.debug("blocked card edit skipped", exc_info=True)

    @staticmethod
    def _blocked_head(name: str) -> str:
        return _("⚠️ {name} is waiting for an answer, but I did not recognise "
                 "the question. Here is the screen:").format(name=name)

    @staticmethod
    def _blocked_foot() -> str:
        return _("The buttons below work without understanding it: digits "
                 "pick an option, the arrows walk the list, Esc backs out. "
                 "🖥 sends a fresh screen.")

    def _blocked_blocks(self, name: str, raw: str) -> list[rich.Block]:
        """The whole screen, scrollable, with the frame straightened out."""
        return [
            rich.para(self._blocked_head(name)),
            rich.pre(rich.ascii_frame(screenmod.tail_text(raw, rich.PRE_LIMIT))),
            rich.para(rich.italic(self._blocked_foot())),
        ]

    def _blocked_text(self, name: str, raw: str) -> str:
        """The same card for a client that cannot take a rich message."""
        return (html.escape(self._blocked_head(name)) + "\n"
                + as_pre_lines(screenmod.tail_text(raw, 1500))
                + "\n<i>" + html.escape(self._blocked_foot()) + "</i>")

    async def _pulse(self, session_id: str, name: str, raw: str,
                     rt: SessionRuntime, blocked: bool) -> None:
        """Show that a long-running session is working, not stuck.

        Claude writes to the transcript in bursts, so a turn with heavy
        thinking sends nothing to the chat for minutes. From the outside that
        is indistinguishable from a dead bot — the terminal, meanwhile, is
        counting elapsed time and tokens out loud.
        """
        activity = "" if blocked else screenmod.read_activity(raw)
        if not activity:
            rt.work_since = 0.0
            await self._drop_pulse(rt)
            return
        now = time.time()
        if not rt.work_since:
            rt.work_since = now
            return
        if now - rt.work_since < _PULSE_AFTER:
            return
        text = f"⏳ <b>{html.escape(name)}</b>\n{html.escape(activity)}"
        if rt.pulse_msg_id is None:
            msg = await self._say_html(text, session_id=session_id)
            rt.pulse_msg_id = getattr(msg, "message_id", None)
            rt.pulse_edited = now
            return
        if now - rt.pulse_edited < _PULSE_EDIT:
            return
        rt.pulse_edited = now
        try:
            await self.bot.edit_message_text(
                text, chat_id=self.chat_id, message_id=rt.pulse_msg_id,
                parse_mode="HTML",
            )
        except Exception:
            # Too old to edit, or unchanged — neither is worth a word to the user.
            log.debug("pulse edit skipped", exc_info=True)

    async def _drop_pulse(self, rt: SessionRuntime) -> None:
        """Remove the heartbeat once the answer itself arrives."""
        if rt.pulse_msg_id is None:
            return
        msg_id, rt.pulse_msg_id = rt.pulse_msg_id, None
        rt.pulse_edited = 0.0
        try:
            await self.bot.delete_message(chat_id=self.chat_id, message_id=msg_id)
        except Exception:
            log.debug("pulse delete skipped", exc_info=True)

    async def _check_usage(self, name: str, rt: SessionRuntime, usage, status) -> None:
        """Notify when context or a rate limit crosses a step."""
        for key, pct, note in gauges(usage, status):
            steps = _CTX_STEPS if key == "ctx" else _LIMIT_STEPS
            reached = max([t for t in steps if pct >= t], default=0)
            previous = rt.alerted.get(key, 0)
            if reached > previous:
                log.info("usage alert %s=%d%% (step %d)", key, pct, reached)
                icon = "🔴" if reached >= 90 else ("🟠" if reached >= 75 else "🟡")
                await self._say(f"{icon} {name}: {note} — {pct}%")
            if reached != previous:
                rt.alerted[key] = reached

    def _dialog_blocks(self, name: str, session_id: str, dialog) -> tuple:
        """The question as rich blocks — nothing in here is parsed as markup.

        Labels and diagrams come off a terminal, where "<" is a "<" and not
        the start of a tag: with blocks there is no escaping step left to
        forget. The diagram is inline at any width now, because a preformatted
        block scrolls instead of wrapping.
        """
        head = [_DIALOG_ICON.get(dialog.kind, "❓ "), rich.bold(name)]
        title = _dialog_title(dialog)
        if title:
            head.append(f" · {title}")
        blocks: list[rich.Block] = [rich.para(*head)]
        if dialog.question:
            blocks.append(rich.para(dialog.question))
        if dialog.note:
            blocks.append(rich.para(dialog.note))
        # The slider's levels carry no description, so the buttons already
        # say everything a list of them would.
        for o in (dialog.options if dialog.kind != "effort" else []):
            mark = "▸ " if o.selected else ""
            box = "" if o.checked is None else ("☑ " if o.checked else "☐ ")
            # A digit is a shortcut in an ordinary question and a lie in the
            # settings dialogs: /effort has no digits at all, and in /model a
            # digit also saves the pick as the default.
            head_text = (f"{o.number}. {o.label}" if dialog.kind == "choice"
                         else o.label)
            if o.current:
                head_text = f"✅ {head_text}"
            blocks.append(rich.para(mark, box, rich.bold(head_text)))
            if o.description and o.description != o.label:
                blocks.append(rich.para(f"    {o.description}"))
        if dialog.kind in ("model", "effort"):
            # TRANSLATORS: the row of two buttons under a settings dialog.
            blocks.append(rich.para(
                _("A button applies the choice. The switch under the list "
                  "decides whether new sessions get it as their default too.")))
        if dialog.multi_select:
            # TRANSLATORS: how a multi-select question is answered from a chat.
            blocks.append(rich.para(
                _("The numbered buttons only tick boxes — nothing goes to "
                  "Claude until the button under them.")))
        if dialog.extras:
            # TRANSLATORS: options with no digit of their own, reachable only
            # by walking the list with the arrow buttons.
            blocks.append(rich.para(_("Via ⬆️⬇️✅: ") + ", ".join(dialog.extras)))
        if dialog.preview:
            sel = next((o.number for o in dialog.options if o.selected), None)
            blocks.append(rich.para(
                _("🖼 Diagram for option {n}:").format(n=sel) if sel
                else _("🖼 Diagram:")))
            blocks.append(rich.pre(rich.ascii_frame(dialog.preview)))
        return blocks, dialog_kb(session_id, dialog)

    def _render_dialog(self, name: str, session_id: str, dialog):
        head = f"{_DIALOG_ICON.get(dialog.kind, '❓ ')}<b>{html.escape(name)}</b>"
        title = _dialog_title(dialog)
        if title:
            head += f" · {html.escape(title)}"
        parts = [head, ""]
        if dialog.question:
            parts += [html.escape(dialog.question), ""]
        if dialog.note:
            parts += [html.escape(dialog.note), ""]
        for o in (dialog.options if dialog.kind != "effort" else []):
            mark = "▸ " if o.selected else ""
            box = "" if o.checked is None else ("☑ " if o.checked else "☐ ")
            head_text = (f"{o.number}. {o.label}" if dialog.kind == "choice"
                         else o.label)
            if o.current:
                head_text = f"✅ {head_text}"
            parts.append(f"{mark}{box}<b>{html.escape(head_text)}</b>")
            if o.description and o.description != o.label:
                parts.append(f"    {html.escape(o.description)}")
        if dialog.kind in ("model", "effort"):
            parts.append("")
            # TRANSLATORS: the row of two buttons under a settings dialog.
            parts.append(_("A button applies the choice. The switch under the "
                           "list decides whether new sessions get it as their "
                           "default too."))
        if dialog.multi_select:
            parts.append("")
            # TRANSLATORS: how a multi-select question is answered from a chat.
            parts.append(_("The numbered buttons only tick boxes — nothing "
                           "goes to Claude until the button under them."))
        if dialog.extras:
            parts.append("")
            # TRANSLATORS: options with no digit of their own, reachable only
            # by walking the list with the arrow buttons.
            parts.append(_("Via ⬆️⬇️✅: ")
                         + ", ".join(html.escape(e) for e in dialog.extras))
        if dialog.preview:
            sel = next((o.number for o in dialog.options if o.selected), None)
            caption = (_("🖼 Diagram for option {n}:").format(n=sel) if sel
                       else _("🖼 Diagram:"))
            if render.max_line_width(dialog.preview) <= _PHONE_COLS:
                parts += ["", caption, as_pre_lines(dialog.preview)]
            else:
                parts += ["", _("🖼 The diagram is wider than the screen — "
                                "sent as a picture")]
        text = "\n".join(parts)
        if len(text) > 3900:
            text = text[:3900] + "\n…"
        return text, dialog_kb(session_id, dialog)

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

    async def send_preview(self, session_id: str, dialog) -> None:
        """The highlighted option's drawing, on demand from the 🖼 button.

        Text first: a scrollable block keeps the alignment and can be copied.
        The PNG stays underneath for a client that has no rich messages.
        """
        if not dialog.preview:
            return
        sel = next((o.number for o in dialog.options if o.selected), None)
        caption = (_("🖼 Diagram for option {n}").format(n=sel) if sel
                   else _("🖼 Diagram"))
        if await self._say_rich(session_id, blocks=[
                rich.para(caption),
                rich.pre(rich.ascii_frame(dialog.preview))]):
            return
        if not render.available():
            return
        out = Path(tempfile.gettempdir()) / f"ccbot-preview-{session_id[:8]}.png"
        try:
            render.text_to_png(dialog.preview, out)
        except Exception:
            log.exception("preview render failed")
            return
        try:
            await self.bot.send_photo(self.chat_id, FSInputFile(out), caption=caption)
        except Exception:
            log.exception("send_photo failed")

    async def send_screen(self, window_id: str, name: str,
                          session_id: str | None = None) -> None:
        raw = await tmux.capture(window_id)
        if await self._say_rich(session_id, blocks=[
                rich.para("🖥 ", rich.bold(name)),
                rich.pre(rich.ascii_frame(screenmod.tail_text(raw, rich.PRE_LIMIT))),
        ]):
            return
        await self._say(f"🖥 {name}\n" + as_pre(screenmod.tail_text(raw, 3200)),
                        session_id=session_id, parse_mode="HTML")
