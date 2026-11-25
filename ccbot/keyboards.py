"""Inline keyboards. Callback payloads stay well under Telegram's 64-byte cap."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .i18n import _, language_name, offered
from .sessions import DirStat, SessionView

# Models and effort levels are NOT listed here. They come from the session's
# own `get_server_info()` (`CCBot._sdk_picker`), because a table in the source
# is a table that goes stale.
# The permission modes, in the order the picker offers them.
MODES = [
    ("⏵⏵ auto", "auto"),
    ("⏸ plan", "plan"),
    ("⏵⏵ accept edits", "acceptEdits"),
    ("⏸ manual", "manual"),
]

# The one thing a glance at the list has to find is where plain text goes, so
# the active session gets the only green mark on it; the idle state, the most
# common one, stays grey ("☑️", not "✅") and does not compete with it.
ACTIVE_ICON = "🟢"

STATUS_ICON = {
    "busy": "⚡",
    "idle": "☑️",
    "waiting": "⏸",
    "starting": "🔄",
    "closed": "🕘",
}


# Telegram truncates long button captions, and a phone shows fewer characters
# than a desktop — so trim deliberately instead of letting it happen at random.
_BUTTON_CHARS = 46


def _fit(text: str, suffix: str = "") -> str:
    tail = f" · {suffix}" if suffix else ""
    room = _BUTTON_CHARS - len(tail)
    if len(text) > room:
        text = text[:room - 1].rstrip() + "…"
    return text + tail


def _distinct_name(name: str, folder: str) -> str:
    """Drop the folder prefix the bot itself put in the session name.

    "finman · finman-a3f2" wastes the little space a button has; the folder is
    already the first field.
    """
    for sep in ("-", "_", " "):
        prefix = folder + sep
        if name.startswith(prefix) and len(name) > len(prefix):
            return name[len(prefix):]
    return name


def sid8(session_id: str) -> str:
    return session_id[:8]


def _named(v: SessionView) -> str:
    """A row's name, marked when it is the one the user gave it themselves."""
    return f"✏️ {v.name}" if v.saved_name else v.name


def sessions_kb(managed: list[SessionView],
                active: str | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for v in managed:
        icon = STATUS_ICON.get(v.status, "•")
        label = _distinct_name(v.name, v.dir_name)
        hint = _("waiting") if v.status == "waiting" else ""
        # 🟢 marks where plain text goes; everything else needs a reply.
        # A tap makes the session active at once (`sp:`): a separate «Open»
        # step drew its button on the list, often far up the chat, and text
        # typed next went to the old session (2026-11-25).
        mark = ACTIVE_ICON if v.session_id == active else icon
        kb.row(InlineKeyboardButton(
            text=_fit(f"{mark} {v.dir_name} · {label}", hint),
            callback_data=f"sp:{sid8(v.session_id)}",
        ))
    kb.row(
        InlineKeyboardButton(text=_("➕ New session"), callback_data="new"),
        InlineKeyboardButton(text=_("🕘 Recent"), callback_data="hist"),
    )
    kb.row(InlineKeyboardButton(text=_("🔄 Refresh"), callback_data="ls"))
    return kb.as_markup()


def closed_kb(session_id: str, back: str = "hist") -> InlineKeyboardMarkup:
    """A closed session's detail card: resume it, or go back to the list."""
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text=_("▶️ Resume"), callback_data=f"dores:{sid8(session_id)}"),
        InlineKeyboardButton(text=_("⬅️ Back"), callback_data=back),
    )
    return kb.as_markup()


def reroute_kb(tok: str) -> InlineKeyboardMarkup:
    """«📋 To another session» on a cancelled batch: no typing it again."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text=_("📋 To another session"), callback_data=f"xr:{tok}"))
    return kb.as_markup()


def cancel_kb(tok: str) -> InlineKeyboardMarkup:
    """«↩️ Cancel» on a sent message the session has not taken in yet."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text=_("↩️ Cancel"), callback_data=f"x:{tok}"))
    return kb.as_markup()


