"""Inline keyboards. Callback payloads stay well under Telegram's 64-byte cap."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .i18n import _, language_name, offered
from .screen import Dialog
from .sessions import DirStat, SessionView

# Aliases accepted by `/model`; the CLI resolves them to the latest release.
MODELS = [
    ("Opus 5", "opus"),
    ("Opus 5 · 1M", "opus[1m]"),
    ("Sonnet 5", "sonnet"),
    ("Fable 5", "fable"),
    ("Haiku 4.5", "haiku"),
]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
# Shift+Tab cycles in this order; the bot presses it until the target is shown.
MODES = [
    ("⏵⏵ auto", "auto"),
    ("⏸ plan", "plan"),
    ("⏵⏵ accept edits", "acceptEdits"),
    ("⏸ manual", "manual"),
]

STATUS_ICON = {
    "busy": "⚡",
    "idle": "✅",
    "waiting": "⏸",
    "starting": "🔄",
    "dead": "💀",
    "gone": "❓",
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


def sessions_kb(managed: list[SessionView], foreign: list[SessionView],
                active: str | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    # The two kinds look alike in a list of rows, so they are split by a
    # divider row. Telegram centres a caption, which makes dashes read as a
    # separator; tapping one explains the group rather than doing nothing.
    if managed:
        kb.row(InlineKeyboardButton(
            text=_("──── 🖥 in tmux ────"), callback_data="grp:tmux"))
    for v in managed:
        icon = STATUS_ICON.get(v.status, "•")
        label = _distinct_name(v.name, v.dir_name)
        hint = _("waiting") if v.status == "waiting" else ""
        # ▶️ marks where plain text goes; everything else needs a reply.
        mark = "▶️" if v.session_id == active else icon
        kb.row(InlineKeyboardButton(
            text=_fit(f"{mark} {v.dir_name} · {label}", hint),
            callback_data=f"s:{sid8(v.session_id)}",
        ))
    if foreign:
        kb.row(InlineKeyboardButton(
            text=_("──── 🔗 in your terminal ────"), callback_data="grp:term"))
    for v in foreign:
        # 🔗 means "not mine yet" — opening it moves the session into tmux.
        hint = _("waiting") if v.status == "waiting" else ""
        kb.row(InlineKeyboardButton(
            text=_fit(f"🔗 {v.dir_name} · {v.name}", hint),
            callback_data=f"f:{sid8(v.session_id)}",
        ))
    kb.row(
        InlineKeyboardButton(text=_("➕ New session"), callback_data="new"),
        InlineKeyboardButton(text=_("🕘 Recent"), callback_data="hist"),
    )
    kb.row(InlineKeyboardButton(text=_("🔄 Refresh"), callback_data="ls"))
    return kb.as_markup()


def confirm_kb(session_id: str, kind: str,
               back: str = "hist") -> InlineKeyboardMarkup:
    """Detail card actions: a button caption is one line, a card is not."""
    s = sid8(session_id)
    kb = InlineKeyboardBuilder()
    if kind == "closed":
        kb.row(
            InlineKeyboardButton(text=_("▶️ Resume"), callback_data=f"dores:{s}"),
            InlineKeyboardButton(text=_("⬅️ Back"), callback_data=back),
        )
    elif kind == "foreign":
        kb.row(
            InlineKeyboardButton(text=_("🔗 Move into tmux"), callback_data=f"grab:{s}"),
            InlineKeyboardButton(text=_("⬅️ Back"), callback_data="ls"),
        )
    else:
        kb.row(
            InlineKeyboardButton(text=_("▶️ Open"), callback_data=f"open:{s}"),
            InlineKeyboardButton(text=_("⬅️ Back"), callback_data="ls"),
        )
    return kb.as_markup()


def session_kb(v: SessionView) -> InlineKeyboardMarkup:
    s = sid8(v.session_id)
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="⏸ Esc", callback_data=f"k:{s}:esc"),
        InlineKeyboardButton(text=_("🖥 Screen"), callback_data=f"k:{s}:screen"),
        InlineKeyboardButton(text=_("📊 Limits"), callback_data=f"k:{s}:usage"),
    )
    kb.row(
        InlineKeyboardButton(text="🧹 /clear", callback_data=f"k:{s}:clear"),
        InlineKeyboardButton(text=_("📉 Context breakdown"),
                             callback_data=f"k:{s}:ctx"),
        InlineKeyboardButton(text=_("🚪 End"), callback_data=f"k:{s}:close"),
    )
    kb.row(
        InlineKeyboardButton(text=_("🧠 Model"), callback_data=f"cfg:{s}:model"),
        InlineKeyboardButton(text=_("◉ Effort"), callback_data=f"cfg:{s}:effort"),
        InlineKeyboardButton(text=_("🔐 Mode"), callback_data=f"cfg:{s}:mode"),
    )
    kb.row(
        InlineKeyboardButton(text=_("✏️ Rename"), callback_data=f"ren:{s}"),
        InlineKeyboardButton(text=_("⬅️ Sessions"), callback_data="ls"),
    )
    return kb.as_markup()


def choice_kb(session_id: str, kind: str, current: str = "") -> InlineKeyboardMarkup:
    """Picker for model / effort / permission mode."""
    s = sid8(session_id)
    kb = InlineKeyboardBuilder()
    if kind == "model":
        items = MODELS
    elif kind == "effort":
        items = [(e, e) for e in EFFORTS]
    else:
        items = MODES
    for label, value in items:
        mark = "✅ " if value == current or (label.endswith(current) and current) else ""
        kb.row(InlineKeyboardButton(
            text=f"{mark}{label}", callback_data=f"set:{s}:{kind}:{value}",
        ))
    kb.row(InlineKeyboardButton(text=_("⬅️ Back"), callback_data=f"s:{s}"))
    return kb.as_markup()


def dialog_kb(session_id: str, dialog: Dialog) -> InlineKeyboardMarkup:
    """One button per option; free-text options route to a reply prompt."""
    s = sid8(session_id)
    kb = InlineKeyboardBuilder()
    for opt in dialog.options:
        label = opt.label if len(opt.label) <= 48 else opt.label[:47] + "…"
        if opt.is_free_text:
            kb.row(InlineKeyboardButton(
                text=f"✏️ {label}", callback_data=f"dt:{s}:{opt.number}",
            ))
        else:
            mark = "▸ " if opt.selected else ""
            kb.row(InlineKeyboardButton(
                text=f"{mark}{opt.number}. {label}", callback_data=f"d:{s}:{opt.number}",
            ))
    # Arrow keys reach options the digit shortcuts cannot: unnumbered entries
    # like "Chat about this", and previews that only render for the highlighted
    # row.
    nav = [
        InlineKeyboardButton(text="⬆️", callback_data=f"nav:{s}:up"),
        InlineKeyboardButton(text="⬇️", callback_data=f"nav:{s}:down"),
        InlineKeyboardButton(text=_("✅ Select"), callback_data=f"nav:{s}:enter"),
    ]
    kb.row(*nav)
    bottom = [InlineKeyboardButton(text="⏸ Esc", callback_data=f"k:{s}:esc")]
    if dialog.preview:
        bottom.append(InlineKeyboardButton(text=_("🖼 Diagram"), callback_data=f"pic:{s}"))
    bottom.append(InlineKeyboardButton(text=_("🖥 Screen"), callback_data=f"k:{s}:screen"))
    kb.row(*bottom)
    return kb.as_markup()


def blocked_kb(session_id: str) -> InlineKeyboardMarkup:
    """Controls for a session that is waiting on something we could not parse.

    The keys are the ones that work without knowing the question: digits pick
    a numbered option, the arrows walk the list, Esc backs out.
    """
    s = sid8(session_id)
    kb = InlineKeyboardBuilder()
    kb.row(*[InlineKeyboardButton(text=str(n), callback_data=f"d:{s}:{n}")
             for n in (1, 2, 3)])
    kb.row(
        InlineKeyboardButton(text="⬆️", callback_data=f"nav:{s}:up"),
        InlineKeyboardButton(text="⬇️", callback_data=f"nav:{s}:down"),
        InlineKeyboardButton(text=_("✅ Select"), callback_data=f"nav:{s}:enter"),
    )
    kb.row(
        InlineKeyboardButton(text="⏸ Esc", callback_data=f"k:{s}:esc"),
        InlineKeyboardButton(text=_("🖥 Screen"), callback_data=f"k:{s}:screen"),
    )
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


def cancel_rename_kb(session_id: str) -> InlineKeyboardMarkup:
    """A way out of "send me a name" that does not need a message.

    Any button clears a half-finished prompt (see `_on_callback`), so going
    back to the card is itself the cancellation.
    """
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text=_("⬅️ Cancel"),
                                callback_data=f"s:{sid8(session_id)}"))
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
