"""Telegram front-end: a dispatcher for Claude Code sessions held by the session host."""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import os
import secrets
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
    ReplyParameters,
    Update,
)

from . import (
    account,
    authlogin,
    logsetup,
    media,
    rich,
    service,
    status_feed,
    transcript,
    updates,
    util,
)
from . import sessions as sess
from .authlogin import AuthLogin, LoginError
from .authlogin import status as auth_status
from .bridge import SdkBridge
from .bridge import _plain as plain_text
from .config import Config
from .hostlink import HostError
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
    ACTIVE_ICON,
    HISTORY_PAGE,
    STATUS_ICON,
    auth_link_kb,
    auth_retry_kb,
    cancel_kb,
    cancel_rename_kb,
    choice_kb,
    closed_kb,
    dirs_kb,
    done_kb,
    held_kb,
    held_wait_kb,
    history_dirs_kb,
    history_kb,
    lang_kb,
    project_dirs_kb,
    recap_kb,
    reroute_kb,
    restart_confirm_kb,
    route_kb,
    route_pick_kb,
    sdk_effort_kb,
    sdk_models_kb,
    search_kb,
    service_kb,
    session_kb,
    sessions_kb,
    update_kb,
)
from .settings import Settings
from .state import Managed, Store
from .transport import TolerantSession
from .util import account_line, as_pre, split_text, usage_report
from .watcher import Watcher

log = logging.getLogger("ccbot.bot")

# Commands the bot handles itself; anything else starting with "/" is forwarded
# to Claude, so /model, /compact, /cost and friends keep working.
OWN_COMMANDS = {"start", "help", "sessions", "menu", "new", "exit", "screen",
                "esc", "usage", "clear", "log", "dirs", "rename", "service",
                "restart", "lang", "update", "login"}

# Telegram's Bot API refuses to serve files larger than this.
_MAX_ATTACHMENT = 20 * 1024 * 1024
# How long the inbox waits for the rest of a burst before sending it on.
# Telegram hands a whole forwarded batch over in a single poll — thirteen
# messages landed within 190 ms on 2026-08-27 — and an album is no slower, so
# a short window catches both while costing a lone message almost nothing.
# Pauses before re-sending a delivery confirmation the network dropped.
_ACK_RETRIES = (10.0, 30.0, 90.0)
_INBOX_QUIET = 0.8
# How often the collector looks at a batch it is holding.
_INBOX_TICK = 0.2
# How much of the changelog one message may carry. Below the rich-message
# document cap, with room for the heading and the "and N older" tail.
_NEWS_LIMIT = 24000
# How much of a name a card's heading may take before it is trimmed; the rest
# of it still reaches the reader, on the "it started with" line.
_CARD_HEAD = 80
# How many directories the "new session" list may offer. Configured roots
# come first and are always kept, so a low cap makes each added root push a
# real project off the tail: with seven roots a limit of ten left three
# slots, and 7loc-client-web fell out of the list a tap could reach.
_DIR_CHOICES = 14
# A half-finished prompt ("send me a path") expires rather than lingering.
_PENDING_TTL = 180.0
# How long a forwarded batch waits for someone to say where it goes. It has to
# outlast a phone call, but not a night: a message typed tomorrow belongs to
# tomorrow's conversation, not to yesterday's forward.
_PARKED_TTL = 3600.0
# How long text that goes to a busy session by default (not as a reply) waits
# in the bot with «↩️ Cancel». The CLI takes a prompt sent mid-turn into the
# turn as soon as the current tool call ends — half the time within 5 s —
# which is too soon to notice it went to the wrong session (2026-11-25).
_DELAY = 10.0
# More typing pushes a delayed batch back, but never past this from its start.
_DELAY_CAP = 30.0
_DELAY_TICK = 0.5
# Where an attachment lands while no session owns it yet — a forwarded batch
# is downloaded before the chat has said which session it is for.
_INBOX_DIR = "inbox"

# Text held back because the session had a question open: how often the
# "send after the answer" wait looks, and how long the session has to stand
# without a question before it sends (a question answered from its card may
# be followed by the next one at once).
_HELD_TICK = 1.0
_HELD_CLEAR = 2.5


class Refused(Exception):
    """The session has a question open, so the text was not sent."""


_MODE_LABELS = {"auto": "auto", "manual": "manual", "acceptEdits": "accept edits",
                "plan": "plan"}


@dataclass
class _Restart:
    """What came of relaunching one session's `claude`."""
    ok: bool
    reason: str = ""     # why not, phrased for the chat
    version: str = ""    # the build it came back as, when it could be read


def restart_ask() -> str:
    return _(
        "🔄 <b>Restart the bot?</b>\n\n"
        "Claude's sessions are unaffected: they live in the session host, a "
        "service of their own. Once the bot is back, it picks up the host's "
        "events where it left off.\n\n"
        "Only what is in the process's memory is lost: a half-finished request "
        "(\u00absend me a path\u00bb, \u00absend me a name\u00bb) and "
        "attachments waiting for a caption.\n\n"
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
/menu — the controls of one session, without the list in the
   way: 🧠 model, ◉ effort, 🔐 permission mode, 🔁 restart,
   ✏️ rename, 🚪 end. Without a reply it is the active session;
   as a reply to a session's message, that one (which then
   becomes active).
/new — start a session (pick a directory)

<b>Where things go</b>
Without a reply, everything — text and commands — goes to the
<b>active</b> session. Replying to a session's message goes to
<b>that</b> session and makes it active. The commands below
follow the same rule.

<b>Acting on a session</b>
/usage — how much of the 5-hour and 7-day quota is spent, and
   how full this session's context is. Taken from what the
   session last reported, so it is instant, costs no tokens and
   does not disturb the session. Not to be confused with
   <code>/context</code>: that is Claude Code's own breakdown of
   which files, tools and messages the context is spent on.
   «🗜 /compact» on the card sums the conversation up and carries
   on with the summary instead of the history: the session, its
   name and its id all stay.
   «🔁 Restart» next to it relaunches the session with its
   context.
/screen — where the session stopped: its last request and answer
/esc — <b>interrupt what Claude is doing</b>.
   The session stays alive and its context is untouched.
/clear — clear the session's context. The session lives on,
   its history is gone.
/rename <i>name</i> — your own name instead of <code>project-1a2b</code>.
   Sent bare, from the "/" menu, it asks for the name and takes
   your next message as it. (<code>/rename -</code> gives the
   automatic one back.) Until you name it, the list shows the
   topic Claude came up with itself.
/exit — <b>end the session</b>: Claude shuts down. The transcript
   stays — the session can be brought back from 🕘 in the list.

Any other <code>/command</code> (<code>/model</code>, <code>/compact</code>,
<code>/cost</code>…) is passed to Claude as it is.

<b>The bot itself</b>
/update — which Claude Code each session runs, which one is
   on disk, and a restart that keeps the context: Claude
   updates itself in the background, but a running session
   stays on the build it started with.
/login — log Claude in to another account (when the limits of
   this one run out). The sign-in link comes here, the code from
   the page goes back as a message. The other sessions switch to
   the new account with their next request, without a restart.
/service — uptime, code version, session host state, restart button
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

↪️ <b>Forwarded</b> messages ask where they should go, instead of
going to the active session: «▶️ Current», «📋 Another session»
(the running sessions, ➕ a new one, 🕘 a closed one to bring
back) or «🗑 Discard». Anything you send while the question is
open joins the same batch, so the words that explain a forward
travel with it. Forwarding as a reply to a session's message
needs no question — the reply has already said where.

When Claude asks a question, buttons with the options arrive.""")


# The order the states are explained in: from the ones that want something
# from you down to the ones that are merely busy or free.
_LEGEND_ORDER = ("waiting", "busy", "idle", "starting")


def _mode_label(mode: str) -> str:
    """A permission mode the way the session card names it."""
    return _MODE_LABELS.get(mode, mode or "?")


def _status_legend(views: list[sess.SessionView],
                   active: str | None) -> list[str]:
    """One line per icon actually present in the list, and nothing more.

    The active session wears 🟢 instead of its own status icon, so it is not
    what puts a state into the legend.
    """
    seen = {v.status for v in views if v.session_id != active}
    return [f"{STATUS_ICON[s]} {sess.status_label(s)}"
            for s in _LEGEND_ORDER if s in seen and s in STATUS_ICON]


def _account_lines(views: list[sess.SessionView]) -> list[str]:
    """Which account each listed session runs under, grouped by account.

    A session moves to a new login only with its next request, so one that
    has been quiet since still names the old account — a list that mixes two
    is exactly the case worth showing.
    """
    groups: dict[str, tuple[str, list[str]]] = {}
    for v in views:
        if v.account:
            groups.setdefault(v.account.lower(), (v.account, []))[1].append(v.name)
    if not groups:
        return []
    live = len(views)
    if len(groups) == 1:
        email, names = next(iter(groups.values()))
        if len(names) == live:
            return [account_line(email)]
    out = []
    for email, names in groups.values():
        shown = ", ".join(html.escape(n) for n in names[:4])
        if len(names) > 4:
            shown += ", …"
        out.append(f"👤 {html.escape(email)}: {shown}")
    return out


def _sessions_text(managed: list[sess.SessionView],
                   active: str | None, behind: int = 0) -> str:
    """The list's caption: a legend for exactly the icons on the screen."""
    if managed:
        waiting = [v for v in managed if v.status == "waiting"]
        head = ngettext("📋 <b>Sessions</b> — {count} running",
                        "📋 <b>Sessions</b> — {count} running",
                        len(managed)).format(count=len(managed))
        if waiting:
            head += ngettext(", {count} waiting for you",
                             ", {count} waiting for you",
                             len(waiting)).format(count=len(waiting))
        block = []
        cur = next((v for v in managed if v.session_id == active), None)
        if cur:
            block.append(_("🟢 active: <b>{name}</b> — plain text goes here"
                           ).format(name=html.escape(cur.name)))
        block += _status_legend(managed, active)
        block += _account_lines(managed)
        if behind:
            block.append(ngettext(
                "⬆️ {count} session runs an older Claude Code — /update",
                "⬆️ {count} sessions run an older Claude Code — /update",
                behind).format(count=behind))
        parts = [head] + (["\n".join(block)] if block else [])
    else:
        parts = [_("📋 <b>Sessions</b>\n\nNo session is running yet — "
                   "start one with {button}.").format(
                       button=_("➕ New session"))]
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


def _host_refusal(exc: HostError) -> str:
    """Why the session host would not start a session, for the chat."""
    if exc.code == "offline":
        return _("❌ The session host is not answering, so the session cannot "
                 "start. It restarts by itself — try again in a minute.")
    detail = html.escape((exc.message or exc.code)[:500])
    return _("❌ The session host could not start the session:\n<code>{detail}</code>"
             ).format(detail=detail)


def _picker_failed() -> str:
    return _("The session host did not answer — try again in a moment.")


def _close_failed(name: str) -> str:
    return _("<b>❌ {name} was not ended</b>\n\n"
             "The session host is not answering. The session may still be "
             "running — try again in a minute.").format(
                 name=html.escape(name))


def _stale_card(command: str) -> str:
    """Telegram forgets a card after 48 hours; say so instead of failing."""
    return _("This card is too old — send {command} again").format(command=command)


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
    if m.live_photo:
        # The still frame, not the clip: Claude sees a picture, and what the
        # person pointed the camera at is in it either way.
        lp = m.live_photo
        return (_att(lp.photo[-1], "image/jpeg", ".jpg") if lp.photo
                else _att(lp, "video/mp4", ".mp4"))
    return None


# What a chat reports about itself rather than something the person sent:
# pinning a card must not be answered with "I cannot read this".
_SERVICE_KINDS = frozenset({
    "pinned_message", "message_auto_delete_timer_changed",
    "chat_background_set", "write_access_allowed", "new_chat_members",
    "left_chat_member", "new_chat_title", "new_chat_photo",
    "delete_chat_photo", "group_chat_created", "supergroup_chat_created",
    "channel_chat_created", "migrate_to_chat_id", "migrate_from_chat_id",
    "gift", "unique_gift", "gift_upgrade_sent", "paid_message_price_changed",
    "direct_message_price_changed", "users_shared", "chat_shared",
    "boost_added", "chat_owner_changed", "chat_owner_left",
    "forum_topic_created", "forum_topic_edited", "forum_topic_closed",
    "forum_topic_reopened", "general_forum_topic_hidden",
    "general_forum_topic_unhidden", "video_chat_scheduled",
    "video_chat_started", "video_chat_ended",
    "video_chat_participants_invited", "proximity_alert_triggered",
    "successful_payment", "refunded_payment", "giveaway_created",
    "giveaway_completed", "giveaway_winners", "checklist_tasks_done",
    "checklist_tasks_added", "suggested_post_approved",
    "suggested_post_approval_failed", "suggested_post_declined",
    "suggested_post_paid", "suggested_post_refunded", "managed_bot_created",
    "connected_website", "community_chat_added", "community_chat_removed",
    "poll_option_added", "poll_option_deleted",
})




@dataclass
class _Item:
    """One queued message: what it said, what it brought, who wrote it."""

    message: Message
    text: str = ""
    # A rich message carries its pictures among the paragraphs, so one
    # message may bring several files.
    paths: list[Path] = field(default_factory=list)
    sender: str = ""


@dataclass
class _Parked:
    """A batch held back until the chat says which session it belongs to.

    Forwarding a conversation is how a session is given the context it should
    start from, so its destination is a question — and everything sent while
    that question is open joins the same batch, because the comment that
    explains a forward ("look at this") is part of it. A batch sent while no
    session is active is held the same way: there is nothing to send it to
    yet, and the session it is meant for is usually about to be started.
    """

    items: list[_Item] = field(default_factory=list)
    # The question in the chat. It is deleted and asked again at the bottom
    # when the batch grows: a card scrolled above the new messages is a card
    # that gets answered for the wrong batch.
    ask_id: int | None = None
    at: float = 0.0
    # Set by "new session" and by "recent": whichever session `_create` brings
    # up next is the destination.
    await_new: bool = False

    @property
    def forwarded(self) -> bool:
        """Whether any of it came from elsewhere — it decides the wording."""
        return any(i.sender for i in self.items)


@dataclass
class _Held:
    """Text that reached a session while it had a question open.

    Pasted, it would be swallowed by the dialog and its Enter would pick
    whatever option the cursor was on — the message lost and an answer given
    that nobody chose (2026-10-20, twice in one afternoon). The destination
    is known; what is not known is whether the text answers the question, so
    the chat is asked. One per session. A batch is one prompt; a Claude
    command always travels as a batch of its own.
    """

    session_id: str
    chat_id: int
    batches: list[list[_Item]] = field(default_factory=list)
    ask_id: int | None = None
    at: float = 0.0
    # "Send after the answer" was chosen: `_held_loop` delivers it once the
    # session stands without a question.
    waiting: bool = False
    # Which question the card was drawn for. "This answers the question"
    # types into the free-text row only if it is still the same one — the
    # next section of a multi-part question is not what the text was about.
    question_sig: str = ""
    task: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def count(self) -> int:
        return sum(len(b) for b in self.batches)


@dataclass
class _Outgoing:
    """A batch on its way to a session that the chat can still take back.

    *delayed*: held in the bot for `_DELAY` (the session is busy and the text
    was not a reply); *sending*: in the hands of the host; *queued*: in the
    CLI, not yet taken into a turn (`cancel_async_message` can still drop
    it); *cancelling*: a tap is being answered. A record leaves the registry
    once the batch is taken in, cancelled or failed.
    """

    tok: str
    chat_id: int
    session_id: str
    items: list[_Item]
    state: str = "delayed"
    note: str = ""
    uuid: str = ""
    ack: Message | None = None
    first: float = 0.0
    deadline: float = 0.0
    task: asyncio.Task | None = None


def _is_command(items: list[_Item]) -> bool:
    """Whether a batch is a lone Claude command such as "/compact".

    A path is not a command, though it starts the same way: "/home/x.log —
    look" is text.
    """
    if len(items) != 1:
        return False
    head = items[0].text.split(maxsplit=1)[0] if items[0].text.strip() else ""
    return len(head) > 1 and head.startswith("/") and "/" not in head[1:]


def _switch_note(mgd, previous=None) -> str:
    """Where plain text goes now: that on a bold line of its own, the rest
    after an empty one — run together, the session's name was lost in the
    sentence (2026-11-25)."""
    head = _("<b>🟢 Text now goes to {name}</b>").format(name=html.escape(mgd.full_label))
    if previous is None:
        return head
    return head + "\n\n" + _(
        "{previous} keeps working — to write to it, reply to one of its "
        "messages.").format(previous=html.escape(previous.full_label))