def session_kb(v: SessionView, asking: bool = False,
               has_transcript: bool = True) -> InlineKeyboardMarkup:
    """A session's controls. «📜 Where it stopped» is the recap read from its
    transcript — there is no screen to show — so a session that has not
    written one yet (a fresh launch) does not get the button."""
    s = sid8(v.session_id)
    kb = InlineKeyboardBuilder()
    if asking:
        # The session waits on a question whose card may be far up the chat
        # by now; this sends it again at the bottom (`CCBot._show_question`).
        kb.row(InlineKeyboardButton(text=_("🔔 Show the question"),
                                    callback_data=f"k:{s}:ask"))
    if has_transcript:
        kb.row(InlineKeyboardButton(text=_("📜 Where it stopped"),
                                    callback_data=f"k:{s}:recap"))
    kb.row(InlineKeyboardButton(text="⏸ Esc", callback_data=f"k:{s}:esc"),
           InlineKeyboardButton(text=_("📊 Limits"), callback_data=f"k:{s}:usage"))
    kb.row(InlineKeyboardButton(text="🧹 /clear", callback_data=f"k:{s}:clear"),
           InlineKeyboardButton(text="🗜 /compact", callback_data=f"k:{s}:compact"),
           InlineKeyboardButton(text=_("🔁 Restart"), callback_data=f"k:{s}:restart"))
    kb.row(InlineKeyboardButton(text=_("🧠 Model"), callback_data=f"cfg:{s}:model"),
           InlineKeyboardButton(text=_("◉ Effort"), callback_data=f"cfg:{s}:effort"),
           InlineKeyboardButton(text=_("🔐 Mode"), callback_data=f"cfg:{s}:mode"))
    kb.row(InlineKeyboardButton(text=_("✏️ Rename"), callback_data=f"ren:{s}"),
           InlineKeyboardButton(text=_("🔑 Other account"), callback_data=f"k:{s}:login"),
           InlineKeyboardButton(text=_("🚪 End"), callback_data=f"k:{s}:close"))
    kb.row(InlineKeyboardButton(text=_("⬅️ Sessions"), callback_data="ls"))
    return kb.as_markup()


def choice_kb(session_id: str, kind: str, current: str = "") -> InlineKeyboardMarkup:
    """Picker for the permission mode — the one setting with no dialog of its own."""
    s = sid8(session_id)
    kb = InlineKeyboardBuilder()
    items = MODES
    for label, value in items:
        mark = "✅ " if value == current or (label.endswith(current) and current) else ""
        kb.row(InlineKeyboardButton(
            text=f"{mark}{label}", callback_data=f"set:{s}:{kind}:{value}",
        ))
    kb.row(InlineKeyboardButton(text=_("⬅️ Back"), callback_data=f"sc:{s}"))
    return kb.as_markup()


def route_kb(active: str = "") -> InlineKeyboardMarkup:
    """Where a forwarded batch should go — asked before it is sent anywhere.

    Forwarding a conversation is how a session is *started* on somebody
    else's words, so the destination is a choice, not the session that
    happens to be active. The active one is still offered first: it is the
    one-tap answer, and its name is on the button because "current" alone
    does not say which project that is.
    """
    kb = InlineKeyboardBuilder()
    if active:
        kb.row(InlineKeyboardButton(
            text=_fit(_("▶️ Current: {name}").format(name=active)),
            callback_data="fwd:cur"))
    kb.row(InlineKeyboardButton(text=_("📋 Another session"),
                                callback_data="fwd:list"))
    kb.row(InlineKeyboardButton(text=_("🗑 Discard"), callback_data="fwd:drop"))
    return kb.as_markup()


def route_pick_kb(managed: list[SessionView], active: str | None = None,
                  back: bool = True) -> InlineKeyboardMarkup:
    """The session list, but every row is a destination for the held batch.

    A new session and the recent ones are both offered, because "the
    context to start from" usually means exactly that — a session that does
    not exist yet, or one to come back to.
    Without *back* the list is the question itself (no session is active, so
    there is no shorter card to go back to) and ends in «🗑 Discard» instead.
    """
    kb = InlineKeyboardBuilder()
    for v in managed:
        icon = STATUS_ICON.get(v.status, "•")
        label = _distinct_name(v.name, v.dir_name)
        mark = ACTIVE_ICON if v.session_id == active else icon
        kb.row(InlineKeyboardButton(
            text=_fit(f"{mark} {v.dir_name} · {label}",
                      _("waiting") if v.status == "waiting" else ""),
            callback_data=f"fwd:to:{sid8(v.session_id)}",
        ))
    kb.row(
        InlineKeyboardButton(text=_("➕ New session"), callback_data="fwd:new"),
        InlineKeyboardButton(text=_("🕘 Recent"), callback_data="fwd:hist"),
    )
    if back:
        kb.row(InlineKeyboardButton(text=_("⬅️ Back"),
                                    callback_data="fwd:back"))
    else:
        kb.row(InlineKeyboardButton(text=_("🗑 Discard"),
                                    callback_data="fwd:drop"))
    return kb.as_markup()


def menu_kb(session_id: str) -> InlineKeyboardMarkup:
    """A single way into the session's controls.

    Sent under the "context cleared" note: the cards printed before the clear
    are answered by the alias table, but the new id deserves a card of its
    own — the buttons a person reaches for after a clear are right here.
    """
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text=_("🎛 Session controls"),
                                callback_data=f"sc:{sid8(session_id)}"))
    return kb.as_markup()


def service_kb(can_restart: bool) -> InlineKeyboardMarkup:
    """Bot-process actions. Restart is offered only when something can bring
    the bot back — otherwise the button would end the conversation."""
    kb = InlineKeyboardBuilder()
    if can_restart:
        kb.row(InlineKeyboardButton(text=_("🔄 Restart the bot"),
                                    callback_data="svc:ask"))
    kb.row(
        InlineKeyboardButton(text=_("♻️ Refresh"), callback_data="svc:show"),
        InlineKeyboardButton(text=_("⬅️ Sessions"), callback_data="ls"),
    )
    return kb.as_markup()


def sdk_models_kb(session_id: str, models: list[dict],
                  current: str) -> InlineKeyboardMarkup:
    """The models an SDK session can switch to, as Claude Code lists them."""
    s = sid8(session_id)
    kb = InlineKeyboardBuilder()
    # One mark: "Default" resolves to a model listed in its own right, and
    # two ticks read as two choices in force.
    hits = [m for m in models if m.get("resolvedModel") == current]
    chosen = next((m for m in hits if m.get("value") != "default"), hits[0] if hits else None)
    for m in models:
        value = str(m.get("value") or "")
        mark = "✅ " if m is chosen else ""
        if value and len(f"smd:{s}:{value}".encode()) <= 64:
            kb.row(InlineKeyboardButton(text=f"{mark}{m.get('displayName') or value}"[:60],
                                        callback_data=f"smd:{s}:{value}"))
    kb.row(InlineKeyboardButton(text=_("⬅️ Back"), callback_data=f"sc:{s}"))
    return kb.as_markup()


def sdk_effort_kb(session_id: str, levels: list[str], now: str) -> InlineKeyboardMarkup:
    """The effort levels the session's model takes."""
    s = sid8(session_id)
    kb = InlineKeyboardBuilder()
    for lv in levels:
        kb.row(InlineKeyboardButton(text=("✅ " if lv == now else "") + lv,
                                    callback_data=f"sef:{s}:{lv}"))
    kb.row(InlineKeyboardButton(text=_("⬅️ Back"), callback_data=f"sc:{s}"))
    return kb.as_markup()


def auth_link_kb(url: str, serial: int) -> InlineKeyboardMarkup:
    """The sign-in page of `claude auth login`, and a way out of that one login."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text=_("🔑 Open the sign-in page"), url=url))
    kb.row(InlineKeyboardButton(text=_("✖️ Cancel"), callback_data=f"alg:cancel:{serial}"))
    return kb.as_markup()


def auth_retry_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text=_("🔁 Try again with a new link"),
                                callback_data="alg:retry"))
    return kb.as_markup()


def recap_kb(session_id: str, asking: bool, can_end: bool) -> InlineKeyboardMarkup | None:
    """Under «📜 Where it stopped»: «🔔 Show the question» for a waiting
    session, «🚪 End» for one that has finished — a recap is mostly read to
    make sure the work is done, and then the session is left (2026-11-25)."""
    kb = InlineKeyboardBuilder()
    if asking:
        kb.row(InlineKeyboardButton(text=_("🔔 Show the question"),
                                    callback_data=f"k:{sid8(session_id)}:ask"))
    if can_end:
        kb.row(InlineKeyboardButton(text=_("🚪 End"),
                                    callback_data=f"k:{sid8(session_id)}:quit"))
    return kb.as_markup() if asking or can_end else None


def held_kb(session_id: str, free_text: bool) -> InlineKeyboardMarkup:
    """What to do with text sent while the session has a question open.

    Sent along it would land in the middle of the question, so it waits for
    one of these. "This answers the question" is offered only for a question
    that takes a typed answer, and for a single batch.
    """
    sid = sid8(session_id)
    kb = InlineKeyboardBuilder()
    if free_text:
        kb.row(InlineKeyboardButton(text=_("✏️ This answers the question"),
                                    callback_data=f"hold:ans:{sid}"))
    kb.row(InlineKeyboardButton(text=_("💬 Close the question and send"),
                                callback_data=f"hold:esc:{sid}"))
    kb.row(InlineKeyboardButton(text=_("⏳ Send after the answer"),
                                callback_data=f"hold:wait:{sid}"))
    kb.row(InlineKeyboardButton(text=_("🗑 Don't send"),
                                callback_data=f"hold:drop:{sid}"))
    return kb.as_markup()


def held_wait_kb(session_id: str) -> InlineKeyboardMarkup:
    """The waiting line keeps a way out: a decision taken is not a trap."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text=_("🗑 Don't send"),
                                callback_data=f"hold:drop:{sid8(session_id)}"))
    return kb.as_markup()