def _not_sent_text(count: int) -> str:
    """A batch whose session ended before it could go.

    One message has its own wording; every other count carries the number,
    since Ukrainian's first plural form also serves 21, 31…
    """
    if count == 1:
        return _("🗑 The session has ended — the message was not sent.")
    return ngettext("🗑 The session has ended — {count} message was not sent.",
                    "🗑 The session has ended — {count} messages were not sent.",
                    count).format(count=count)


def _batch_text(items: list[_Item]) -> str:
    """The one prompt a batch becomes."""
    return media.build_batch_prompt(
        [media.Piece(text=i.text, paths=tuple(i.paths), sender=i.sender)
         for i in items])


@dataclass
class _Inbox:
    """One chat's messages waiting to leave as a single prompt.

    Telegram delivers a forwarded batch in one poll and aiogram runs an update
    per task, so a forwarded conversation used to become one prompt per
    message, in whatever order the event loop scheduled them, each with its
    own send — and each racing the others into the session. They are
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
        session = TolerantSession()
        session.on_damaged = self._on_damaged
        self.bot = Bot(token=cfg.token, session=session)
        self.dp = Dispatcher()
        self.store = Store()
        self.settings = Settings()
        install()
        self.watcher = Watcher(self.bot, self.store, self.settings, cfg.owner)
        # The sessions: held by the session host, followed through its events.
        self.bridge = SdkBridge(self.bot, self.store, self.watcher, cfg.owner)
        # /login: `claude auth login` under a pty.
        self.auth = AuthLogin()
        sess.sdk_state = self.bridge.state
        status_feed.sdk_usage = self.bridge.usage_for
        # chat_id -> ("dir",) | ("rename", session_id) | ("adddir",)
        #          | ("hfind",) | ("sdktext", rid8, act, session_id)
        self.pending: dict[int, tuple] = {}
        # The messages that asked for the pending input: chat_id ->
        # [(message_id, role)], role being "picker", "prompt" or "question".
        # Once the input has done its job each one says what was chosen
        # instead of offering the choice again — see `_retire_cards`.
        self.prompt_cards: dict[int, list[tuple[int, str]]] = {}
        # Incoming messages wait here for the rest of their burst, so a
        # forwarded conversation, an album and the sentence that explains it
        # reach Claude as one prompt.
        self.inbox: dict[int, _Inbox] = {}
        # Forwarded batches waiting for a destination, one per chat.
        self.parked: dict[int, _Parked] = {}
        # Text kept out of a session that has a question open, per session.
        self.held: dict[str, _Held] = {}
        # Sent text that can still be taken back («↩️ Cancel»), by token.
        self.outgoing: dict[str, _Outgoing] = {}
        # Batches taken back, kept for «📋 To another session»: token ->
        # (chat, items, when).
        self.cancelled: dict[str, tuple[int, list[_Item], float]] = {}
        # Tokens already handed to a picker: a second tap is not "lost".
        self.rerouted: set[str] = set()
        self.bridge.on_taken = self._on_taken
        # Sessions a restart is under way in: a second press must not ask
        # the host for a second one.
        self.restarting: set[str] = set()
        # Notices sent from outside a handler (an expired batch, the question
        # re-asked after a launch): kept only so the task is not
        # garbage-collected while it is still speaking.
        self.expiries: set[asyncio.Task] = set()
        self.dir_choices: list[str] = []
        # The last history search per chat: a query does not fit in 64 bytes
        # of callback_data, so paging through the results needs it kept here.
        self.searches: dict[int, str] = {}
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

        @dp.message(Command("menu"))
        async def _menu(m: Message):
            # The session card without the list in front of it: model, effort,
            # mode and the rest used to be reachable only by walking through
            # /sessions and tapping a row.
            if not self._ok(m):
                return
            mgd, note = await self._target(m)
            if mgd:
                await self._send_session_card(m, mgd, note)

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
                await self._send_recap(m.chat.id, mgd)

        @dp.message(Command("esc"))
        async def _esc(m: Message):
            if not self._ok(m):
                return
            mgd, note = await self._target(m)
            if mgd:
                await self._interrupt_sdk(m, mgd, note)

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
                           reply_markup=self._service_kb())

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
                if not await self._send_command(mgd, "/clear"):
                    await self._ack(m, _("It is waiting for an answer — deal "
                                         "with that first"), mgd)
                    return
                await self._ack(m, _("🧹 /clear sent to <b>{name}</b>{note}").format(
                    name=html.escape(mgd.full_label), note=note), mgd)

        @dp.message(Command("login"))
        async def _login(m: Message):
            if not self._ok(m):
                return
            mgd, _note = await self._target(m)
            if mgd:
                await self._start_auth_login(m, mgd)

        @dp.message(Command("usage"))
        async def _usage(m: Message):
            if not self._ok(m):
                return
            mgd, _note = await self._target(m)
            if mgd:
                await self._ack(
                    m,
                    usage_report(mgd.full_label, status_feed.read(mgd.session_id),
                                 await self._account_of(mgd)),
                    mgd,
                )

        @dp.message(Command("exit"))
        async def _exit(m: Message):
            if not self._ok(m):
                return
            mgd, _note = await self._target(m)
            if mgd:
                note = await m.answer(
                    _("⏳ Ending {name}…").format(name=mgd.full_label))
                if not await self._close(mgd.session_id):
                    await note.edit_text(_close_failed(mgd.full_label), parse_mode="HTML")
                    return
                await note.edit_text(
                    _("<b>❌ Session {name} has ended</b>\n\n"
                      "The transcript stays — it can be brought back from 🕘 in "
                      "the list.").format(name=html.escape(mgd.full_label)),
                    parse_mode="HTML",
                )
                await self._show_sessions(m)

        # Every kind of attachment, not a chosen few: an unhandled kind falls
        # through to no handler at all, and a voice message inside a forwarded
        # conversation would silently disappear from the batch.
        @dp.message(F_.photo | F_.document | F_.video | F_.audio | F_.voice
                    | F_.video_note | F_.animation | F_.sticker | F_.live_photo)
        async def _media(m: Message):
            if self._ok(m):
                att = _attachment(m)
                await self._on_media(m, m.caption or "", [att] if att else [])

        @dp.message(F_.text)
        async def _text(m: Message):
            if self._ok(m):
                await self._on_text(m)

        # A Premium client sends formatted text this way: no `text` at all,
        # the words and the pictures are blocks (2026-10-20, lost in silence).
        @dp.message(F_.rich_message)
        async def _rich(m: Message):
            if self._ok(m):
                await self._on_rich(m)

        # Whatever kind Telegram adds next must not vanish: it is answered,
        # and the log names its content type.
        @dp.message()
        async def _other(m: Message):
            if not self._ok(m):
                return
            if logsetup.content_kind(m) in _SERVICE_KINDS:
                log.info("service message id=%s ignored: %s",
                         m.message_id, m.content_type)
                return
            await self._unreadable(m)

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
        """The session a button's `sid8` points at, after /clear included.

        A card keeps the id it was printed with, and `/clear` hands the
        session a new one — so every button on a card older than the last
        clear used to answer "that session is gone". `Store.find_prefix`
        falls back to the ids the session has left behind.
        """
        return self.store.find_prefix(short)

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

    async def _ack(self, m: Message, text: str, mgd,
                   reply_markup: InlineKeyboardMarkup | None = None,
                   edit: Message | None = None) -> Message | None:
        """Confirm an action, and let a reply to that confirmation land back.

        The last message in the chat is the one a phone replies to fastest, so
        the bot's own acknowledgements route like the session's output does.
        *edit* turns an earlier notice (a delayed batch's) into this one.
        """
        msg: Message | None = None
        try:
            if edit is not None:
                with contextlib.suppress(TelegramBadRequest):
                    done = await edit.edit_text(text, parse_mode="HTML",
                                                reply_markup=reply_markup)
                    msg = done if isinstance(done, Message) else edit
            if msg is None:
                msg = await m.answer(text, parse_mode="HTML", reply_markup=reply_markup)
        except TelegramBadRequest:
            log.exception("ack failed: %r", text[:120])
            return None
        log.info("out msg id=%s -> %s: %r",
                 getattr(msg, "message_id", "—"), mgd.session_id[:8], text[:120])
        if msg:
            self.store.remember_message(msg.message_id, mgd.session_id)
        return msg

    def _service_kb(self) -> InlineKeyboardMarkup:
        return service_kb(service.under_systemd())

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
            behind = sum(1 for m in managed if self._stale(m, disk))
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
        lines.append(_("📋 Sessions: {count}").format(count=len(managed)))
        lines.append(_("📓 Journal: <code>{path}</code> — /log").format(
            path=logsetup.LOG_PATH))
        lines += ["", await self._host_lines()]
        return "\n".join(lines)

    async def _host_lines(self) -> str:
        """The session host, for /service."""
        try:
            st = await self.bridge.link.call("status", timeout=5, wait_online=0.5)
        except HostError:
            return _("🛰 Session host: not answering — the sessions cannot be "
                     "reached until it is back")
        up = service.human_delta(time.time() - float(st.get("started") or time.time()))
        return _("🛰 Session host: PID {pid}, up for {duration} · SDK {sdk} · "
                 "live sessions: {live}").format(
                     pid=st.get("pid"), duration=up,
                     sdk=html.escape(str(st.get("sdk"))), live=st.get("live"))

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

    def _hold_card(self, chat_id: int, msg: Message | None,
                   role: str) -> None:
        """Remember a message that belongs to the pending input."""
        if msg is not None:
            self.prompt_cards.setdefault(chat_id, []).append(
                (msg.message_id, role))

    async def _retire_cards(self, chat_id: int, summary: str = "",
                            mark: str = "") -> None:
        """The pending input has been used: record the choice where it was.

        A picker becomes the one line *summary* (a list of fourteen
        directories after one was picked is only a second tap waiting to
        happen, and deleting it leaves the chat with no trace of the choice);
        a question keeps its text and trades its buttons for *mark*; the
        "send me a path" prompt goes — the summary above it says it all.
        """
        for msg_id, role in self.prompt_cards.pop(chat_id, []):
            with contextlib.suppress(Exception):
                if role == "picker":
                    await self.bot.edit_message_text(
                        summary, chat_id=chat_id, message_id=msg_id,
                        parse_mode="HTML")
                elif role == "prompt":
                    await self.bot.delete_message(chat_id, msg_id)
                else:
                    await self.bot.edit_message_reply_markup(
                        chat_id=chat_id, message_id=msg_id,
                        reply_markup=done_kb(mark))

    async def _settle(self, msg: Message, summary: str,
                      session_id: str | None = None,
                      reply_markup: InlineKeyboardMarkup | None = None) -> None:
        """Turn a picker into the line saying what was picked on it.

        A line that names the session it launched belongs to that session: a
        reply to "▶️ Resuming …" is meant for the session it resumed, and an
        unregistered line sent it to whichever was active (2026-10-03).
        """
        with contextlib.suppress(Exception):
            await msg.edit_text(summary, parse_mode="HTML", reply_markup=reply_markup)
        if session_id:
            self.store.remember_message(msg.message_id, session_id)

    @staticmethod
    async def _mark(msg: Message, label: str) -> None:
        """Trade a question card's buttons for one saying how it was answered."""
        with contextlib.suppress(Exception):
            await msg.edit_reply_markup(reply_markup=done_kb(label))

    @staticmethod
    def _dir_picked(path: str) -> str:
        return _("📁 New session in <code>{path}</code>").format(
            path=html.escape(str(Path(path).expanduser())))

    def _ask_name(self, chat_id: int, mgd) -> str:
        """Arm "the next message is the name" and word the question.

        Shared by the ✏️ button and by a bare `/rename`: picking the command
        out of the "/" menu and typing the name after it is the whole point —
        nobody wants to type `/rename` by hand to get the name on the same
        line.
        """
        self.pending[chat_id] = ("rename", mgd.session_id, time.time())
        self.prompt_cards.pop(chat_id, None)
        return _("<b>✏️ What should {name} be called?</b>\n\n"
                 "Send the name in your next message (up to {limit} "
                 "characters).\n<code>-</code> gives the automatic name "
                 "back.").format(name=html.escape(mgd.full_label),
                                 limit=util.NAME_LIMIT)

    async def _rename_session(self, m: Message, mgd, raw: str, note: str = "") -> None:
        """Give a session a name a human can recognise in a list."""
        auto = util.default_name(mgd.cwd, mgd.session_id)
        wanted = raw.strip()
        # "-" is the documented way back to the automatic name; the word is
        # accepted too, in whatever language the interface is speaking.
        reset_words = {"-", "auto", _("auto")}
        name = auto if wanted.lower() in reset_words else util.clean_name(wanted)
        if not name:
            # Either the command came in bare — the usual case, straight from
            # the "/" menu — or what arrived was nothing but unprintables.
            head = ""
            if wanted:
                head = _("❌ There is nothing left of that name once "
                         "unprintable characters are dropped.") + "\n\n"
            ask = await m.answer(head + self._ask_name(m.chat.id, mgd),
                                 parse_mode="HTML",
                                 reply_markup=cancel_rename_kb(mgd.session_id))
            self._hold_card(m.chat.id, ask, "question")
            return
        # Only the display name changes: `name` stays the one Claude was
        # launched with, which is how the session is found again after /clear.
        self.store.set_custom_name(mgd.session_id, None if name == auto else name)
        # The question is answered: its «Cancel» would now only mislead.
        await self._retire_cards(m.chat.id, mark=f"✏️ {name}")
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
        here = _editable(target)
        if here is None:
            await target.answer(_stale_card("/sessions"), show_alert=True)
            return
        await account.fill(managed)
        active = self.store.get_active(here.chat.id)
        disk = await updates.installed()
        behind = sum(1 for m in self.store.all_managed() if self._stale(m, disk))
        text = _sessions_text(managed, active, behind)
        kb = sessions_kb(managed, active)
        if isinstance(target, CallbackQuery):
            await self._safe_edit(here, text, reply_markup=kb, parse_mode="HTML")
            # It may have been a session's card a moment ago; as the list it
            # belongs to none, and a reply follows the active session.
            self.store.forget_message(here.message_id)
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
            # The one directory that can never be on the list, and the one
            # whose sessions used to vanish because of it.
            head += "\n\n" + _(
                "<code>~</code> counts as well: the home directory cannot be a "
                "root — it would take in every project below it — but sessions "
                "started there are offered and kept in 🕘 history.")
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
        self.dir_choices = await sess.recent_dirs(self.store, _DIR_CHOICES, roots=self.roots)
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
    async def _create(self, chat_id: int, cwd: str,
                      resume: str | None = None,
                      title: str | None = None) -> str | None:
        """Start (or resume) a session in *cwd* through the host; the new id.

        The id is returned because a forwarded batch may be waiting for
        exactly this session — see `_deliver_after_launch`, which is called
        from here so that both routes into a new session ("new" and a resume
        from the history list) deliver it. *title* is what the chat called
        the session where it was picked; see `_launch_card` for why it has to
        be in place before the card goes out.

        The row is written only once the host has the session up: a launch
        that fails leaves nothing behind that looks like a session.
        """
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
            return None
        session_id = resume or str(uuid.uuid4())
        name = util.default_name(cwd, session_id)
        previous = self.store.get_active(chat_id)
        # The launch name is the host's key for the session, and it can repeat:
        # a transcript resumed after /clear computes the name its row still
        # carries under the new id. Another session's name gets a suffix.
        base, n = name, 1
        while True:
            taken = self.store.by_name(name)
            if taken is None or taken.session_id == session_id:
                probe = Managed(session_id=session_id, cwd=cwd, name=name,
                                created_at=time.time(), title=None)
                try:
                    await self.bridge.create(probe, resume=bool(resume))
                    break
                except HostError as exc:
                    if exc.code != "exists" or n >= 9:
                        log.warning("create failed name=%s: %s", name, exc)
                        await self.bot.send_message(chat_id, _host_refusal(exc),
                                                    parse_mode="HTML")
                        return None
            n += 1
            name = f"{base}-{n}"
        log.info("session created id=%s name=%s cwd=%s resume=%s",
                 session_id[:8], name, cwd, bool(resume))
        self.store.add(session_id, cwd, name)
        if title:
            self.store.set_title(session_id, title)
        self.store.set_active(chat_id, session_id)
        await self._launch_card(chat_id, session_id, previous, resumed=bool(resume))
        # "Send the forwarded messages to a new session" ends here: the
        # session exists now, so the batch that was waiting for one goes in.
        await self._deliver_after_launch(chat_id, session_id)
        return session_id

    async def _launch_card(self, chat_id: int, session_id: str,
                           previous: str | None, resumed: bool) -> None:
        """Answer a launch with the session's own card, the one /sessions opens.

        A bare "✅ Resumed: 7loc-7487" was all a resume used to get: the
        launch name rather than the one the session was picked by, no
        controls, and not a word about where text goes now. On 2026-10-03 a
        session found by a search was resumed that way, the user went on
        answering another session's question cards, and two messages meant
        for the found session went to the other one. So a launch ends the way
        tapping a row of /sessions does — the card, plus who has the text now
        and who keeps working — registered, so that a reply to it lands in
        the session it names.
        """
        mgd = self.store.get(session_id)
        if mgd is None:
            return
        prev = self.store.get(previous) if previous and previous != session_id \
            else None
        tail = [_switch_note(mgd, prev)]
        note = " · " + (_("resumed") if resumed else _("new"))
        card = await self._session_card(mgd, note, chat_id=chat_id)
        if card is None:
            head = (_("✅ Resumed: <b>{name}</b>") if resumed
                    else _("✅ Created: <b>{name}</b>")).format(
                        name=html.escape(mgd.full_label))
            text, kb = f"{head}\n<code>{html.escape(mgd.cwd)}</code>", None
        else:
            text, kb = card
        msg = await self.bot.send_message(
            chat_id, text + "\n\n" + "\n".join(tail),
            parse_mode="HTML", reply_markup=kb)
        self.store.remember_message(msg.message_id, session_id)

    async def _close(self, session_id: str) -> bool:
        """End a session: the host shuts its Claude down; False if it did not."""
        mgd = self.store.get(session_id)
        if not mgd:
            return True
        log.info("session closing id=%s name=%s", session_id[:8], mgd.full_label)
        try:
            await self.bridge.close(mgd)
        except HostError as exc:
            # The row stays: the process may well still be running, and a
            # session the chat no longer knows would ask into the void.
            log.warning("close failed id=%s: %s", session_id[:8], exc)
            return False
        self.store.remove(session_id)
        return True

    # --------------------------------------------------- claude code updates
    # ----------------------------------------------------------------- login
    async def _enter_login_code(self, m: Message, code: str, lone: bool) -> bool:
        """A sign-in code from the chat, for the login the bot runs.

        It never takes the road ordinary text takes: it is a credential, not
        a prompt. A message that is nothing but a code is answered even with
        no login waiting, not sent to Claude; one with words around it is
        taken only while a login waits (False: not handled).
        """
        replied_to = getattr(m, "reply_to_message", None)
        replied_sid = (self.store.session_of_message(replied_to.message_id)
                       if replied_to else None)
        replied_mgd = self.store.get(replied_sid) if replied_sid else None
        if self.auth.waiting:
            await self._enter_auth_code(m, code, replied_mgd)
            return True
        if not lone:
            return False
        await self._delete_quietly(m.chat.id, m.message_id)
        await m.answer(_(
            "🔑 That looks like a sign-in code, and no session is waiting "
            "for one, so I have not sent it anywhere. /login starts a "
            "login."))
        return True

    async def _restart_session(self, mgd) -> _Restart:
        """The session onto the CLI on disk, once at a time per session.

        The host leaves the client and resumes the same id, so the context
        comes back with it.
        """
        sid = mgd.session_id
        if sid in self.restarting:
            return _Restart(False, _("a restart of it is already under way"))
        self.restarting.add(sid)
        try:
            data = await self.bridge.link.call("restart", timeout=180, name=mgd.name,
                                               force=False)
        except HostError as exc:
            if exc.code == "busy":
                return _Restart(False, _("it is working or waiting for an answer"))
            log.warning("restart failed id=%s: %s", mgd.session_id[:8], exc)
            return _Restart(False, html.escape((exc.message or exc.code)[:300]))
        finally:
            self.restarting.discard(sid)
        return _Restart(True, version=str((data.get("info") or {}).get("version") or ""))

    def _stale(self, mgd, disk: str) -> str:
        """The version *mgd* runs if a restart would bring a newer one, else "".

        A resting session runs nothing: it wakes on the build on disk.
        """
        if self.bridge.state(mgd) in ("dormant", "offline", "closed"):
            return ""
        return updates.stale(mgd.session_id, disk)

    async def _outdated(self) -> tuple[str, list[tuple]]:
        """The disk version, and (session, status, its version) for each one."""
        disk = await updates.installed()
        views = {v.session_id: v for v in await sess.managed_views(self.store)}
        rows = []
        for m in self.store.all_managed():
            view = views.get(m.session_id)
            rows.append((m, view.status if view else "", self._stale(m, disk)))
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
                here = ACTIVE_ICON if active and m.session_id == active.session_id else ""
                mine = old or updates.running(m.session_id)
                mark = " ⬆️" if old else ""
                lines.append(f"{here}{icon} <b>{html.escape(m.full_label)}</b> — "
                             f"<code>{html.escape(mine or '?')}</code>{mark}")
        if not behind:
            lines += ["", _("✅ Every session is already running the version "
                            "that is on disk.")]
        else:
            skipped = [(m, st) for m, st in behind
                       if st not in ("idle", "starting")]
            lines += ["", ngettext("{count} session is behind.",
                                   "{count} sessions are behind.",
                                   len(behind)).format(count=len(behind))]
            if skipped:
                lines.append(_("Left alone by «Restart all»:"))
            reasons = [self._skip_reason(st) for _m, st in skipped]
            for (m, _st), r in zip(skipped, reasons, strict=True):
                lines.append(f"• <b>{html.escape(m.full_label)}</b> — {r}")
            lines += ["", _(
                "A restart resumes the same session on the build on disk: the "
                "session id, the transcript and the context all survive. What "
                "does not: the prompt cache — so the first reply after a "
                "restart takes a little longer.")]
        return "\n".join(lines), update_kb(
            one.session_id if one else None, len(idle))

    @staticmethod
    def _skip_reason(status: str) -> str:
        """Why «Restart all» passes a session by."""
        if status == "waiting":
            return _("it is waiting for an answer")
        if status != "busy":
            return _("its state could not be read")
        return _("it is working or waiting for an answer")

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
            _("⏳ Restarting <b>{name}</b>…").format(name=html.escape(mgd.full_label)),
            parse_mode="HTML")
        self.store.remember_message(note.message_id, mgd.session_id)
        res = await self._restart_session(mgd)
        with contextlib.suppress(Exception):
            await note.edit_text(self._restart_text(mgd, res), parse_mode="HTML")

    @staticmethod
    def _restart_text(mgd, res: _Restart) -> str:
        """What came of a restart, said in one line."""
        if res.ok and res.version:
            return _("✅ <b>{name}</b> now runs <code>{version}</code> — the "
                     "context came back with it.").format(
                         name=html.escape(mgd.full_label),
                         version=html.escape(res.version))
        if res.ok:
            return _("✅ <b>{name}</b> has been restarted; it has not said "
                     "which version it is yet.").format(
                         name=html.escape(mgd.full_label))
        return _("⚠️ <b>{name}</b> was not restarted — {reason}. Nothing "
                 "was changed.").format(name=html.escape(mgd.full_label),
                                        reason=res.reason)

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
        await self._restart_sessions(chat_id, queue)

    async def _restart_sessions(self, chat_id: int, queue: list) -> None:
        """Restart *queue* one after another, with one message saying how."""
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
                failed.append((mgd, res))
        lines = []
        if done:
            lines.append(ngettext("✅ Restarted {count} session:",
                                  "✅ Restarted {count} sessions:",
                                  len(done)).format(count=len(done)))
            lines += [f"• <b>{html.escape(m.full_label)}</b> — "
                      f"<code>{html.escape(v or '?')}</code>" for m, v in done]
        if failed:
            lines.append(_("⚠️ Left as they were:"))
            lines += [f"• <b>{html.escape(m.full_label)}</b> — {r.reason}"
                      for m, r in failed]
        with contextlib.suppress(Exception):
            await note.edit_text("\n".join(lines), parse_mode="HTML")

    async def _show_news(self, chat_id: int) -> None:
        """Release notes between the oldest session and what is on disk.

        Read from Claude Code's own cached changelog, which it refreshes when
        it updates itself: no network, no tokens, and it names exactly the
        releases a restart would bring in.
        """
        if not updates.available():
            await self.bot.send_message(chat_id, _(
                "I have no local copy of the release notes "
                "(<code>~/.claude/cache/changelog.md</code>). "
                "They are at code.claude.com/docs/en/changelog, and "
                "<code>/release-notes</code> shows them inside a session."),
                parse_mode="HTML")
            return
        disk, rows = await self._outdated()
        behind = [old for _m, _st, old in rows if old]
        since = min(behind, key=updates.parse) if behind else ""
        body, releases, left = updates.changelog(
            since, disk, limit=_NEWS_LIMIT,
            max_releases=0 if since else 1)
        if not body:
            await self.bot.send_message(chat_id, _(
                "Nothing new: every session already runs what is on disk."))
            return
        if since:
            head = ngettext(
                "🆕 **Claude Code {since} → {disk}** — {count} release",
                "🆕 **Claude Code {since} → {disk}** — {count} releases",
                releases).format(since=since, disk=disk, count=releases)
        else:
            head = _("🆕 **Claude Code {disk}** — the newest release notes"
                     ).format(disk=disk)
        tail = ""
        if left and since:
            tail = "\n\n" + ngettext(
                "…and {count} older release, at code.claude.com/docs/en/changelog",
                "…and {count} older releases, at code.claude.com/docs/en/changelog",
                left).format(count=left)
        doc = f"{head}\n\n{body}{tail}"
        if await rich.send(self.bot, chat_id, markdown=doc):
            return
        for chunk in split_text(f"{head}\n\n{body}{tail}"):
            await self.bot.send_message(chat_id, chunk)

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

    async def _sdk_picker(self, c: CallbackQuery, msg: Message, mgd, kind: str) -> None:
        """Models or effort levels of an SDK session, from Claude Code's own data.

        `get_server_info()` lists the models with their effort levels — the
        list Claude Code's own picker draws, so nothing is written down in the bot.
        A pick applies to this session only.
        """
        try:
            server = (await self.bridge.link.call("info", name=mgd.name)).get("server") or {}
        except HostError:
            await c.answer(_picker_failed(), show_alert=True)
            return
        models = [m for m in server.get("models") or [] if isinstance(m, dict)]
        rt = self.bridge.info(mgd)
        info = rt.info if rt else {}
        current = str(info.get("model") or "")
        if kind == "model":
            kb = sdk_models_kb(mgd.session_id, models, current)
            title = _("🧠 Model for <b>{name}</b> — this session only").format(
                name=html.escape(mgd.full_label))
            lines = [title, ""] + [
                f"<b>{html.escape(str(m.get('displayName') or m.get('value')))}</b> — "
                f"{html.escape(str(m.get('description') or ''))}" for m in models]
        else:
            here = next((m for m in models if m.get("resolvedModel") == current), None) \
                or (models[0] if models else {})
            levels = [str(x) for x in here.get("supportedEffortLevels") or []]
            kb = sdk_effort_kb(mgd.session_id, levels, str(info.get("effort") or ""))
            lines = [_("◉ Reasoning effort for <b>{name}</b> — this session only").format(
                name=html.escape(mgd.full_label))]
            if not levels:
                lines.append(_("This model takes no effort level."))
        await c.answer()
        await self._safe_edit(msg, "\n".join(lines), parse_mode="HTML", reply_markup=kb)

    async def _sdk_pick(self, c: CallbackQuery, mgd, what: str, value: str) -> None:
        """A model (`smd:`) or an effort level (`sef:`) picked for an SDK session."""
        try:
            if what == "smd":
                await self.bridge.link.call("set_model", name=mgd.name, model=value)
            else:
                await self.bridge.link.call("set_effort", name=mgd.name, effort=value)
        except HostError as exc:
            await c.answer(_("It is waiting for an answer — deal with that first")
                           if exc.code == "request_open" else _picker_failed(), show_alert=True)
            return
        rt = self.bridge.info(mgd)
        if rt is not None and what == "smd":
            # The model in force is known from the next reply; until then the
            # card and the effort list follow the pick.
            with contextlib.suppress(HostError):
                server = (await self.bridge.link.call("info", name=mgd.name)).get("server") or {}
                picked = next((m for m in server.get("models") or []
                               if isinstance(m, dict) and m.get("value") == value), None)
                if picked and picked.get("resolvedModel"):
                    rt.info["model"] = picked["resolvedModel"]
        log.info("sdk %s -> %s id=%s", "model" if what == "smd" else "effort", value,
                 mgd.session_id[:8])
        await c.answer(_("Model: {value}").format(value=value) if what == "smd"
                       else _("Effort: {value}").format(value=value))
        await self._show_session_card(c, mgd)

    async def _ask_sdk_text(self, c: CallbackQuery, msg: Message, chat_id: int,
                            short: str, act: str) -> None:
        """✏️ on an SDK card: the next message is the answer, the changes, the reason."""
        req = self.bridge.find(short)
        if req is None:
            await c.answer(_("This question is no longer open"), show_alert=True)
            return
        if not self.bridge.text_fits(short, act):
            await c.answer(_("The card was out of date — it is shown again"))
            await self.bridge.redraw(req)
            return
        await c.answer()
        prompt = {"pr": _("✏️ Write what to change in the plan — one message."),
                  "r": _("✏️ Write why — one message; Claude gets it with the refusal.")
                  }.get(act, _("✏️ Write your answer — one message."))
        owner = self.store.by_name(req.name)
        self.pending[chat_id] = ("sdktext", short, act,
                                 owner.session_id if owner else "", time.time())
        self.prompt_cards.pop(chat_id, None)
        self._hold_card(chat_id, msg, "question")
        ask = await msg.answer(prompt)
        if owner is not None:
            self.store.remember_message(ask.message_id, owner.session_id)
        self._hold_card(chat_id, ask, "prompt")

    async def _send_again(self, c: CallbackQuery, msg: Message, chat_id: int,
                          arg: str) -> None:
        """«🔁 Send again» under a cut-off prompt: the same text, same session."""
        n = int(arg) if arg.isdigit() else -1
        # Popped before the first await: a double tap must not send it twice.
        item = self.bridge.again.pop(n, None)
        mgd = self.store.by_name(item[0]) if item else None
        if item is None or mgd is None:
            await c.answer(_("I no longer keep that text — copy it from the card."),
                           show_alert=True)
            return
        try:
            await self._send_prompt(mgd, item[1], guard=True)
        except Refused:
            self.bridge.again[n] = item
            await c.answer(_("It has a question open — answer it first."), show_alert=True)
            return
        except HostError as exc:
            self.bridge.again[n] = item
            await c.answer(_host_refusal(exc)[:190], show_alert=True)
            return
        self.bridge.save_again()
        await c.answer(_("Sent again"))
        await self._ack(msg, _("🔁 Sent again to <b>{name}</b>").format(
            name=html.escape(mgd.full_label)), mgd)
        if self._focus(chat_id, mgd):
            await self._say_focused(msg, mgd)

    async def _start_auth_login(self, m: Message, mgd=None) -> None:
        """/login with no terminal: the sign-in link from `claude auth login`.

        The login is the machine's, not one session's: every session reads the
        same credentials.
        """
        try:
            url = await self.auth.start()
        except (LoginError, OSError) as exc:
            log.warning("auth login did not start: %s", exc)
            err = await m.answer(_("❌ Could not start the login: <code>{detail}</code>").format(
                detail=html.escape(str(exc)[:300])), parse_mode="HTML")
            if mgd is not None:
                self.store.remember_message(err.message_id, mgd.session_id)
            return
        msg = await m.answer(
            _("🔑 <b>Sign in to Claude</b> — for every session of this machine.\n\n"
              "1. Open the page with the button below and sign in with the account "
              "you want this machine to use.\n"
              "2. The page shows a code: copy all of it, the part after # included.\n"
              "3. Send it here as a message, and I will enter it."),
            parse_mode="HTML", reply_markup=auth_link_kb(url, self.auth.serial))
        if mgd is not None:
            self.store.remember_message(msg.message_id, mgd.session_id)

    async def _enter_auth_code(self, m: Message, code: str, mgd=None) -> None:
        """The code for a login the bot runs: typed in, the message deleted."""
        with contextlib.suppress(Exception):
            await m.delete()        # a credential until it is used
        note = await m.answer(_("⏳ Entering the code…"))
        if mgd is not None:
            self.store.remember_message(note.message_id, mgd.session_id)
        ok, said = await self.auth.enter(code)
        if ok:
            st = {}
            with contextlib.suppress(Exception):
                st = await auth_status()
            email = str(st.get("email") or "")
            text = (_("✅ Logged in as <b>{email}</b>.").format(email=html.escape(email))
                    if email else _("✅ Logged in."))
            await note.edit_text(text, parse_mode="HTML")
            return
        await note.edit_text(
            _("❌ Claude did not accept the code.") + f"\n\n<i>{html.escape(said)}</i>",
            parse_mode="HTML", reply_markup=auth_retry_kb())

    async def _auth_callback(self, c: CallbackQuery, msg: Message, arg: str) -> None:
        """«🔁 New link» and «✖️ Cancel» of the bot's own login."""
        if arg.startswith("cancel"):
            serial = arg.partition(":")[2]
            if serial and serial != str(self.auth.serial):
                # A card of an earlier login: the one in progress is not its.
                await c.answer(_("Login cancelled"))
                with contextlib.suppress(Exception):
                    await msg.edit_reply_markup(reply_markup=done_kb(_("✖️ Login cancelled")))
                return
            await self.auth.cancel()
            await c.answer(_("Login cancelled"))
            with contextlib.suppress(Exception):
                await msg.edit_reply_markup(reply_markup=done_kb(_("✖️ Login cancelled")))
            return
        await c.answer()
        with contextlib.suppress(Exception):
            await msg.edit_reply_markup(reply_markup=None)
        await self._start_auth_login(msg)

    async def _send_recap(self, chat_id: int, mgd) -> None:
        """«📜 Where it stopped»: the session's stand-in for a screen.

        Its state, the last task, the steps since and the last answer, read
        from the transcript — no tokens spent, and true after any restart.
        """
        rc = await asyncio.to_thread(transcript.recap, mgd.session_id)
        state = self.bridge.state(mgd)
        rt = self.bridge.info(mgd)
        if state == "busy" and rt is not None and rt.work_since:
            minutes = max(1, int((time.time() - rt.work_since) // 60))
            said = _("working for {minutes} min").format(minutes=minutes)
        else:
            said = {"busy": _("working"), "waiting": _("waiting for your answer"),
                    "idle": _("free"), "dormant": _("resting — your next message wakes it"),
                    "offline": _("the session host is not answering")}.get(state, state)
        parts = [_("**📜 {name}: where it stopped**").format(name=rich.md_escape(mgd.full_label)),
                 _("📊 State: {state}").format(state=rich.md_escape(said))]
        if rc.prompt:
            short = rc.prompt if len(rc.prompt) <= 600 else rc.prompt[:600] + "…"
            parts.append(_("**✍️ Your last task:**") + "\n\n" + rich.md_escape(short))
        if rc.steps:
            # A Markdown list: single line breaks are folded into one line.
            parts.append(_("**🔧 Last steps:**") + "\n\n" + "\n".join(
                f"- {rich.md_escape(x)}" for x in rc.steps))
        if rc.answer:
            parts.append(_("**💬 Last answer:**") + "\n\n" + rc.answer)
        if not (rc.prompt or rc.steps or rc.answer):
            parts.append(_("The transcript has nothing yet."))
        doc = "\n\n".join(parts)
        if len(doc) > rich.DOC_LIMIT:
            doc = doc[:rich.DOC_LIMIT - 1] + "…"
        kb = recap_kb(mgd.session_id, asking=self.bridge.question_open(mgd),
                      can_end=state in ("idle", "dormant"))
        msg = await rich.send(self.bot, chat_id, markdown=doc, reply_markup=kb)
        if msg is None:
            chunks = util.split_text(plain_text(doc))
            for i, chunk in enumerate(chunks):
                msg = await self.bot.send_message(
                    chat_id, chunk, reply_markup=kb if i == len(chunks) - 1 else None)
                self.store.remember_message(msg.message_id, mgd.session_id)
            return
        self.store.remember_message(msg.message_id, mgd.session_id)

    async def _interrupt_sdk(self, m: Message | None, mgd, note: str) -> None:
        """Esc for an SDK session: interrupt the turn, keep the session."""
        try:
            await self.bridge.interrupt(mgd)
            text = _("⏸ Interrupted <b>{name}</b>{note}").format(
                name=html.escape(mgd.full_label), note=note)
        except HostError as exc:
            log.info("interrupt refused id=%s: %s", mgd.session_id[:8], exc)
            if exc.code in ("dormant", "no_session"):
                text = _("💤 <b>{name}</b> is resting — there is nothing to "
                         "interrupt.").format(name=html.escape(mgd.full_label))
            else:
                text = _host_refusal(exc)
        if isinstance(m, Message):
            await self._ack(m, text, mgd)

    def _focus(self, chat_id: int, mgd) -> bool:
        """Make *mgd* the session plain text goes to; True if that moved it.

        Answering a question from its card is working *inside* that session,
        and what the user types next belongs to the same thread. Without this
        the next message went to whatever session happened to be active: on
        2026-09-01 a clarification meant for one pay4say session (cf0ab576,
        "Chat about this" pressed at 13:51) landed in another (05caad4f) 13
        seconds later, which then started working on a question that was never
        put to it.
        """
        previous = self.store.get_active(chat_id)
        if previous == mgd.session_id:
            return False
        self.store.set_active(chat_id, mgd.session_id)
        log.info("active switched by dialog: chat=%s -> %s (%s)",
                 chat_id, mgd.full_label, mgd.session_id[:8])
        return True

    async def _activate(self, chat_id: int, msg: Message, mgd) -> None:
        """Make *mgd* active from a list or a card, said at the bottom of the chat.

        The list being redrawn may be far up the chat; the line saying where
        text goes now is a new message, where the next one will be typed.
        """
        previous = self.store.get_active(chat_id)
        if previous == mgd.session_id:
            return
        self.store.set_active(chat_id, mgd.session_id)
        log.info("active switched by pick: chat=%s -> %s (%s)",
                 chat_id, mgd.full_label, mgd.session_id[:8])
        prev = self.store.get(previous) if previous else None
        with contextlib.suppress(Exception):
            note = await msg.answer(_switch_note(mgd, prev), parse_mode="HTML")
            self.store.remember_message(note.message_id, mgd.session_id)

    async def _say_focused(self, msg, mgd) -> None:
        """Tell the chat where its text goes now. Silence here is a trap."""
        with contextlib.suppress(Exception):
            note = await msg.answer(_switch_note(mgd), parse_mode="HTML")
            # Registered: a reply to it means this session, not the active one.
            self.store.remember_message(note.message_id, mgd.session_id)

    async def _send_prompt(self, mgd, text: str, guard: bool = False) -> dict | None:
        """Send *text* to the session as a prompt; the host's reply (None for /btw).

        *guard* refuses (`Refused`) when the session has a question open —
        checked by the host, so nothing can slip in between.
        """
        log.info("prompt -> %s (%s): %r", mgd.full_label, mgd.session_id[:8], text[:120])
        if text.split(maxsplit=1)[:1] == ["/btw"]:
            # Not available through the SDK as a command: the bot asks a fork.
            parts = text.split(maxsplit=1)
            question = parts[1].strip() if len(parts) > 1 else ""
            if question:
                self.bridge.side(mgd, question)
            else:
                await self.bot.send_message(self.cfg.owner, _(
                    "/btw needs the question after it, in the same message."))
            return None
        try:
            return await self.bridge.send(mgd, text, guard=guard)
        except HostError as exc:
            if exc.code == "request_open":
                raise Refused(text) from exc
            raise

    # ----------------------------------------------------------------- inbox
    def _inbox_of(self, chat_id: int) -> _Inbox:
        box = self.inbox.get(chat_id)
        if box is None:
            box = self.inbox[chat_id] = _Inbox()
        return box

    def _hold(self, m: Message, text: str = "", downloads: int = 0) -> _Item:
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
        box.downloads += downloads
        box.files += downloads
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
        paths = [p for i in items for p in i.paths]
        parked = self._parked_of(chat_id)
        if paths and not any(i.text.strip() for i in items) and parked is None:
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
        routed = await self._explicit_route(items)
        if parked is not None:
            # A batch is already waiting for a destination, so whatever
            # arrives now is part of it — the comment on a forward, a second
            # burst — rather than something of its own.
            parked.items.extend(items)
            parked.at = time.time()
            log.info("parked batch grew to %d item(s): chat=%s",
                     len(parked.items), chat_id)
            if routed and routed[0]:
                await self._deliver_parked(chat_id, routed[0], routed[1])
            elif routed:
                return          # the replied-to session has ended; it said so
            else:
                await self._ask_route(chat_id)
            return
        forwarded = any(i.sender for i in items)
        if routed is None and (forwarded or self._homeless(chat_id, items)):
            # Nothing said where this goes and it came from somewhere else:
            # forwarding a conversation is how a session is given the context
            # it starts from, so the active one is a guess, not an answer.
            # With no session active there is not even a guess, and answering
            # "no active session" dropped the message on the floor — it had
            # to be sent again once a session existed (2026-10-01).
            self.parked[chat_id] = _Parked(items=list(items), at=time.time())
            log.info("%s batch parked: chat=%s items=%d",
                     "forwarded" if forwarded else "homeless",
                     chat_id, len(items))
            await self._ask_route(chat_id)
            return
        mgd, note = routed if routed else await self._target(items[-1].message)
        if not mgd:
            return
        # Only text that went to the active session by default waits: a
        # reply names its session, so it is not the mistake the wait is for.
        await self._dispatch(chat_id, items, mgd, note, delay=routed is None)

    def _homeless(self, chat_id: int, items: list[_Item]) -> bool:
        """A batch with no session to go to, held rather than refused.

        A Claude command on its own is the exception: it acts on a session
        that is already running, so "/compact" kept for a session started
        afterwards would be meaningless there. A path is not a command,
        though it starts the same way: "/home/x.log — look" is held.
        """
        if self._active_managed(chat_id) is not None:
            return False
        return not _is_command(items)

    async def _send_batch(self, items: list[_Item], mgd, note: str = "",
                          guard: bool = False, rec: _Outgoing | None = None) -> None:
        """Fold a batch into one prompt, send it, and say where it went.

        While the session has not taken the prompt in, the confirmation
        carries «↩️ Cancel»; *rec* is the batch's record when it was delayed,
        and its notice becomes the confirmation.
        """
        paths = [p for i in items for p in i.paths]
        prompt = _batch_text(items)
        info = await self._send_prompt(mgd, prompt, guard=guard) or {}
        uid = str(info.get("uuid") or "")
        edit = rec.ack if rec else None
        if info.get("waiting") and uid and uid not in self.bridge.taken:
            if rec is None:
                rec = self._new_outgoing(items[-1].message.chat.id, mgd, items)
            rec.state, rec.uuid = "queued", uid
        elif rec is not None:
            rec.state = "done"
            self.outgoing.pop(rec.tok, None)
            rec = None
        kb = cancel_kb(rec.tok) if rec else None
        parts = []
        if len(items) > 1:
            parts.append(ngettext("{count} message", "{count} messages",
                                  len(items)).format(count=len(items)))
        if paths:
            parts.append(ngettext("{count} attachment", "{count} attachments",
                                  len(paths)).format(count=len(paths)))
        text = _("➡️ Sent to <b>{name}</b>{extra}{note}").format(
            name=html.escape(mgd.full_label),
            extra=(" · " + " · ".join(parts)) if parts else "",
            note=note)
        try:
            shown = await self._ack(items[-1].message, text, mgd, reply_markup=kb, edit=edit)
            if rec is not None:
                rec.ack = shown
                if rec.state == "done" and shown:
                    # Taken in while the confirmation was on its way.
                    with contextlib.suppress(Exception):
                        await shown.edit_reply_markup(reply_markup=None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The prompt is in the session; only the word about it failed.
            # Raising would tell the chat it "could not pass" (2026-11-02:
            # it had) or re-hold text that was already delivered, so the
            # confirmation is sent again later instead.
            log.warning("ack not delivered, retrying later: %s", exc)
            task = asyncio.create_task(
                self._ack_later(items[-1].message, text, mgd, rec))
            self.expiries.add(task)
            task.add_done_callback(self.expiries.discard)

    async def _ack_later(self, m: Message, text: str, mgd,
                         rec: _Outgoing | None = None) -> None:
        """Keep trying a delivery confirmation that met a network failure."""
        for delay in _ACK_RETRIES:
            await asyncio.sleep(delay)
            try:
                live = rec is not None and rec.state == "queued"
                shown = await self._ack(m, text, mgd,
                                        reply_markup=cancel_kb(rec.tok) if live and rec else None,
                                        edit=rec.ack if rec else None)
                if rec is not None:
                    rec.ack = shown
                return
            except Exception as exc:
                log.warning("ack still not delivered: %s", exc)
        log.error("ack given up: %r", text[:120])

    # ------------------------------------------- taking back what was sent
    def _new_outgoing(self, chat_id: int, mgd, items: list[_Item],
                      state: str = "queued", note: str = "") -> _Outgoing:
        now = time.monotonic()
        for tok, old in list(self.outgoing.items()):
            if now - old.first > _PARKED_TTL:
                del self.outgoing[tok]
        rec = _Outgoing(tok=secrets.token_hex(4), chat_id=chat_id,
                        session_id=mgd.session_id, items=list(items), state=state,
                        note=note, first=now, deadline=now + _DELAY)
        self.outgoing[rec.tok] = rec
        return rec

    def _delayed_of(self, session_id: str) -> _Outgoing | None:
        return next((r for r in self.outgoing.values() if r.state == "delayed"
                     and self.store.follow(r.session_id) == session_id), None)

    def _save_delayed(self) -> None:
        """Keep delayed batches in state.db: a restart inside the wait must
        say the text was not sent rather than drop it without a word."""
        rows = {}
        for r in self.outgoing.values():
            mgd = self.store.get(self.store.follow(r.session_id))
            if r.state != "delayed" or mgd is None:
                continue
            rows[r.tok] = {"chat_id": r.chat_id, "ack_id": r.ack.message_id if r.ack else None,
                           "name": mgd.name, "label": mgd.full_label,
                           "text": _batch_text(r.items), "count": len(r.items)}
        self.store.meta_set("delayed_batches", json.dumps(rows, ensure_ascii=False))

    @staticmethod
    def _delayed_text(rec: _Outgoing, mgd) -> str:
        seconds = max(1, round(rec.deadline - time.monotonic()))
        n = len(rec.items)
        text = _("⏳ <b>{name}</b> — sending in {seconds} s: the session is busy.") \
            if n == 1 else ngettext(
                "⏳ <b>{name}</b> — sending your {count} message in {seconds} s: "
                "the session is busy.",
                "⏳ <b>{name}</b> — sending your {count} messages in {seconds} s: "
                "the session is busy.", n)
        return text.format(name=html.escape(mgd.full_label), count=n, seconds=seconds)

    async def _show_delayed(self, rec: _Outgoing, mgd) -> None:
        """Post the notice at the bottom, where it is seen; an older one goes."""
        old = rec.ack
        rec.ack = None
        try:
            rec.ack = await self.bot.send_message(
                rec.chat_id, self._delayed_text(rec, mgd), parse_mode="HTML",
                reply_markup=cancel_kb(rec.tok))
            self.store.remember_message(rec.ack.message_id, mgd.session_id)
        except Exception:
            log.exception("delayed notice not sent: tok=%s", rec.tok)
        if old is not None:
            await self._delete_quietly(rec.chat_id, old.message_id)
        self._save_delayed()

    async def _delay(self, chat_id: int, items: list[_Item], mgd, note: str) -> None:
        rec = self._new_outgoing(chat_id, mgd, items, state="delayed", note=note)
        log.info("delayed batch: id=%s items=%d tok=%s",
                 mgd.session_id[:8], len(items), rec.tok)
        await self._show_delayed(rec, mgd)
        rec.task = asyncio.create_task(self._delay_loop(rec.tok))

    async def _join_delayed(self, rec: _Outgoing, items: list[_Item], mgd) -> None:
        rec.items.extend(items)
        rec.deadline = min(time.monotonic() + _DELAY, rec.first + _DELAY_CAP)
        log.info("delayed batch grew to %d item(s): tok=%s", len(rec.items), rec.tok)
        await self._show_delayed(rec, mgd)

    async def _delay_loop(self, tok: str) -> None:
        try:
            while True:
                await asyncio.sleep(_DELAY_TICK)
                rec = self.outgoing.get(tok)
                if rec is None or rec.state != "delayed":
                    return
                if time.monotonic() >= rec.deadline:
                    break
            with use_locale(self._locale()):
                await self._send_delayed(rec)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("delayed batch failed: tok=%s", tok)
            rec = self.outgoing.pop(tok, None)
            if rec is not None and rec.state in ("delayed", "sending"):
                self._save_delayed()
                with contextlib.suppress(Exception), use_locale(self._locale()):
                    await self._say_not_passed(rec)

    async def _send_delayed(self, rec: _Outgoing) -> None:
        """The wait is over (or earlier text has to go first): send it now."""
        if rec.state != "delayed":
            return
        rec.state = "sending"
        self._save_delayed()
        mgd = self.store.get(self.store.follow(rec.session_id))
        if mgd is None:
            self.outgoing.pop(rec.tok, None)
            log.info("delayed batch dropped, session gone: tok=%s", rec.tok)
            if rec.ack is not None:
                with contextlib.suppress(Exception):
                    await rec.ack.edit_text(_not_sent_text(len(rec.items)))
            return
        try:
            sent = await self._try_send(rec.items, mgd, rec.note, rec=rec)
        except Exception:
            self.outgoing.pop(rec.tok, None)
            log.exception("delayed batch not delivered: tok=%s", rec.tok)
            await self._say_not_passed(rec)
            return
        if sent:
            log.info("delayed batch delivered: tok=%s items=%d", rec.tok, len(rec.items))
            return
        # A question opened during the wait: the held-text card takes over.
        self.outgoing.pop(rec.tok, None)
        if rec.ack is not None:
            await self._delete_quietly(rec.chat_id, rec.ack.message_id)
        await self._hold_for_dialog(rec.chat_id, mgd, rec.items)

    async def _say_not_passed(self, rec: _Outgoing) -> None:
        """A delayed batch that failed: said on its notice, with «🔁 Send again»."""
        mgd = self.store.get(self.store.follow(rec.session_id))
        text = _("❌ Could not pass that on — see /log")
        kb = self.bridge.again_kb([(mgd.name, _batch_text(rec.items))]) if mgd else None
        if rec.ack is not None:
            with contextlib.suppress(Exception):
                await rec.ack.edit_text(text, reply_markup=kb)
                return
        sent = await self.bot.send_message(rec.chat_id, text, reply_markup=kb)
        if mgd is not None:
            self.store.remember_message(sent.message_id, mgd.session_id)

    async def _on_taken(self, name: str, uid: str) -> None:
        """A turn took the prompt in: «↩️ Cancel» can do nothing any more."""
        rec = next((r for r in self.outgoing.values() if r.uuid == uid), None)
        if rec is None or rec.state != "queued":
            return
        rec.state = "done"
        self.outgoing.pop(rec.tok, None)
        if rec.ack is not None:
            with contextlib.suppress(Exception):
                await rec.ack.edit_reply_markup(reply_markup=None)

    async def _cancel_outgoing(self, c: CallbackQuery, msg: Message, tok: str) -> None:
        rec = self.outgoing.get(tok)
        if rec is None:
            await c.answer(_("This message can no longer be cancelled."), show_alert=True)
            with contextlib.suppress(Exception):
                await msg.edit_reply_markup(reply_markup=None)
            return
        mgd = self.store.get(self.store.follow(rec.session_id))
        label = mgd.full_label if mgd else rec.session_id[:8]
        n = len(rec.items)
        done = (_("↩️ Cancelled — <b>{name}</b> did not get it.") if n == 1
                else ngettext("↩️ Cancelled — <b>{name}</b> did not get your {count} message.",
                              "↩️ Cancelled — <b>{name}</b> did not get your {count} messages.",
                              n)).format(name=html.escape(label), count=n)
        if rec.state == "delayed":
            rec.state = "done"
            self.outgoing.pop(tok, None)
            if rec.task is not None:
                rec.task.cancel()
            self._save_delayed()
            log.info("delayed batch cancelled: tok=%s items=%d", tok, len(rec.items))
            await c.answer()
            await self._settle(msg, done, rec.session_id, reply_markup=self._keep_cancelled(rec))
            return
        if rec.state in ("sending", "cancelling"):
            # On its way to the host, or a tap already being answered.
            await c.answer(_("It is being sent right now — tap again in a moment."),
                           show_alert=True)
            return
        if rec.state != "queued" or mgd is None:
            self.outgoing.pop(tok, None)
            await c.answer(_("This message can no longer be cancelled."), show_alert=True)
            with contextlib.suppress(Exception):
                await msg.edit_reply_markup(reply_markup=None)
            return
        rec.state = "cancelling"
        try:
            res = await self.bridge.cancel(mgd, rec.uuid)
        except HostError as exc:
            rec.state = "queued"
            log.info("cancel refused tok=%s: %s", tok, exc)
            with contextlib.suppress(Exception):
                await c.answer(_("Could not cancel it — try again."), show_alert=True)
            return
        self.outgoing.pop(tok, None)
        if res.get("cancelled"):
            rec.state = "done"
            log.info("queued prompt cancelled: tok=%s uuid=%s", tok, rec.uuid[:8])
            with contextlib.suppress(Exception):
                await c.answer()
            await self._settle(msg, done, rec.session_id, reply_markup=self._keep_cancelled(rec))
            return
        rec.state = "done"
        log.info("cancel too late: tok=%s why=%s", tok, res.get("why"))
        with contextlib.suppress(Exception):
            await c.answer(_("Too late — {name} has already taken it in.").format(
                name=label) if res.get("why") == "taken"
                else _("This message can no longer be cancelled."), show_alert=True)
        with contextlib.suppress(Exception):
            await msg.edit_reply_markup(reply_markup=None)

    def _prune_cancelled(self) -> None:
        now = time.time()
        for tok, kept in list(self.cancelled.items()):
            if now - kept[2] > _PARKED_TTL:
                del self.cancelled[tok]
        if len(self.rerouted) > 200:
            self.rerouted.clear()

    def _keep_cancelled(self, rec: _Outgoing) -> InlineKeyboardMarkup:
        """Keep a cancelled batch for a while so it can go elsewhere untyped."""
        self._prune_cancelled()
        self.cancelled[rec.tok] = (rec.chat_id, list(rec.items), time.time())
        return reroute_kb(rec.tok)

    async def _reroute_cancelled(self, c: CallbackQuery, msg: Message, tok: str) -> None:
        """«📋 To another session»: the cancelled batch waits for a destination.

        It becomes a held batch (`_Parked`), the way a forward does, and the
        list of destinations is asked at the bottom of the chat: the picked
        session gets it at once, and text typed meanwhile joins it.
        """
        self._prune_cancelled()
        kept = self.cancelled.pop(tok, None)
        if kept is None and tok in self.rerouted:
            await c.answer()        # a second tap: the picker is already below
            return
        with contextlib.suppress(Exception):
            await msg.edit_reply_markup(reply_markup=None)
        if kept is None:
            await c.answer(_("I no longer keep that text — send it again."), show_alert=True)
            return
        chat_id, items, _at = kept
        parked = self._parked_of(chat_id)
        fresh = parked is None
        if parked is None:
            parked = self.parked[chat_id] = _Parked(items=list(items), at=time.time())
        else:
            parked.items.extend(items)
            parked.items.sort(key=lambda i: i.message.message_id)
            parked.at = time.time()
        old_ask = parked.ask_id
        count = len(parked.items)
        head = ngettext("📥 <b>{count} message</b> is waiting",
                        "📥 <b>{count} messages</b> are waiting", count).format(count=count)
        try:
            sent = await self.bot.send_message(
                chat_id, head + "\n\n" + self._route_picker_text(chat_id), parse_mode="HTML",
                reply_markup=route_pick_kb(await sess.managed_views(self.store),
                                           self.store.get_active(chat_id), back=False))
        except Exception:
            log.exception("reroute picker not sent: chat=%s", chat_id)
            # Undone, so the text is not held where nobody can see it.
            if fresh:
                self.parked.pop(chat_id, None)
            else:
                parked.items = [i for i in parked.items if i not in items]
            self.cancelled[tok] = kept
            with contextlib.suppress(Exception):
                await msg.edit_reply_markup(reply_markup=reroute_kb(tok))
            with contextlib.suppress(Exception):
                await c.answer(_("❌ Could not pass that on — see /log"), show_alert=True)
            return
        self.rerouted.add(tok)
        await c.answer()
        parked.ask_id = sent.message_id
        if old_ask:
            await self._delete_quietly(chat_id, old_ask)
        log.info("cancelled batch parked for another session: chat=%s items=%d",
                 chat_id, count)

    async def _recover_delayed(self) -> None:
        """Batches a restart caught inside their wait: say they were not sent."""
        try:
            rows = json.loads(self.store.meta_get("delayed_batches") or "{}")
        except ValueError:
            rows = {}
        self.store.meta_set("delayed_batches", "{}")
        for row in rows.values() if isinstance(rows, dict) else []:
            try:
                n = int(row.get("count") or 1)
                text = (_("<b>⚠️ Not sent to {name}</b>\n\n"
                          "The bot restarted before your message went there.")
                        if n == 1 else ngettext(
                    "<b>⚠️ Not sent to {name}</b>\n\n"
                    "The bot restarted before your {count} message went there.",
                    "<b>⚠️ Not sent to {name}</b>\n\n"
                    "The bot restarted before your {count} messages went there.", n)).format(
                        name=html.escape(str(row.get("label") or row.get("name"))), count=n)
                kb = self.bridge.again_kb([(str(row["name"]), str(row["text"]))])
                log.info("delayed batch lost to a restart: %s", row.get("name"))
                mgd = self.store.by_name(str(row["name"]))
                if row.get("ack_id"):
                    try:
                        await self.bot.edit_message_text(
                            text, chat_id=row["chat_id"], message_id=row["ack_id"],
                            parse_mode="HTML", reply_markup=kb)
                        continue
                    except TelegramBadRequest:
                        pass
                sent = await self.bot.send_message(row["chat_id"], text, parse_mode="HTML",
                                                   reply_markup=kb)
                if mgd is not None:
                    self.store.remember_message(sent.message_id, mgd.session_id)
            except Exception:
                log.exception("could not report a delayed batch lost to a restart")

    # ------------------------------------------------- held for an open question
    def _question_open(self, mgd) -> bool:
        """Whether the session has a question open, so it takes no prompt."""
        return self.bridge.question_open(mgd)

    async def _dispatch(self, chat_id: int, items: list[_Item], mgd,
                        note: str = "", delay: bool = False) -> None:
        """Send a batch, or hold it while the session has a question open.

        The only road user text takes into a session: sent while a question
        stands, it would land beside the question nobody has answered.
        *delay*: the batch went to the active session by default, so if that
        session is busy it waits `_DELAY` with «↩️ Cancel» — the window to
        notice it went to the wrong place.
        """
        held = self._held_of(mgd.session_id)
        if held is not None:
            await self._join_held(held, items, mgd)
            return
        pending = self._delayed_of(mgd.session_id)
        if pending is not None:
            if delay and not _is_command(items):
                await self._join_delayed(pending, items, mgd)
                return
            # A reply or a command must not overtake text typed before it.
            await self._send_delayed(pending)
        if delay and not _is_command(items) and self.bridge.state(mgd) == "busy":
            await self._delay(chat_id, items, mgd, note)
            return
        if await self._try_send(items, mgd, note):
            return
        await self._hold_for_dialog(chat_id, mgd, items)

    async def _try_send(self, items: list[_Item], mgd, note: str = "",
                        rec: _Outgoing | None = None) -> bool:
        """Send unless a question is open — checked here, and again by the host."""
        if self._question_open(mgd):
            return False
        try:
            await self._send_batch(items, mgd, note, guard=True, rec=rec)
        except Refused:
            log.info("held: send refused, a question appeared id=%s",
                     mgd.session_id[:8])
            return False
        return True

    async def _send_command(self, mgd, command: str) -> bool:
        """A Claude command from a button, refused while a question is open."""
        if self._question_open(mgd):
            return False
        try:
            await self._send_prompt(mgd, command, guard=True)
        except Refused:
            return False
        return True

    async def _hold_for_dialog(self, chat_id: int, mgd,
                               items: list[_Item]) -> None:
        held = self.held.get(mgd.session_id)
        if held is not None:
            # Two flushes raced to the same blocked session.
            await self._join_held(held, items, mgd)
            return
        held = self.held[mgd.session_id] = _Held(
            session_id=mgd.session_id, chat_id=chat_id,
            batches=[list(items)], at=time.time())
        log.info("held batch: id=%s items=%d (a question is open)",
                 mgd.session_id[:8], len(items))
        await self._ask_held(held, mgd)

    def _held_of(self, session_id: str) -> _Held | None:
        """The text held for this session, if it is still current."""
        held = self.held.get(session_id) or next(
            (h for k, h in self.held.items()
             if self.store.follow(k) == session_id), None)
        if held is None or time.time() - held.at <= _PARKED_TTL:
            return held
        session_id = held.session_id
        self.held.pop(session_id, None)
        if held.task is not None:
            held.task.cancel()
        log.info("held batch expired: id=%s items=%d",
                 session_id[:8], held.count)
        task = asyncio.create_task(self._say_held_expired(held))
        self.expiries.add(task)
        task.add_done_callback(self.expiries.discard)
        return None

    async def _join_held(self, held: _Held, items: list[_Item], mgd) -> None:
        """More text for a session that is still held: it waits with the rest.

        A command travels alone, so it never merges into a text batch.
        """
        if (_is_command(items) or not held.batches
                or _is_command(held.batches[-1])):
            held.batches.append(list(items))
        else:
            held.batches[-1].extend(items)
        held.at = time.time()
        log.info("held batch grew: id=%s items=%d",
                 held.session_id[:8], held.count)
        await self._ask_held(held, mgd)

    def _rehold(self, held: _Held) -> None:
        """Put a popped hold back, merging whatever was held meanwhile."""
        newer = self.held.get(held.session_id)
        if newer is not None and newer is not held:
            held.batches.extend(newer.batches)
            if newer.ask_id:
                task = asyncio.create_task(
                    self._delete_quietly(held.chat_id, newer.ask_id))
                self.expiries.add(task)
                task.add_done_callback(self.expiries.discard)
        held.waiting = False
        held.at = time.time()
        self.held[held.session_id] = held

    async def _delete_quietly(self, chat_id: int, message_id: int) -> None:
        with contextlib.suppress(Exception):
            await self.bot.delete_message(chat_id, message_id)

    @staticmethod
    def _held_waiting_text(held: _Held, mgd) -> str:
        return ngettext(
            "⏳ Your message goes to <b>{name}</b> once its question is "
            "answered.",
            "⏳ Your {count} messages go to <b>{name}</b> once its question "
            "is answered.", held.count).format(
                count=held.count, name=html.escape(mgd.full_label))

    async def _ask_held(self, held: _Held, mgd) -> None:
        """Ask what to do with the held text, always at the bottom of the chat.

        A card scrolled above the newer messages would be answered for the
        wrong batch, so a grown hold is asked again. A waiting one says again
        that it waits, for the same reason: a message joining it silently
        would look lost.
        """
        async with held.lock:
            if held.ask_id:
                await self._delete_quietly(held.chat_id, held.ask_id)
                held.ask_id = None
            if held.waiting:
                text = self._held_waiting_text(held, mgd)
                kb = held_wait_kb(held.session_id)
            else:
                # A question takes a typed answer for its current part.
                asked = self.bridge.open_question(mgd)
                held.question_sig = f"{asked.rid}:{asked.index}" if asked else ""
                free = (asked is not None and len(held.batches) == 1
                        and not _is_command(held.batches[0]))
                text = (_(
                    "<b>❓ {name} has an open question</b>\n\n"
                    "It is above — you may have missed it. Your message has "
                    "not been sent yet, so that it does not pick an option for "
                    "you.") if held.count == 1 else ngettext(
                    "<b>❓ {name} has an open question</b>\n\n"
                    "It is above — you may have missed it. Your {count} message "
                    "has not been sent yet, so that it does not pick an option "
                    "for you.",
                    "<b>❓ {name} has an open question</b>\n\n"
                    "It is above — you may have missed it. Your {count} "
                    "messages have not been sent yet, so that they do not pick "
                    "an option for you.",
                    held.count)).format(count=held.count,
                                        name=html.escape(mgd.full_label))
                kb = held_kb(held.session_id, free)
            try:
                msg = await self.bot.send_message(
                    held.chat_id, text, parse_mode="HTML", reply_markup=kb)
            except TelegramBadRequest:
                log.exception("held card failed: id=%s", held.session_id[:8])
                return
            held.ask_id = msg.message_id
            self.store.remember_message(msg.message_id, held.session_id)

    def _on_damaged(self, chat_id: int | None, message_id: int | None,
                    parts: list[str]) -> None:
        """An update lost parts on the way in: say so (see `transport`).

        Only to a chat the bot serves: a stranger's broken message is logged
        by the transport and answered with nothing, like the rest of theirs.
        """
        chat_id = chat_id or self.cfg.owner
        if chat_id not in self.cfg.allowed:
            return
        task = asyncio.get_running_loop().create_task(
            self._say_damaged(chat_id, message_id, parts))
        self.expiries.add(task)
        task.add_done_callback(self.expiries.discard)

    async def _say_damaged(self, chat_id: int, message_id: int | None,
                           parts: list[str]) -> None:
        """Silence here would read as "the whole message was received".

        It answers the message that lost the part, so in a forwarded batch
        the right bubble is the one marked.
        """
        with use_locale(self._locale()):
            if "update" in parts:
                text = _("⚠️ Telegram sent something this version of the bot "
                         "cannot read at all, and it was skipped. If it was a "
                         "message, send it again in another form. Details in "
                         "/log.")
            else:
                text = _("⚠️ Part of this message could not be read by this "
                         "version of the bot and was left out: {parts}. The "
                         "rest was received. Send that part again in another "
                         "form if it matters.").format(
                             parts=", ".join(dict.fromkeys(parts)))
        reply = (ReplyParameters(message_id=message_id,
                                 allow_sending_without_reply=True)
                 if message_id else None)
        try:
            await self.bot.send_message(chat_id, text, reply_parameters=reply)
        except Exception:
            log.exception("damaged-update notice failed")

    async def _say_held_expired(self, held: _Held) -> None:
        """Silence here would read as "it was delivered"."""
        text = ngettext(
            "⌛️ The message that was waiting for an answered question has "
            "expired — it went nowhere. Send it again if it is still needed.",
            "⌛️ The {count} messages that were waiting for an answered "
            "question have expired — they went nowhere. Send them again if "
            "they are still needed.", held.count).format(count=held.count)
        if held.ask_id:
            with contextlib.suppress(Exception):
                await self.bot.edit_message_text(
                    text, chat_id=held.chat_id, message_id=held.ask_id)
                return
        with contextlib.suppress(Exception):
            msg = await self.bot.send_message(held.chat_id, text)
            if msg:
                self.store.remember_message(msg.message_id, held.session_id)

    async def _held_callback(self, c: CallbackQuery, msg: Message,
                             chat_id: int, arg: str) -> None:
        """The answer to a held card: ✏️ ans, 💬 esc, ⏳ wait, 🗑 drop."""
        action, _sep, short = arg.partition(":")
        held = next((h for h in self.held.values()
                     if h.session_id.startswith(short)), None)
        if held is None:
            await c.answer(_("Those messages are no longer waiting"),
                           show_alert=True)
            with contextlib.suppress(Exception):
                await msg.edit_reply_markup(reply_markup=None)
            return
        # Popped before the first await: a double tap, or the waiting loop
        # finding the question answered, cannot deliver it twice.
        self.held.pop(held.session_id, None)
        if held.task is not None and held.task is not asyncio.current_task():
            held.task.cancel()
            held.task = None
        mgd = self.store.get(self.store.follow(held.session_id))
        count = held.count
        if mgd is None:
            await c.answer(_("That session is gone"), show_alert=True)
            await self._settle(msg, ngettext(
                "🗑 Not sent", "🗑 {count} messages not sent",
                count).format(count=count))
            return
        name = html.escape(mgd.full_label)
        if action == "drop":
            await c.answer()
            log.info("held batch dropped: id=%s items=%d",
                     held.session_id[:8], count)
            await self._settle(msg, ngettext(
                "🗑 Not sent", "🗑 {count} messages not sent",
                count).format(count=count), held.session_id)
        elif action == "wait":
            await c.answer()
            held.waiting = True
            held.ask_id = msg.message_id
            self.held[held.session_id] = held
            with contextlib.suppress(Exception):
                await msg.edit_text(self._held_waiting_text(held, mgd),
                                    parse_mode="HTML",
                                    reply_markup=held_wait_kb(held.session_id))
            held.task = asyncio.create_task(self._held_loop(held.session_id))
            log.info("held batch waits for the answer: id=%s items=%d",
                     held.session_id[:8], count)
        elif action == "esc":
            await c.answer(_("Closing the question…"))
            await self.bridge.dismiss(mgd, "The user closed this question "
                                           "to send a message instead.")
            if self._question_open(mgd):
                log.warning("held: Esc did not close the question id=%s",
                            held.session_id[:8])
                self._rehold(held)
                note = await msg.answer(_("Esc did not close the question — "
                                          "the message is still waiting."))
                if note:
                    self.store.remember_message(note.message_id,
                                                held.session_id)
                await self._ask_held(held, mgd)
                return
            if await self._held_deliver_or_rehold(held, mgd):
                await self._settle(msg, _(
                    "💬 Question in <b>{name}</b> closed, message sent").format(
                        name=name), held.session_id)
        elif action == "ans":
            asked = self.bridge.open_question(mgd)
            if asked is None and not self.bridge.question_open(mgd):
                # Answered meanwhile from its card: the text goes as it is.
                await c.answer()
                if await self._held_deliver_or_rehold(held, mgd):
                    await self._settle(msg, _(
                        "➡️ The question had closed — sent to <b>{name}</b> "
                        "as an ordinary message").format(name=name),
                        held.session_id)
                return
            if (asked is None or f"{asked.rid}:{asked.index}" != held.question_sig
                    or len(held.batches) != 1):
                self._rehold(held)
                await c.answer(_("The question has changed — it is above"),
                               show_alert=True)
                await self._ask_held(held, mgd)
                return
            await c.answer()
            status, answered, note = await self.bridge.answer_text(
                asked.rid[:8], f"t{asked.index}", _batch_text(held.batches[0]))
            log.info("held batch given as the sdk answer: id=%s status=%s",
                     held.session_id[:8], status)
            if status == "failed":
                # Nothing was delivered: the text waits again, card and all.
                self._rehold(held)
                with contextlib.suppress(Exception):
                    await msg.answer(note)
                await self._ask_held(held, mgd)
                return
            await self._settle(msg, html.escape(note), held.session_id)
            if status == "done" and answered is not None and self._focus(chat_id, answered):
                await self._say_focused(msg, answered)
        else:
            self._rehold(held)
            await c.answer()

    async def _ask_held_by_id(self, held: _Held) -> None:
        mgd = self.store.get(self.store.follow(held.session_id))
        if mgd is not None:
            await self._ask_held(held, mgd)

    async def _held_deliver_or_rehold(self, held: _Held, mgd) -> bool:
        """Deliver every batch; a question standing again holds what is left."""
        rest = await self._deliver_held(held, mgd)
        if not rest:
            return True
        held.batches = rest
        self._rehold(held)
        await self._ask_held(held, mgd)
        return False

    async def _deliver_held(self, held: _Held, mgd) -> list[list[_Item]]:
        """Send the batches in order; return those a new question stopped."""
        for i, batch in enumerate(held.batches):
            if not await self._try_send(batch, mgd):
                return held.batches[i:]
        log.info("held batch delivered: id=%s items=%d",
                 held.session_id[:8], held.count)
        return []

    async def _held_loop(self, session_id: str) -> None:
        """Send the held text once the question is gone, for long enough.

        "Gone" means no dialog at all for `_HELD_CLEAR`: a multi-part question
        draws its next section at once, and a frame caught in between must
        not send the text into the middle of it. A busy session is fine — the
        CLI takes a prompt sent mid-turn into the running turn once the
        current tool call ends.
        """
        clear_since = 0.0
        held: _Held | None = None
        try:
            while True:
                await asyncio.sleep(_HELD_TICK)
                held = self.held.get(session_id)
                if held is None or not held.waiting:
                    return
                if time.time() - held.at > _PARKED_TTL:
                    self.held.pop(session_id, None)
                    log.info("held batch expired: id=%s items=%d",
                             session_id[:8], held.count)
                    await self._say_held_expired(held)
                    return
                mgd = self.store.get(self.store.follow(session_id))
                if mgd is None:
                    self.held.pop(session_id, None)
                    log.info("held batch dropped, session gone: id=%s",
                             session_id[:8])
                    with contextlib.suppress(Exception):
                        await self.bot.edit_message_text(
                            _not_sent_text(held.count),
                            chat_id=held.chat_id, message_id=held.ask_id)
                    return
                blocked = self._question_open(mgd)
                now = time.monotonic()
                if blocked:
                    clear_since = 0.0
                    continue
                if not clear_since:
                    clear_since = now
                if now - clear_since < _HELD_CLEAR:
                    continue
                if self.held.get(session_id) is not held:
                    return
                self.held.pop(session_id, None)
                rest = await self._deliver_held(held, mgd)
                if rest:
                    held.batches = rest
                    self._rehold(held)
                    held.waiting = True
                    clear_since = 0.0
                    continue
                with contextlib.suppress(Exception):
                    await self.bot.edit_message_text(
                        _("✅ The question in <b>{name}</b> was answered — "
                          "message sent").format(
                              name=html.escape(mgd.full_label)),
                        chat_id=held.chat_id, message_id=held.ask_id,
                        parse_mode="HTML")
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("held delivery failed: id=%s", session_id[:8])
            if held is not None and session_id not in self.held:
                # Popped for delivery and then lost: put it back undecided,
                # so the card offers the choice again instead of nothing.
                self._rehold(held)
                with contextlib.suppress(Exception):
                    await self._ask_held_by_id(held)
            with contextlib.suppress(Exception):
                await self.bot.send_message(
                    held.chat_id if held else self.cfg.owner,
                    _("❌ Could not pass that on — see /log"))

    async def _explicit_route(self, items: list[_Item]) -> tuple | None:
        """The session a batch names itself, by replying to one of ours.

        None means nothing named one. A forwarded message often replies to
        another forwarded message, which is no route at all — hence the check
        that the reply is one of the bot's own cards before it decides
        anything. The tuple may carry no session at all: the reply named one
        that has meanwhile ended, and `_target` has already said so.
        """
        for item in items:
            replied = getattr(item.message, "reply_to_message", None)
            if replied and self.store.session_of_message(replied.message_id):
                return await self._target(item.message)
        return None

    async def _batch_target(self, items: list[_Item]) -> tuple:
        """Where a whole batch goes: the first reply naming a session wins."""
        routed = await self._explicit_route(items)
        if routed is not None:
            return routed
        return await self._target(items[-1].message)

    # ---------------------------------------------------------------- routing
    def _parked_of(self, chat_id: int) -> _Parked | None:
        """The batch this chat is holding, if it is still current.

        An expired one is dropped here rather than delivered: a forward from
        yesterday must not swallow today's first message.
        """
        parked = self.parked.get(chat_id)
        if parked is None:
            return None
        if time.time() - parked.at <= _PARKED_TTL:
            return parked
        self.parked.pop(chat_id, None)
        log.info("parked batch expired: chat=%s items=%d",
                 chat_id, len(parked.items))
        task = asyncio.create_task(self._say_parked_expired(chat_id, parked))
        self.expiries.add(task)
        task.add_done_callback(self.expiries.discard)
        return None

    async def _say_parked_expired(self, chat_id: int, parked: _Parked) -> None:
        """Silence here would read as "the forward was delivered"."""
        with contextlib.suppress(Exception):
            if parked.ask_id:
                await self.bot.delete_message(chat_id, parked.ask_id)
        with contextlib.suppress(Exception):
            await self.bot.send_message(chat_id, (ngettext(
                "⌛️ The forwarded message that was waiting for a session has "
                "expired — it went nowhere. Forward it again if it is still "
                "needed.",
                "⌛️ The {count} forwarded messages that were waiting for a "
                "session have expired — they went nowhere. Forward them again "
                "if they are still needed.",
                len(parked.items)) if parked.forwarded else ngettext(
                "⌛️ The message that was waiting for a session has expired — "
                "it went nowhere. Send it again if it is still needed.",
                "⌛️ The {count} messages that were waiting for a session have "
                "expired — they went nowhere. Send them again if they are "
                "still needed.",
                len(parked.items))).format(count=len(parked.items)))

    async def _ask_route(self, chat_id: int) -> None:
        """Ask where the held batch goes, always at the bottom of the chat."""
        parked = self.parked.get(chat_id)
        if parked is None:
            return
        if parked.ask_id:
            # The batch has grown since the question was asked, and the answer
            # has to sit under the messages it is about.
            with contextlib.suppress(Exception):
                await self.bot.delete_message(chat_id, parked.ask_id)
            parked.ask_id = None
        files = sum(len(i.paths) for i in parked.items)
        count = len(parked.items)
        head = (ngettext("📥 <b>{count} forwarded message</b> is waiting",
                         "📥 <b>{count} forwarded messages</b> are waiting",
                         count) if parked.forwarded else
                ngettext("📥 <b>{count} message</b> is waiting",
                         "📥 <b>{count} messages</b> are waiting",
                         count)).format(count=count)
        if files:
            head += " · " + ngettext("{count} file", "{count} files",
                                     files).format(count=files)
        active = self._active_managed(chat_id)
        if active:
            text = head + "\n\n" + _("Which session should they go to? "
                                     "Anything you send meanwhile joins them.")
            kb = route_kb(active.full_label)
        else:
            # No session is active, so «▶️ Current» would lead nowhere and
            # «📋 Another session» would be a tap spent on reaching the list:
            # the list itself is the question, and «➕ New session» on it is
            # the usual answer.
            text = head + "\n\n" + _(
                "No session is active, so there is nowhere to send them yet. "
                "Start a new one, bring a closed one back or pick one from "
                "the list — they go there as soon as it is up. Anything you "
                "send meanwhile joins them.")
            kb = route_pick_kb(await sess.managed_views(self.store), None,
                               back=False)
        msg = await self.bot.send_message(
            chat_id, text, parse_mode="HTML", reply_markup=kb)
        if msg:
            parked.ask_id = msg.message_id

    def _active_managed(self, chat_id: int):
        """The active session, or None — without saying anything about it."""
        sid = self.store.get_active(chat_id)
        return self.store.get(sid) if sid else None

    async def _deliver_parked(self, chat_id: int, mgd, note: str = "") -> None:
        """Send the held batch to *mgd*, and make that session the active one.

        Answering the question is working inside that session, so what the
        user types next belongs to it — the same rule as answering a dialog
        from its card.
        """
        parked = self.parked.pop(chat_id, None)
        if parked is None or not parked.items:
            return
        if parked.ask_id:
            # It has been answered; leaving it standing invites a second tap.
            with contextlib.suppress(Exception):
                await self.bot.delete_message(chat_id, parked.ask_id)
        if self._focus(chat_id, mgd) and not note:
            note = " · " + _("now active")
        log.info("parked batch delivered: chat=%s -> %s items=%d",
                 chat_id, mgd.session_id[:8], len(parked.items))
        await self._dispatch(chat_id, parked.items, mgd, note)

    async def _deliver_after_launch(self, chat_id: int, session_id: str) -> None:
        """Hand the held batch to a session that has just been launched.

        The host answers a create once the session is up, so the batch can go
        at once. A session started from /sessions rather than from the
        question gets the question again, under its own card, with «▶️
        Current» naming it.
        """
        mgd = self.store.get(session_id)
        parked = self._parked_of(chat_id)
        if mgd is None or parked is None:
            return
        if not parked.await_new:
            await self._ask_route(chat_id)
            return
        parked.await_new = False
        await self._deliver_parked(chat_id, mgd)

    async def _route_callback(self, c: CallbackQuery, msg: Message,
                              chat_id: int, arg: str) -> None:
        """The `fwd:` buttons — where a held batch goes, or that it does not.

        Both "new session" and "recent" hand the choice over to the ordinary
        pickers (`nd:` and the history list) and only mark the batch as
        waiting: whichever session `_create` brings up next takes it, so
        there is one delivery path instead of three.
        """
        parked = self._parked_of(chat_id)
        if parked is None:
            await c.answer(_("Those messages are no longer waiting"),
                           show_alert=True)
            with contextlib.suppress(Exception):
                await msg.edit_reply_markup(reply_markup=None)
            return
        if arg == "drop":
            self.parked.pop(chat_id, None)
            await c.answer(_("Discarded"))
            await self._safe_edit(msg, (ngettext(
                "🗑 The forwarded message was discarded — it went to no "
                "session.",
                "🗑 The {count} forwarded messages were discarded — they went "
                "to no session.",
                len(parked.items)) if parked.forwarded else ngettext(
                "🗑 The message was discarded — it went to no session.",
                "🗑 The {count} messages were discarded — they went to no "
                "session.",
                len(parked.items))).format(count=len(parked.items)))
            return
        if arg == "cur":
            mgd = self._active_managed(chat_id)
            if mgd is None:
                await c.answer(_("No active session — pick one below"),
                               show_alert=True)
                await self._show_route_picker(msg, chat_id)
                return
            await c.answer(_("Sending…"))
            await self._deliver_parked(chat_id, mgd)
            return
        if arg == "list":
            await c.answer()
            await self._show_route_picker(msg, chat_id)
            return
        if arg == "back":
            # Asked again rather than edited back: `_ask_route` is the one
            # place that words the question, and it puts it at the bottom,
            # under whatever has arrived meanwhile.
            await c.answer()
            await self._ask_route(chat_id)
            return
        if arg.startswith("to:"):
            mgd = self._resolve(arg[3:])
            if not mgd:
                await c.answer(_("That session is gone"), show_alert=True)
                await self._show_route_picker(msg, chat_id)
                return
            await c.answer(_("Sending…"))
            await self._deliver_parked(chat_id, mgd)
            return
        # Both pickers below open in a message of their own, leaving the
        # question standing: one that had been replaced by a directory list
        # would leave the batch with no visible destination if the user backed
        # out of that list.
        if arg == "new":
            parked.await_new = True
            await c.answer()
            await self._ask_dir(msg)
            return
        if arg == "hist":
            parked.await_new = True
            await c.answer(_("Looking…"))
            holder = await msg.answer(_("🕘 Looking through the transcripts…"))
            await self._show_history(holder, chat_id)
            return
        await c.answer()

    def _route_picker_text(self, chat_id: int) -> str:
        """The words above the list of destinations for the held batch."""
        parked = self.parked.get(chat_id)
        title = (_("📋 <b>Where should the forwarded messages go?</b>")
                 if parked is None or parked.forwarded
                 else _("📋 <b>Where should the messages go?</b>"))
        return title + "\n\n" + _(
            "A running session takes them straight away. ➕ starts a new one "
            "in a directory you pick, 🕘 brings a closed one back — either "
            "way they are sent as soon as it is up.")

    async def _show_route_picker(self, msg: Message, chat_id: int) -> None:
        """The session list, as a list of destinations for the held batch."""
        views = await sess.managed_views(self.store)
        active = self.store.get_active(chat_id)
        await self._safe_edit(msg, self._route_picker_text(chat_id), parse_mode="HTML",
                              reply_markup=route_pick_kb(views, active))

    # ----------------------------------------------------------------- media
    async def _on_media(self, m: Message, text: str, atts: list[_Att]) -> None:
        """Save a message's files into the batch, keeping its place in the order.

        A file that cannot be had does not take the words with it: they are
        still the message, and the person has been told about the file.
        """
        box = self._inbox_of(m.chat.id)
        item = self._hold(m, text=text, downloads=len(atts))
        first = box.files - len(atts)
        # Several files come down side by side, and their failures are told
        # once each, not once per file.
        problems: dict[str, None] = {}
        try:
            owner = await self._media_owner(m) if atts else _INBOX_DIR
            got = await asyncio.gather(*(
                self._download(m, att, owner, first + i, problems)
                for i, att in enumerate(atts)))
            item.paths.extend(p for p in got if p is not None)
        finally:
            box.downloads = max(0, box.downloads - len(atts))
            box.last = time.monotonic()
        for text_ in problems:
            with contextlib.suppress(Exception):
                await m.answer(text_)
        if not item.paths and not item.text.strip():
            box.items.pop(m.message_id, None)

    async def _on_rich(self, m: Message) -> None:
        """A Premium client's formatted message: paragraphs and pictures as blocks.

        Without files it is just text, and it takes the text's road: a dialog
        answer, a new name or a directory typed with formatting is still that.
        """
        text, found = rich.read(m.rich_message)
        atts = [_att(f.obj, f.mime, f.suffix) for f in found]
        log.info("rich message id=%s: %d chars, %d file(s)",
                 m.message_id, len(text), len(atts))
        if not text and not atts:
            await self._unreadable(m)
        elif not atts:
            await self._on_text(m, text)
        else:
            await self._on_media(m, text, atts)

    async def _unreadable(self, m: Message) -> None:
        """A message no handler could read says so instead of vanishing."""
        kind = logsetup.content_kind(m)
        log.warning("unreadable message id=%s content_type=%s", m.message_id, kind)
        await m.answer(_("⚠️ I cannot read this kind of message "
                         "(<code>{kind}</code>), so it went nowhere. Send it "
                         "as text, a photo or a file.").format(
                             kind=html.escape(kind)),
                       parse_mode="HTML")

    async def _media_owner(self, m: Message) -> str:
        """The session a message's files are filed under."""
        # A photo sent as a reply switches the active session too, so the
        # caption that follows it lands in the same place.
        mgd = None
        if getattr(m, "reply_to_message", None) or self._active_managed(m.chat.id):
            mgd, _note = await self._target(m)
        # No session yet is not a reason to refuse the file: a forwarded batch
        # is held until the chat says where it goes, and that may well be a
        # session created afterwards. The id only names the folder the file
        # lands in — Claude is given the absolute path either way.
        return mgd.session_id if mgd else _INBOX_DIR

    async def _download(self, m: Message, att: _Att, owner: str, index: int,
                        problems: dict[str, None]) -> Path | None:
        """Fetch one file of *m*; note what went wrong in *problems*."""
        if not att.file_id:
            return None

        size = att.size
        if size > _MAX_ATTACHMENT:
            # Telegram's own ceiling for a bot download, not a rule of ours.
            problems[_("That file is too big ({size} MB). Telegram lets "
                       "a bot download 20 MB at most — put it somewhere "
                       "on disk and send me the path instead.").format(
                           size=size // 1024 // 1024)] = None
            return None

        try:
            tg_file = await self.bot.get_file(att.file_id)
            if not tg_file.file_path:
                raise RuntimeError(f"Telegram returned no path for {att.file_id}")
            suffix = media.guess_suffix(tg_file.file_path, att.mime, att.fallback)
            path = media.new_path(owner, suffix, index, att.name)
            await self.bot.download_file(tg_file.file_path, destination=path)
        except Exception:
            log.exception("attachment download failed")
            problems[_("❌ Could not download the file")] = None
            return None
        log.info("attachment saved: %s (%s, %s bytes)", path, att.mime or "?", size)
        return path

    # -------------------------------------------------------------- handlers
    async def _on_text(self, m: Message, text: str | None = None) -> None:
        """Ordinary text; *text* stands in for ``m.text`` when it came as blocks."""
        chat_id = m.chat.id
        text = m.text or "" if text is None else text
        pend_kind = self.pending.get(chat_id, ("—",))[0]
        active = self.store.get_active(chat_id)
        log.info(
            "text chat=%s pending=%s active=%s reply_to=%s text=%r",
            chat_id, pend_kind, (active or "—")[:8],
            getattr(getattr(m, "reply_to_message", None), "message_id", None),
            authlogin.mask(text)[:120],
        )
        code = authlogin.code_in(text) if m.forward_origin is None else ""
        if code and await self._enter_login_code(
                m, code, lone=bool(authlogin.code_of(text))):
            return

        pend = self.pending.pop(chat_id, None)
        if pend and time.time() - pend[-1] > _PENDING_TTL:
            await m.answer(_("⌛️ That request has expired — treating this as "
                             "an ordinary message."))
            self.prompt_cards.pop(chat_id, None)
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
        # A command typed instead of a search word is a change of mind, not a
        # search for "/sessions": let it through to its own handler.
        if pend and pend[0] == "hfind" and not text.startswith("/"):
            await self._search_from_text(m, text)
            return
        if pend and pend[0] == "sdktext" and not text.startswith("/"):
            _kind, short, act, owner_sid, _ts = pend
            status, mgd, note = await self.bridge.answer_text(short, act, text)
            if mgd is None and owner_sid:
                # The request is gone altogether: the session it belonged to
                # still gets the text.
                mgd = self.store.get(self.store.follow(owner_sid))
            # Only the "write your answer" prompt goes: the card is the
            # bridge's — it shows the next part, or the answer, by now.
            for msg_id, role in self.prompt_cards.pop(chat_id, []):
                if role == "prompt":
                    await self._delete_quietly(chat_id, msg_id)
            if status == "failed" and mgd is not None:
                # Not applied, and not lost: it goes to the session the way
                # any message does — held there, with its own card, while a
                # question is open.
                reply = await m.answer(note + "\n" + _(
                    "Your message goes to the session as an ordinary message."))
                self.store.remember_message(reply.message_id, mgd.session_id)
                await self._dispatch(chat_id, [_Item(message=m, text=text,
                                                     sender=_sender_name(m))], mgd)
                return
            reply = await m.answer(note)
            if mgd is not None:
                self.store.remember_message(reply.message_id, mgd.session_id)
                if status == "done" and self._focus(chat_id, mgd):
                    await self._say_focused(m, mgd)
            return
        if pend and pend[0] == "rename":
            _kind, session_id, _ts = pend
            # `follow`: /clear may have renamed the session while the name was
            # being typed, and the row moved to a new id.
            mgd = self.store.get(self.store.follow(session_id))
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
                if await self._create(chat_id, candidate):
                    await self._retire_cards(
                        chat_id, summary=self._dir_picked(candidate))
                else:
                    self.prompt_cards.pop(chat_id, None)
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
        if not data.startswith(("nd:manual", "hfind", "done", "x:", "xr:")):
            self.pending.pop(chat_id, None)
            # Not deleted: the button pressed may have turned one of them
            # into its own card («Cancel» on a rename opens the session).
            self.prompt_cards.pop(chat_id, None)

        if data == "done":
            # The marker left on a settled card: it answers, it does nothing.
            await c.answer(_("Already answered — the button only shows what "
                             "was chosen"))
            return

        if data == "ls":
            await c.answer()
            await self._show_sessions(c)
            return

        if data.startswith("x:"):
            await self._cancel_outgoing(c, msg, data[2:])
            return

        if data.startswith("xr:"):
            await self._reroute_cancelled(c, msg, data[3:])
            return

        if data.startswith("upd:"):
            action = data[4:]
            if action == "show":
                await c.answer()
                await self._show_update(c)
            elif action == "news":
                await c.answer(_("Reading the release notes…"))
                await self._show_news(chat_id)
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

        if data.startswith("hold:"):
            # Text kept out of a session that has a question open.
            await self._held_callback(c, msg, chat_id, data[5:])
            return

        if data.startswith("fwd:"):
            # Where a held forwarded batch goes. Kept above the ordinary
            # pickers because two of its buttons hand over to them.
            await self._route_callback(c, msg, chat_id, data[4:])
            return

        if data == "new":
            await c.answer()
            await self._ask_dir(c)
            return

        if data == "hist":
            await c.answer(_("Looking…"))
            await self._show_history(msg, chat_id)
            return

        if data == "hdirs":
            await c.answer(_("Looking…"))
            await self._show_history_dirs(msg)
            return

        if data.startswith("hd:"):
            key, _sep, page = data[3:].partition(":")
            await c.answer(_("Looking…"))
            await self._show_history(msg, chat_id, key=key,
                                     page=int(page or 0))
            return

        if data == "hfind":
            self.pending[chat_id] = ("hfind", time.time())
            await c.answer()
            await msg.answer(_("🔍 Send a word to look for — it is matched "
                               "against the session's name, its first prompt "
                               "and its last one, in every directory."))
            return

        if data.startswith("hq:"):
            query = self.searches.get(chat_id, "")
            if not query:
                await c.answer(_("That search is gone — press 🔍 again"),
                               show_alert=True)
                return
            await c.answer(_("Looking…"))
            await self._show_search(msg, query, page=int(data[3:] or 0))
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
                self.prompt_cards.pop(chat_id, None)
                await c.answer()
                ask = await msg.answer(
                    _("Send the absolute path of a directory (it starts "
                      "with / or ~).\nOr just press any button to cancel.")
                )
                # The list stays until the path turns out to be real, so a
                # typo still has the buttons to fall back on.
                self._hold_card(chat_id, msg, "picker")
                self._hold_card(chat_id, ask, "prompt")
                return
            await c.answer(_("Starting…"))
            idx = int(arg)
            if 0 <= idx < len(self.dir_choices):
                sid = await self._create(chat_id, self.dir_choices[idx])
                if sid:
                    await self._settle(
                        msg, self._dir_picked(self.dir_choices[idx]), sid)
            return

        if data.startswith("res:"):
            # res:<sid8>[:<dir key>:<page>] — the tail says where "Back" goes;
            # a card from an older build has none, and falls back to the top.
            short, _sep, origin = data[4:].partition(":")
            v = await self._find_closed(short)
            if not v:
                await c.answer(self._closed_gone(short), show_alert=True)
                return
            # "q:<page>" means the card was opened from a search.
            back = "hist"
            if origin.startswith("q:"):
                back = f"hq:{origin[2:]}"
            elif origin:
                back = f"hd:{origin}"
            await c.answer()
            await self._safe_edit(
                msg,
                self._closed_details(v), parse_mode="HTML",
                reply_markup=closed_kb(v.session_id, back=back),
            )
            return

        if data.startswith("dores:"):
            v = await self._find_closed(data[6:])
            if not v:
                await c.answer(self._closed_gone(data[6:]), show_alert=True)
                return
            await c.answer(_("Bringing it up…"))
            # The caption it was picked by goes along as the title, so the
            # card that answers the press calls it by that rather than by its
            # launch name. A saved name needs no help — `Store.add` restores
            # it — and `v.name` is not used directly: with nothing known it
            # is the "untitled" placeholder, which as a title would keep the
            # watcher from ever naming the session by its first prompt.
            sid = await self._create(
                chat_id, v.cwd, resume=v.session_id,
                title=v.launched or v.title or v.opening or None)
            if sid:
                await self._settle(msg, _(
                    "▶️ Resuming <b>{name}</b>\n<code>{path}</code>").format(
                        name=html.escape(v.name[:_CARD_HEAD]),
                        path=html.escape(v.cwd)), sid)
            return

        if data.startswith(("sp:", "s:", "open:")):
            # A row of /sessions (`sp:`) makes its session active at once and
            # turns the list into the session's card. Cards drawn before
            # 2026-11-25 carry `s:` on their rows (and on a picker's Back) and
            # «▶️ Open» (`open:`); they now do the same.
            # A list card sent between 0f4fb67 and its revert says "s:<id>:l".
            mgd = self._resolve(data.partition(":")[2].partition(":")[0])
            if not mgd:
                await c.answer(_("That session is gone"), show_alert=True)
                return
            await self._activate(chat_id, msg, mgd)
            await c.answer(_("Active: {name}").format(name=mgd.full_label))
            await self._show_session_card(c, mgd)
            return

        if data.startswith("sc:"):
            # The «⬅️ Back» of a session's pickers, «🎛 Session controls»:
            # the card, with no switch.
            mgd = self._resolve(data[3:])
            if not mgd:
                await c.answer(_("That session is gone"), show_alert=True)
                return
            await c.answer()
            await self._show_session_card(c, mgd)
            return

        if data.startswith("cfg:"):
            _kind, short, kind = data.split(":", 2)
            mgd = self._resolve(short)
            if not mgd:
                await c.answer(_("That session is gone"), show_alert=True)
                return
            if kind in ("model", "effort"):
                # The list is Claude Code's own (`get_server_info()`): a
                # table in the bot went stale the day Fable 5.1 shipped.
                await self._sdk_picker(c, msg, mgd, kind)
                return
            mode = self.bridge.mode(mgd)
            await c.answer()
            await msg.edit_text(
                _("{title} for <b>{name}</b>\nCurrently: <code>{value}</code>"
                  ).format(title=_("🔐 Permission mode"),
                           name=html.escape(mgd.full_label),
                           value=mode or "—"),
                parse_mode="HTML",
                reply_markup=choice_kb(mgd.session_id, kind, mode),
            )
            return

        if data.startswith(("smd:", "sef:")):
            what, short, value = data.split(":", 2)
            mgd = self._resolve(short)
            if not mgd:
                await c.answer(_("That session is gone"), show_alert=True)
                return
            await self._sdk_pick(c, mgd, what, value)
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

        if data.startswith("svc:"):
            action = data[4:]
            if action == "show":
                await c.answer()
                await self._safe_edit(
                    msg, await self._service_text(), parse_mode="HTML",
                    reply_markup=self._service_kb(),
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
            else:
                await c.answer()
            return

        if data.startswith("ren:"):
            mgd = self._resolve(data[4:])
            if not mgd:
                await c.answer(_("That session is gone"), show_alert=True)
                return
            await c.answer()
            ask = await msg.answer(self._ask_name(chat_id, mgd),
                                   parse_mode="HTML",
                                   reply_markup=cancel_rename_kb(mgd.session_id))
            self._hold_card(chat_id, ask, "question")
            return

        if data.startswith("lang:"):
            await self._set_language(c, data[5:])
            return

        if data.startswith("rq:"):
            short, _sep, act = data[3:].partition(":")
            if act == "pr" or act == "r" or (act[:1] == "t" and act[1:].isdigit()):
                await self._ask_sdk_text(c, msg, chat_id, short, act)
                return
            answered = await self.bridge.callback(c, data[3:])
            if answered is not None and self._focus(chat_id, answered):
                await self._say_focused(msg, answered)
            return

        if data.startswith("alg:"):
            await self._auth_callback(c, msg, data[4:])
            return

        if data.startswith("again:"):
            await self._send_again(c, msg, chat_id, data[6:])
            return

        if data.startswith("k:"):
            _kind, short, action = data.split(":", 2)
            mgd = self._resolve(short)
            if not mgd:
                await c.answer(_("That session is gone"), show_alert=True)
                return
            await self._session_action(c, mgd, action)
            return

        # A button of a card from an older build whose action is gone: said,
        # not swallowed.
        await c.answer(_stale_card("/sessions"), show_alert=True)

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

    async def _find_closed(self, short: str):
        return await sess.closed_view(self.store, short, roots=self.roots)

    def _closed_gone(self, short: str) -> str:
        """Why a session that was on the list a moment ago cannot be resumed.

        Nearly always because it has since been started: a card stays on the
        screen, and "not found" would send the reader looking for a file that
        is right where it was. Resuming a running session is what is refused,
        not the session.
        """
        if self._resolve(short):
            return _("That session is running again — open it in /sessions.")
        return _("Session not found")

    async def _history_dir(self, chat_id: int, key: str) -> str | None:
        """The directory a `hd:` key stands for, or the one to open by default.

        Without a key the active session's directory is the answer: history is
        reached from a session list, and the project being worked in is the one
        being looked for. Only when that directory holds nothing closed does
        the choice have to be made by hand.
        """
        dirs = await sess.closed_dirs(self.store, roots=self.roots)
        if key:
            return next((d.cwd for d in dirs if d.key == key), None)
        active = self.store.get_active(chat_id)
        mgd = self.store.get(active) if active else None
        if mgd and any(d.cwd == mgd.cwd for d in dirs):
            return mgd.cwd
        return None

    async def _show_history(self, msg: Message, chat_id: int, key: str = "",
                            page: int = 0) -> None:
        cwd = await self._history_dir(chat_id, key)
        if cwd is None:
            # Either the key is stale or there is no obvious directory: both
            # end at the same place, the list of directories.
            await self._show_history_dirs(msg)
            return
        views = await sess.closed_all(self.store, self.roots, cwd)
        pages = max(1, -(-len(views) // HISTORY_PAGE))
        page = max(0, min(page, pages - 1))
        shown = views[page * HISTORY_PAGE:(page + 1) * HISTORY_PAGE]
        head = _("🕘 <b>Closed sessions</b> — resuming brings the context "
                 "back.\n📁 <code>{path}</code>").format(
                     path=html.escape(cwd))
        count = ngettext("{n} session", "{n} sessions", len(views)).format(
            n=len(views))
        body = _("{count} · tap one for details — whether to resume it is "
                 "decided there.\n<i>time · topic · live ones are in "
                 "/sessions</i>").format(count=count)
        await self._safe_edit(
            msg, f"{head}\n\n{body}", parse_mode="HTML",
            reply_markup=history_kb(shown, key=sess.dir_key(cwd), page=page,
                                    pages=pages),
        )

    async def _search_from_text(self, m: Message, text: str) -> None:
        """Run a history search typed in answer to the 🔍 button."""
        query = text.strip()
        if len(query) < 2:
            await m.answer(_("Too short to search for — send at least two "
                             "characters."))
            return
        self.searches[m.chat.id] = query
        note = await m.answer(_("🔍 Looking for «{query}»…").format(
            query=html.escape(query)), parse_mode="HTML")
        await self._show_search(note, query, page=0)

    async def _show_search(self, msg: Message, query: str,
                           page: int = 0) -> None:
        views = await sess.closed_all(self.store, self.roots, None, query)
        if not views:
            await self._safe_edit(
                msg,
                _("🔍 Nothing matches «{query}».\n<i>The search covers names "
                  "and the first and last prompt of every closed session — "
                  "not the whole conversation.</i>").format(
                      query=html.escape(query)),
                parse_mode="HTML", reply_markup=search_kb([]),
            )
            return
        pages = max(1, -(-len(views) // HISTORY_PAGE))
        page = max(0, min(page, pages - 1))
        shown = views[page * HISTORY_PAGE:(page + 1) * HISTORY_PAGE]
        count = ngettext("{n} session", "{n} sessions", len(views)).format(
            n=len(views))
        await self._safe_edit(
            msg,
            _("🔍 «{query}» — {count}.\n<i>time · directory · topic</i>").format(
                query=html.escape(query), count=count),
            parse_mode="HTML", reply_markup=search_kb(shown, page, pages),
        )

    async def _show_history_dirs(self, msg: Message) -> None:
        dirs = await sess.closed_dirs(self.store, roots=self.roots)
        if not dirs:
            await self._safe_edit(
                msg,
                _("🕘 No closed sessions found in the project directories."),
                reply_markup=history_dirs_kb([]),
            )
            return
        total = sum(d.count for d in dirs)
        await self._safe_edit(
            msg,
            _("🕘 <b>Closed sessions</b> — {total} in all, by directory.\n"
              "Pick a project to see its own list.").format(total=total),
            parse_mode="HTML", reply_markup=history_dirs_kb(dirs),
        )

    def _asking(self, mgd) -> bool:
        """Does a card stand that «🔔 Show the question» could post again?"""
        return self.bridge.question_open(mgd)

    def _show_question(self, mgd) -> bool:
        """Have the card this session waits on posted again at the bottom.

        The cards are the bridge's, posted again by its tick. False when no
        card stands, i.e. the session is not waiting on a choice.
        """
        return self.bridge.request_repost(mgd)

    def _closed_details(self, v) -> str:
        # A heading is a heading: when the name is only the first prompt, it
        # can run to a paragraph, and the whole of it belongs further down.
        name = v.name if len(v.name) <= _CARD_HEAD else \
            v.name[:_CARD_HEAD].rstrip() + "…"
        head = ("✏️ " if v.saved_name else "") + html.escape(name)
        lines = [f"🕘 <b>{head}</b>", ""]
        lines.append(f"📁 <code>{html.escape(v.cwd)}</code>")
        lines.append(_("🕘 Last active: {when}").format(when=v.when))
        if v.opening and v.opening != name:
            lines.append(_("💬 It started with: {text}").format(
                text=html.escape(v.opening)))
        # And what it stopped on — which is what "where was I" really asks.
        if v.last and v.last != v.opening:
            lines.append(_("⏹ It stopped on: {text}").format(
                text=html.escape(v.last)))
        if v.size:
            lines.append(
                _("📦 Transcript: {size} MB").format(
                    size=f"{v.size / 1024 / 1024:.1f}")
                if v.size >= 1024 * 1024 else
                _("📦 Transcript: {size} KB").format(size=f"{v.size / 1024:.0f}"))
        lines.append(f"🔑 <code>{v.session_id}</code>")
        lines.append("")
        lines.append(_("{button} brings the session back through the session "
                       "host — it picks up all of its context.").format(
                           button="«" + _("▶️ Resume") + "»"))
        return "\n".join(lines)

    async def _account_of(self, mgd) -> str:
        """The account this managed session runs under, "" when not known."""
        return await account.email_for(
            mgd.session_id, since=await sess.started_at(mgd.session_id))

    async def _session_card(
        self, mgd, note: str = "", chat_id: int | None = None,
    ) -> tuple[str, InlineKeyboardMarkup] | None:
        """One managed session's card: its text and its controls.

        Built apart from the sending because the card has two ways in — a tap
        on a row of /sessions edits the list in place, and /menu sends a new
        message. None means the session is no longer in the list.
        """
        views = await sess.managed_views(self.store)
        view = next((v for v in views if v.session_id == mgd.session_id), None)
        if not view:
            return None
        mode = self.bridge.mode(mgd)
        u = status_feed.read(mgd.session_id)
        bits = []
        model = u.model if u else ""
        effort = u.effort if u else ""
        ctx = f"{u.ctx_pct}%" if u and u.ctx_pct is not None else ""
        if model:
            bits.append(f"🧠 {html.escape(model)}")
        if effort:
            bits.append(f"◉ {effort}")
        if mode:
            bits.append(f"🔐 {_mode_label(mode)}")
        if ctx:
            bits.append(f"ctx {ctx}")
        behind = self._stale(mgd, await updates.installed())
        if behind:
            bits.append(f"⬆️ {behind} → {updates.cached_installed()}")
        active = self.store.get_active(chat_id or self.cfg.owner) == mgd.session_id
        head = (_("▶️ Active session: <b>{name}</b>") if active
                else _("▶️ Session: <b>{name}</b>"))
        text = (head.format(name=html.escape(view.name)) + note
                + f"\n<code>{view.short_cwd}</code>\n"
                + _("Status: {status}").format(
                    status=sess.status_label(view.status)))
        if bits:
            text += "\n" + " · ".join(bits)
        await account.fill([view])
        if view.account:
            text += "\n" + account_line(view.account)
        has_transcript = await asyncio.to_thread(
            transcript.find_transcript, mgd.session_id) is not None
        return text, session_kb(view, self._asking(mgd), has_transcript)

    async def _show_session_card(self, c: CallbackQuery, mgd) -> None:
        msg = _editable(c)
        if msg is None:
            await c.answer(_stale_card("/sessions"), show_alert=True)
            return
        card = await self._session_card(mgd, chat_id=msg.chat.id)
        if card is None:
            return
        text, kb = card
        try:
            await self._safe_edit(msg, text, reply_markup=kb, parse_mode="HTML")
            shown = msg
        except Exception:
            shown = await msg.answer(text, reply_markup=kb, parse_mode="HTML")
        # The message is this session's card now: a reply to it means this
        # session. Unregistered, it went to the active one (2026-11-24 19:17).
        self.store.remember_message(shown.message_id, mgd.session_id)

    async def _send_session_card(self, m: Message, mgd, note: str = "") -> None:
        """The same card as a new message — the way /menu opens the controls.

        Registered like every other outgoing message: the card belongs to one
        session, so a reply to it has to reach that one rather than whichever
        happens to be active by then.
        """
        card = await self._session_card(mgd, note, chat_id=m.chat.id)
        if card is None:
            await m.answer(_("That session has already ended — see /sessions"))
            return
        text, kb = card
        msg = await m.answer(text, reply_markup=kb, parse_mode="HTML")
        if msg:
            self.store.remember_message(msg.message_id, mgd.session_id)

    async def _apply_setting(self, mgd, kind: str, value: str) -> bool:
        """The permission mode, switched by the host; "manual" is its "default"."""
        try:
            await self.bridge.link.call("set_mode", name=mgd.name,
                                        mode="default" if value == "manual" else value)
        except HostError as exc:
            log.warning("set_mode failed id=%s: %s", mgd.session_id[:8], exc)
            return False
        return True

    async def _session_action(self, c: CallbackQuery, mgd, action: str) -> None:
        if action == "recap":
            await c.answer()
            await self._send_recap(c.message.chat.id if c.message else self.cfg.owner, mgd)
            return
        if action == "esc":
            await c.answer(_("Interrupting…"))
            await self._interrupt_sdk(_editable(c), mgd, "")
        elif action == "ask":
            if not self._show_question(mgd):
                await c.answer(_("It is not asking anything right now"),
                               show_alert=True)
                return
            await c.answer(_("The question is below"))
        elif action == "screen":
            # A card from before the recap took the screen's place.
            await c.answer()
            await self._send_recap(c.message.chat.id if c.message else self.cfg.owner, mgd)
        elif action == "usage":
            await c.answer()
            await self.bot.send_message(
                c.message.chat.id if c.message else self.cfg.owner,
                usage_report(mgd.full_label, status_feed.read(mgd.session_id),
                             await self._account_of(mgd)),
                parse_mode="HTML",
            )
        elif action == "ctx":
            if not await self._send_command(mgd, "/context"):
                await c.answer(_("It is waiting for an answer — deal with "
                                 "that first"), show_alert=True)
                return
            await c.answer(_("/context sent — the breakdown comes in a moment"))
        elif action == "restart":
            # Answered first: the resume takes seconds, and a button that
            # seems to do nothing gets pressed again.
            await c.answer(_("Restarting…"))
            await self._restart_one(
                c.message.chat.id if c.message else self.cfg.owner, mgd)
        elif action == "compact":
            # Claude Code's own /compact: it summarises the context in place
            # and keeps the session id, so nothing has to be rebound.
            if not await self._send_command(mgd, "/compact"):
                await c.answer(_("It is waiting for an answer — deal with "
                                 "that first"), show_alert=True)
                return
            await c.answer(_("/compact sent — the chat is told when it is done"))
        elif action == "clear":
            if not await self._send_command(mgd, "/clear"):
                await c.answer(_("It is waiting for an answer — deal with "
                                 "that first"), show_alert=True)
                return
            await c.answer(_("/clear sent"))
        elif action == "login":
            # The same as /login sent as a reply to this session.
            await c.answer()
            here = _editable(c)
            if here is not None:
                await self._start_auth_login(here, mgd)
        elif action == "close":
            # Answered first: the host's shutdown can outlast Telegram's
            # patience with an unanswered callback, and a button that does
            # nothing for eight seconds gets pressed again.
            await c.answer(_("Ending it…"))
            name = mgd.full_label
            if not await self._close(mgd.session_id):
                await self.bot.send_message(
                    c.message.chat.id if c.message else self.cfg.owner,
                    _close_failed(name), parse_mode="HTML")
                return
            await self._show_sessions(c)
            await self._say_ended(c, name)
        elif action == "quit":
            # «🚪 End» under a recap: the recap stays readable (the list is
            # not drawn over it), and a session that went back to work since
            # is not cut off.
            if self.bridge.state(mgd) in ("busy", "waiting"):
                await c.answer(_("{name} is working again — it was not ended.").format(
                    name=mgd.full_label), show_alert=True)
                return
            await c.answer(_("Ending it…"))
            name = mgd.full_label
            if not await self._close(mgd.session_id):
                await self.bot.send_message(
                    c.message.chat.id if c.message else self.cfg.owner,
                    _close_failed(name), parse_mode="HTML")
                return
            here = _editable(c)
            if here is not None:
                await self._mark(here, _("🚪 Ended"))
            await self._say_ended(c, name)
        else:
            await c.answer(_stale_card("/sessions"), show_alert=True)

    async def _say_ended(self, c: CallbackQuery, name: str) -> None:
        await self.bot.send_message(
            c.message.chat.id if c.message else self.cfg.owner,
            _("<b>❌ Session {name} has ended</b>\n\n"
              "The transcript stays — it can be brought back from 🕘 in "
              "the list.").format(name=html.escape(name)),
            parse_mode="HTML",
        )

    # ------------------------------------------------------------------- run
    # Shown in Telegram's "/" menu, so the commands are discoverable.
    # Marked here, translated in _publish_menu: a class body runs at import
    # time, when no language has been chosen yet.
    MENU = [
        ("sessions", N_("Session list and controls")),
        ("menu", N_("Controls of the session you are talking to")),
        ("new", N_("New session in a chosen directory")),
        ("usage", N_("Quota and context, without asking Claude")),
        ("screen", N_("Where the session stopped")),
        ("esc", N_("Interrupt what Claude is doing")),
        ("clear", N_("Clear the active session's context")),
        ("rename", N_("Give a session your own name")),
        ("exit", N_("End the active session cleanly")),
        ("dirs", N_("Project directories for new sessions")),
        ("lang", N_("Interface language")),
        ("log", N_("Last entries from the bot's journal")),
        ("update", N_("Claude Code version and session restarts")),
        ("login", N_("Log Claude in to another account")),
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
                _("<b>✅ The bot is back — the restart took {duration}.</b>\n\n"
                  "Code: <code>{version}</code>").format(
                      duration=took, version=html.escape(service.version())),
                parse_mode="HTML",
            )
        # Resolve the CLI up front: if it is missing, every session list would
        # silently come back empty, which is far harder to notice later.
        if not sess.claude_bin():
            await self.bot.send_message(
                self.cfg.owner,
                _("<b>⚠️ I could not find the claude executable</b>\n\n"
                  "The Claude Code version on disk cannot be read, and the "
                  "session host most likely cannot start sessions either.\n\n"
                  "Set the path in .env: "
                  "<code>CCBOT_CLAUDE_BIN=/path/to/claude</code>"),
                parse_mode="HTML",
            )
        dropped = media.cleanup()
        if dropped:
            log.info("removed %d stale attachments", dropped)
        await self._recover_delayed()
        self.watcher.start()
        self.bridge.start()
        log.info("polling started")
        try:
            await self.dp.start_polling(self.bot, handle_signals=False)
        finally:
            await self.bridge.stop()
            await self.watcher.stop()
            await self.bot.session.close()