def done_kb(label: str) -> InlineKeyboardMarkup:
    """The keyboard of a settled card: one button that says how it settled.

    Stripping the buttons alone leaves a question with no answer on it, and
    the chat no longer says what happened; the button keeps the choice in
    view and answers a tap with a note (`done` does nothing else).
    """
    label = " ".join(label.split())
    if len(label) > 60:
        label = label[:59].rstrip() + "…"
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text=label, callback_data="done"))
    return kb.as_markup()


def cancel_rename_kb(session_id: str) -> InlineKeyboardMarkup:
    """A way out of "send me a name" that does not need a message.

    Any button clears a half-finished prompt (see `_on_callback`), so going
    back to the card is itself the cancellation.
    """
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text=_("⬅️ Cancel"),
                                callback_data=f"sc:{sid8(session_id)}"))
    return kb.as_markup()


def update_kb(session_id: str | None, outdated: int) -> InlineKeyboardMarkup:
    """Actions on the Claude Code version card.

    A restart button appears only for a session that is both behind and idle:
    offering it for one that is working would promise something the bot then
    refuses to do.
    """
    kb = InlineKeyboardBuilder()
    if session_id:
        kb.row(InlineKeyboardButton(
            text=_("⬆️ Restart this session"),
            callback_data=f"upd:one:{sid8(session_id)}"))
    # With a "this session" button already there, "all" is only worth showing
    # when it would do more than that button does.
    if outdated > (1 if session_id else 0):
        kb.row(InlineKeyboardButton(
            text=_("⬆️ Restart all idle ({count})").format(count=outdated),
            callback_data="upd:all"))
    kb.row(
        InlineKeyboardButton(text=_("🆕 What's new"), callback_data="upd:news"),
        InlineKeyboardButton(text=_("🔄 Check for updates"),
                             callback_data="upd:check"),
    )
    kb.row(
        InlineKeyboardButton(text=_("♻️ Refresh"), callback_data="upd:show"),
        InlineKeyboardButton(text=_("⬅️ Sessions"), callback_data="ls"),
    )
    return kb.as_markup()


def update_notice_kb() -> InlineKeyboardMarkup:
    """The one button the "a new version is out" message needs."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text=_("⬆️ Update sessions"),
                                callback_data="upd:show"))
    return kb.as_markup()


def restart_confirm_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text=_("✅ Yes, restart"), callback_data="svc:go"),
        InlineKeyboardButton(text=_("⬅️ Cancel"), callback_data="svc:show"),
    )
    return kb.as_markup()


def project_dirs_kb(dirs: list[str], discovered: bool) -> InlineKeyboardMarkup:
    """Manage the shortlist of project directories."""
    kb = InlineKeyboardBuilder()
    home = str(__import__("pathlib").Path.home())
    for i, d in enumerate(dirs):
        label = ("~" + d[len(home):]) if d.startswith(home) else d
        kb.row(InlineKeyboardButton(
            text=f"➖ {label}", callback_data=f"rmdir:{i}",
        ))
    kb.row(InlineKeyboardButton(text=_("➕ Add a directory"), callback_data="adddir"))
    if dirs:
        kb.row(InlineKeyboardButton(text=_("🔄 Reset to autodiscovery"),
                                    callback_data="cleardirs"))
    kb.row(InlineKeyboardButton(text=_("⬅️ Sessions"), callback_data="ls"))
    return kb.as_markup()


def dirs_kb(dirs: list[str]) -> InlineKeyboardMarkup:
    """The folders a new session can start in."""
    kb = InlineKeyboardBuilder()
    home = str(__import__("pathlib").Path.home())
    for i, d in enumerate(dirs):
        label = ("~" + d[len(home):]) if d.startswith(home) else d
        kb.row(InlineKeyboardButton(text=f"📁 {label}", callback_data=f"nd:{i}"))
    kb.row(InlineKeyboardButton(text=_("✏️ Another path"), callback_data="nd:manual"))
    kb.row(InlineKeyboardButton(text=_("⬅️ Cancel"), callback_data="ls"))
    return kb.as_markup()


def lang_kb(current: str, following: bool = True) -> InlineKeyboardMarkup:
    """Languages the bot can speak, each written in itself.

    A picker that says "Ukrainian" is no use to someone who cannot read
    English, so the endonym is the caption and never goes through gettext.

    Following the Telegram profile is one of the choices, not a state you can
    only leave: the card says that is the default, so it has to be reachable
    again after picking a language.
    """
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text=("✅ " if following else "") + _("🌐 Same as Telegram"),
        callback_data="lang:auto",
    ))
    for code in offered():
        mark = "✅ " if not following and code == current else ""
        kb.row(InlineKeyboardButton(text=f"{mark}{language_name(code)}",
                                    callback_data=f"lang:{code}"))
    kb.row(InlineKeyboardButton(text=_("⬅️ Sessions"), callback_data="ls"))
    return kb.as_markup()


# One page of history. Ten rows plus the navigation still fits a phone
# screen without turning the keyboard into a scroll of its own.
HISTORY_PAGE = 8


def history_kb(views: list[SessionView], key: str = "", page: int = 0,
               pages: int = 1) -> InlineKeyboardMarkup:
    """One page of one directory's closed sessions.

    Without *key* the caption carries the directory, because the list then
    spans several of them.
    """
    kb = InlineKeyboardBuilder()
    for v in views:
        # Inside one directory the path is the same on every row: spending a
        # third of the caption on it would cost the part that tells them apart.
        where = "" if key else f" · {v.dir_name}"
        kb.row(InlineKeyboardButton(
            text=_fit(f"{v.when_short}{where} · {_named(v)}"),
            callback_data=f"res:{sid8(v.session_id)}:{key}:{page}",
        ))
    if key and pages > 1:
        row = []
        if page > 0:
            row.append(InlineKeyboardButton(
                text="◀️", callback_data=f"hd:{key}:{page - 1}"))
        row.append(InlineKeyboardButton(
            text=_("page {page} of {pages}").format(page=page + 1, pages=pages),
            callback_data=f"hd:{key}:{page}"))
        if page + 1 < pages:
            row.append(InlineKeyboardButton(
                text="▶️", callback_data=f"hd:{key}:{page + 1}"))
        kb.row(*row)
    kb.row(
        InlineKeyboardButton(text=_("🔍 Find"), callback_data="hfind"),
        InlineKeyboardButton(text=_("📁 Another directory"),
                             callback_data="hdirs"),
    )
    kb.row(InlineKeyboardButton(text=_("⬅️ Sessions"), callback_data="ls"))
    return kb.as_markup()


def search_kb(views: list[SessionView], page: int = 0,
              pages: int = 1) -> InlineKeyboardMarkup:
    """Search results: every directory at once, so each row carries its own."""
    kb = InlineKeyboardBuilder()
    for v in views:
        kb.row(InlineKeyboardButton(
            text=_fit(f"{v.when_short} · {v.dir_name} · {_named(v)}"),
            callback_data=f"res:{sid8(v.session_id)}:q:{page}",
        ))
    if pages > 1:
        row = []
        if page > 0:
            row.append(InlineKeyboardButton(text="◀️", callback_data=f"hq:{page - 1}"))
        row.append(InlineKeyboardButton(
            text=_("page {page} of {pages}").format(page=page + 1, pages=pages),
            callback_data=f"hq:{page}"))
        if page + 1 < pages:
            row.append(InlineKeyboardButton(text="▶️", callback_data=f"hq:{page + 1}"))
        kb.row(*row)
    kb.row(
        InlineKeyboardButton(text=_("🔍 Search again"), callback_data="hfind"),
        InlineKeyboardButton(text=_("📁 Directories"), callback_data="hdirs"),
    )
    kb.row(InlineKeyboardButton(text=_("⬅️ Sessions"), callback_data="ls"))
    return kb.as_markup()


def history_dirs_kb(dirs: list[DirStat]) -> InlineKeyboardMarkup:
    """Directories that hold closed sessions, with how many and how recent."""
    kb = InlineKeyboardBuilder()
    for d in dirs:
        kb.row(InlineKeyboardButton(
            text=_fit(f"📁 {d.dir_name} · {d.count}", d.when_short),
            callback_data=f"hd:{d.key}:0",
        ))
    kb.row(InlineKeyboardButton(text=_("🔍 Find"), callback_data="hfind"))
    kb.row(InlineKeyboardButton(text=_("⬅️ Sessions"), callback_data="ls"))
    return kb.as_markup()
