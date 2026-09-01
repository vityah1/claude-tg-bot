"""Parser for the Claude Code TUI as captured by ``tmux capture-pane``.

This is the only module that knows what the terminal UI looks like. If a future
Claude Code release changes its dialogs, the damage is contained here — the
sample files under tests/samples/ are captured from real sessions and pin the
current shapes down.

Design note: dialogs are recognised by their *footer* ("Enter to select …"),
never by the mere presence of numbered lines — assistant prose is full of
numbered lists and would produce constant false positives.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass, field

# Footer lines that mark an open, blocking dialog.
_FOOTER_MARKERS = (
    "Enter to select",
    "Enter to confirm",
    "to navigate",
)
_ESC_MARKER = "Esc to cancel"
# Permission prompts (a tool asking to run, a PreToolUse hook demanding
# confirmation) carry none of the markers above — their footer reads
# "Esc to cancel · Tab to amend · ctrl+e to explain". Missing them left the
# session blocked with nothing said in the chat.
_ESC_COMPANIONS = ("select", "amend", "explain", "confirm", "interrupt")

# The review step of a multi-part question (AskUserQuestion with several
# sections) has no footer at all: it lists the answers so far, this line, and
# two choices. That line is the only anchor the screen offers.
_SUBMIT_MARKER = "Ready to submit your answers?"
# Its section tabs: "←  ☒ Language  ☐ Style  ☐ Length  ✔ Submit  →".
_TABS_RE = re.compile(r"^\s*[←‹]\s+.*[☐☑☒✓✔].*[→›]\s*$")
# The section names out of that row, so the chat can head the question with
# them instead of showing the row itself, arrows and ballot boxes and all.
_TAB_CHIP_RE = re.compile(r"[☐☑☒]\s*([^☐☑☒✔→›]+)")
# The mode line and the status line are drawn at the very bottom of the pane,
# and an open dialog covers them. Seeing either one is proof that the bottom
# belongs to the ordinary UI — however much the text above it may look like a
# dialog. Which it does: this repository prints those very strings while it is
# being worked on, and that is exactly how a heredoc full of documentation
# turned into two questions in somebody's chat.
_UI_BOTTOM = ("mode on (shift+tab", "mode off (shift+tab", "claude v")

# "❯ 1. PostgreSQL" / "  2. No, exit"
_OPTION_RE = re.compile(
    r"^(?P<indent>\s*)(?P<cursor>[❯›>])?\s*(?P<num>\d+)[.)]\s+(?P<label>.+?)\s*$"
)

# A multi-select question puts a checkbox in front of every label ("1. [✔] Fix
# parser"), and the only way to send the ticked set is an unnumbered row under
# the list — no digit reaches it. The row reads "Submit" on the last section of
# a question and "Next" while sections remain, and the caption is worth
# keeping: a button that says "Send" where the terminal moves to another
# question is a button that lies.
_CHECKBOX_RE = re.compile(r"^\[(?P<mark>[ xX✓✔✗·]?)\]\s*(?P<rest>.*)$")
_ACTION_ROW_RE = re.compile(r"^(?P<cursor>[❯›>])?\s*(?P<word>Submit|Next)\s*$")

# Header chip above the question, e.g. "☐ Database"
_HEADER_RE = re.compile(r"^\s*[☐☑✓·]\s+(?P<title>.+?)\s*$")

# Box-drawing separators and spinner/status noise we never treat as content.
_SEPARATOR_RE = re.compile(r"^[\s─━═╭╮╰╯│┌┐└┘├┤┬┴┼]*$")
_SPINNER_RE = re.compile(r"^\s*[✻✽✢✳*·●⎿]\s")
# The same, minus the glyphs that also head list items: the answer review uses
# "●" for every question it echoes back, and treating those as spinner noise
# would cut the review down to its last line.
_HARD_SPINNER_RE = re.compile(r"^\s*[✻✽✢✳*]\s")

# The working line, and only it: a spinner glyph (never ⎿ or ·, which head tool
# output and list items), a verb, and a bracketed timer — "(5m 14s · ↓ 13.7k
# tokens)". Without the timer the line is a finished "Worked for 8m 9s".
_ACTIVITY_RE = re.compile(r"^[✻✽✢✳*●]\s+(\S.*\(\d+[hms]\b.*)$")

# Claude's own output is bulleted; a tool call carries its result under a "⎿",
# and the user's own prompts are echoed with a "❯" that no digit follows.
_SAID_RE = re.compile(r"^\s*●\s+(?P<text>\S.*)$")
_TOOL_OUTPUT_MARK = "⎿"
# A tool call is bulleted exactly like prose is — "● Bash(git status)" — and
# repeating it above a permission prompt that already shows the command adds
# nothing.
_TOOL_CALL_RE = re.compile(r"^[A-Z][A-Za-z0-9_]*\(")
_ECHOED_PROMPT_RE = re.compile(r"^\s*❯\s+(?!\d+[.)])\S")
# Enough of the reasoning to decide by; the tail is the part that leads into
# the question, so a longer wall of text loses its head rather than its point.
_MAX_SAID = 4000
# A line this long was wrapped by the terminal, not written that way, so the
# line under it continues the same sentence. Anything shorter ended by choice.
_WRAPPED_AT = 120
# ... unless what follows opens a list or a table row, which stands alone.
_LIST_START_RE = re.compile(r"^\s*([-*•·→▸○◦]|\d+[.)]|[|┃])\s")

# Free-text escape hatches Claude offers inside choice dialogs. They are not
# the same thing, and the bot must not treat them as one (measured on 2.1.252):
# "Type something" is an *input line inside the dialog* — the digit moves the
# cursor onto it, what follows is typed into the row, and Enter answers the
# question in one turn. "Chat about this" answers nothing: the digit alone
# rejects the question with an instruction telling Claude to ask what the user
# would like to clarify, so any text sent after it arrives a turn too late.
_TEXT_OPTION_HINTS = ("type something", "chat about this", "other")
_CHAT_OPTION_HINTS = ("chat about this",)

# Shown while the model is generating. Not enough on its own: the hint only
# joins the spinner line after the first few seconds, so a turn that has just
# started reads as idle (2.1.251: "✻ Smooshing… (1s · thinking)"). The spinner
# line itself is the reliable half — see is_busy.
_BUSY_MARKERS = ("esc to interrupt", "to interrupt")
# The spinner line as a *state*, which is stricter than _ACTIVITY_RE: the verb
# must trail an ellipsis ("Smooshing… (1s · thinking)"). _ACTIVITY_RE accepts
# the "●" that also heads Claude's prose, so without the ellipsis a sentence
# like "● I ran it (3s later)" would read as a session hard at work.
_WORKING_RE = re.compile(r"^[✻✽✢✳*●]\s+\S.*…\s*\(\d+[hms]\b")

# How far above the footer a dialog body may reach, and how much question text to keep.
_WINDOW = 60
_MAX_QUESTION = 700

# Box-drawing glyphs used to spot a side-by-side preview pane.
_BOX_CHARS = set("│┌└├┐┘┤┬┴┼╭╮╰╯║╔╚╠╗╝╣")
# A preview column must be indented at least this far and appear in this many
# rows before we treat it as a second column rather than stray art.
_MIN_PREVIEW_COL = 24
_MIN_PREVIEW_ROWS = 3
# TUI affordances that render inside the preview pane but are not the drawing.
_PREVIEW_NOISE = ("press n to add notes", "notes:")


@dataclass
class Option:
    number: int
    label: str
    description: str = ""
    selected: bool = False          # the cursor is on this row
    checked: bool | None = None     # a multi-select tick; None = no checkbox

    @property
    def is_free_text(self) -> bool:
        return self.label.strip().lower().rstrip(".") in (
            h.rstrip(".") for h in _TEXT_OPTION_HINTS
        )

    @property
    def is_chat_about(self) -> bool:
        """Whether picking this row drops the question instead of answering it."""
        return self.label.strip().lower().rstrip(".") in (
            h.rstrip(".") for h in _CHAT_OPTION_HINTS
        )


@dataclass
class Dialog:
    question: str
    options: list[Option] = field(default_factory=list)
    title: str | None = None
    footer: str = ""
    preview: str = ""          # ASCII art shown beside the selected option
    extras: list[str] = field(default_factory=list)   # unnumbered choices
    # Positions in navigation order (options, then the Submit row, then the
    # unnumbered choices): where the cursor is, and where Submit sits. The
    # distance between them is how many arrow presses reach it.
    cursor_index: int | None = None
    submit_index: int | None = None
    submit_label: str = ""     # what that row is captioned: Submit or Next

    @property
    def allows_free_text(self) -> bool:
        return any(o.is_free_text for o in self.options)

    @property
    def multi_select(self) -> bool:
        """Whether the options are checkboxes rather than one-of choices."""
        return any(o.checked is not None for o in self.options)

    @property
    def checked(self) -> list[Option]:
        return [o for o in self.options if o.checked]


# Belt and braces: if column detection ever misses, strip a trailing chunk of
# box-drawing art off the label rather than showing it on a button.
_LABEL_TAIL_RE = re.compile(r"\s{2,}(?=[\u2502\u250c\u2514\u251c\u2510\u2518\u2524"
                            r"\u252c\u2534\u253c\u2500\u256d\u256e\u2570\u256f])")


# Vertical rules can bleed into wrapped question lines when a preview pane
# is open; strip them off the edges rather than showing them as text.
_EDGE_BOX_RE = re.compile(r"^[\s\u2502\u2503\u254e\u2506|]+|[\s\u2502\u2503\u254e\u2506|]+$")


def strip_box_edges(line: str) -> str:
    return _EDGE_BOX_RE.sub("", line)


def clean_label(label: str) -> str:
    return _LABEL_TAIL_RE.split(label, maxsplit=1)[0].rstrip()


def _preview_column(lines: list[str]) -> int | None:
    """Column where a side-by-side preview pane starts, if there is one.

    Claude renders option previews to the right of the list, so a naive parse
    would splice the ASCII art into the option label.
    """
    counts: dict[int, int] = {}
    for ln in lines:
        stripped = ln.strip()
        if not stripped or _SEPARATOR_RE.match(ln):
            continue          # full-width rules are not a preview border
        for col, ch in enumerate(ln):
            if ch in _BOX_CHARS and col >= _MIN_PREVIEW_COL:
                counts[col] = counts.get(col, 0) + 1
    if not counts:
        return None
    col = min(c for c, n in counts.items() if n >= _MIN_PREVIEW_ROWS) \
        if any(n >= _MIN_PREVIEW_ROWS for n in counts.values()) else None
    return col


def _clean(lines: list[str]) -> list[str]:
    return [ln.rstrip() for ln in lines]


def _footer_index(lines: list[str]) -> int | None:
    """The lowest line that reads like a dialog footer."""
    for i in range(len(lines) - 1, -1, -1):
        ln = lines[i]
        low = ln.lower()
        esc_footer = _ESC_MARKER in ln and any(c in low for c in _ESC_COMPANIONS)
        if any(m in ln for m in _FOOTER_MARKERS) or esc_footer:
            return i
    return None


def _submit_footer_index(lines: list[str]) -> int | None:
    """Where the footer *would* be on the answer-review step.

    That screen ends at its last option — no "Enter to select", nothing. So
    three things have to line up instead, because any one of them can appear
    in ordinary output (this very repository prints all of them while it is
    being worked on, and one marker alone was enough for a false positive):

    * the section tabs of a multi-part question, with their Submit chip;
    * the review question itself, below those tabs;
    * an option list that is the last thing on the screen — a dialog owns the
      bottom of the terminal, and there is never prose underneath it.

    Answering a multi-part question from Telegram used to dead-end here: the
    buttons were gone and the fallback screen took their place.
    """
    low = "\n".join(lines).lower()
    if any(m in low for m in _UI_BOTTOM):
        return None
    marker = next((i for i in range(len(lines) - 1, -1, -1)
                   if _SUBMIT_MARKER in lines[i]), None)
    if marker is None:
        return None
    if not any(_TABS_RE.match(ln) for ln in lines[max(0, marker - _WINDOW):marker]):
        return None
    last_option = None
    for i in range(marker + 1, len(lines)):
        m = _OPTION_RE.match(lines[i])
        if m:
            # The list has to open at "1." and follow the marker directly:
            # numbered lines further down the screen belong to something else.
            if last_option is None and int(m.group("num")) != 1:
                return None
            last_option = i
        elif lines[i].strip():
            return None
    return last_option + 1 if last_option is not None else None


def _dialog_span(lines: list[str]) -> tuple[int, int, bool] | None:
    """Where the dialog starts and ends: (top, footer, footer-was-synthetic).

    Kept apart from the parsing so that `said_above_dialog` can use the same
    boundary. Guessing at it separately let the text above a question run into
    the question's own body on screens that draw no rule between them.
    """
    footer_idx = _footer_index(lines)
    # A footerless dialog is the exception, not the rule: only look for one
    # once the ordinary shapes have all been ruled out.
    footerless = footer_idx is None
    if footerless:
        footer_idx = _submit_footer_index(lines)
    if footer_idx is None:
        return None

    window_start = max(0, footer_idx - _WINDOW)
    # The list belonging to the dialog is the one that ends at the footer, and
    # it starts at "1.". Scanning downwards instead would latch onto the first
    # numbered line in the window — and Claude's own prose, still on screen
    # above the prompt, is full of numbered lists.
    first_opt = None
    for i in range(footer_idx - 1, window_start - 1, -1):
        m = _OPTION_RE.match(lines[i])
        if m:
            first_opt = i
            if int(m.group("num")) == 1:
                break
    if first_opt is None:
        return None

    # The dialog body starts after the last separator above the first option;
    # separators also appear *between* options, so scanning upward from the
    # footer would stop in the wrong place.
    top = window_start
    for i in range(first_opt - 1, window_start - 1, -1):
        ln = lines[i]
        if _SEPARATOR_RE.match(ln) and ln.strip():
            top = i + 1
            break
        if (_HARD_SPINNER_RE if footerless else _SPINNER_RE).match(ln):
            top = i + 1
            break
    return top, footer_idx, footerless


def find_dialog(screen: str) -> Dialog | None:
    """Return the open dialog on screen, or None if nothing is blocking."""
    lines = _clean(screen.splitlines())

    span = _dialog_span(lines)
    if span is None:
        return None
    top, footer_idx, footerless = span

    body = lines[top:footer_idx]
    col = _preview_column(body)
    preview_lines: list[str] = []
    if col is not None:
        # The question and header span the full width above the list; only the
        # option rows share their lines with the preview pane. Splitting from
        # the top would truncate a long question.
        opt_start = next(
            (i for i, ln in enumerate(body) if _OPTION_RE.match(ln)), len(body)
        )
        left = body[:opt_start]
        right = []
        for ln in body[opt_start:]:
            indent = len(ln) - len(ln.lstrip())
            # A real rule starts left of the preview pane; a box border that
            # begins inside the pane is part of the drawing.
            if _SEPARATOR_RE.match(ln) and ln.strip() and indent < col:
                left.append(ln)
                continue
            left.append(ln[:col].rstrip())
            right.append(ln[col:].rstrip())
        body, preview_lines = left, right

    title: str | None = None
    question_lines: list[str] = []
    options: list[Option] = []
    extras: list[str] = []
    cur_indent = 0
    nav = 0                       # rows the cursor can stand on, in order
    cursor_index: int | None = None
    submit_index: int | None = None
    submit_label = ""

    after_rule = False        # a rule separates the list from extra choices
    for ln in body:
        if not ln.strip():
            continue
        if _SEPARATOR_RE.match(ln):
            if options:
                after_rule = True
            continue
        if _TABS_RE.match(ln):
            # The tab row is navigation furniture, not part of the question;
            # its names are worth keeping, the arrows and boxes are not.
            if title is None:
                chips = [c.strip() for c in _TAB_CHIP_RE.findall(ln)]
                chips = [c for c in chips if c and c.lower() != "submit"]
                if chips:
                    title = " · ".join(chips)
            continue
        m = _OPTION_RE.match(ln)
        if m:
            cur_indent = len(m.group("indent"))
            label = clean_label(m.group("label"))
            checked: bool | None = None
            box = _CHECKBOX_RE.match(label)
            if box:
                # Keep the state, drop the glyph: a label that carries its own
                # checkbox changes on every tick, and the watcher would read
                # each tick as a brand-new question.
                checked = bool(box.group("mark").strip())
                label = box.group("rest").strip()
            if m.group("cursor"):
                cursor_index = nav
            options.append(Option(
                number=int(m.group("num")),
                label=label,
                selected=bool(m.group("cursor")),
                checked=checked,
            ))
            nav += 1
            continue
        if options and submit_index is None:
            # The Submit/Next row of a multi-select list. It has to be caught
            # here: it is indented deeper than the options, so the branch below
            # would file it away as the description of the last one.
            srow = _ACTION_ROW_RE.match(ln.strip())
            if srow:
                submit_index = nav
                submit_label = srow.group("word")
                if srow.group("cursor"):
                    cursor_index = nav
                nav += 1
                continue
        indent = len(ln) - len(ln.lstrip())
        if options and indent >= cur_indent and not after_rule:
            opt = options[-1]
            opt.description = (opt.description + " " + ln.strip()).strip()
            continue
        if options:
            # Unnumbered choices ("Chat about this") sit below the list; they
            # have no digit shortcut and are reachable only by navigating.
            choice = ln.strip()
            cursored = choice[:1] in ("❯", "›", ">")
            if cursored:
                choice = choice[1:].strip()
            if choice and len(choice) <= 60:
                if cursored:
                    cursor_index = nav
                extras.append(choice)
                nav += 1
            continue
        hm = _HEADER_RE.match(ln)
        if hm and title is None:
            title = hm.group("title")
            continue
        cleaned = strip_box_edges(ln)
        if cleaned:
            question_lines.append(cleaned)

    if not options:
        return None

    # A review screen is a list, and flattening it to one line would run the
    # questions and their answers together.
    question = ("\n" if footerless else " ").join(question_lines).strip()
    if len(question) > _MAX_QUESTION:
        question = question[-_MAX_QUESTION:].lstrip()

    preview_lines = [
        ln for ln in preview_lines
        if not any(n in ln.lower() for n in _PREVIEW_NOISE)
    ]
    preview = "\n".join(preview_lines).strip("\n")
    while "\n\n\n" in preview:
        preview = preview.replace("\n\n\n", "\n\n")

    return Dialog(
        question=question,
        options=options,
        title=title,
        footer=lines[footer_idx].strip() if footer_idx < len(lines) else "",
        preview=preview,
        extras=extras,
        cursor_index=cursor_index,
        submit_index=submit_index,
        submit_label=submit_label,
    )


def is_review(dialog: Dialog) -> bool:
    """Whether *dialog* is the "Ready to submit your answers?" review step.

    Pressing Submit on a multi-select list lands here, and the answers it
    echoes are the ones the user has already confirmed from the chat — so the
    bot answers it itself rather than sending a card that asks the same thing
    twice.
    """
    return _SUBMIT_MARKER.lower() in dialog.question.lower()


def unwrap(text: str) -> str:
    """Undo the terminal's line wrapping, keeping the author's own breaks.

    Reading the screen means reading text broken to fit 200 columns. Left in,
    those breaks land in the middle of sentences on a phone, where the column
    is a fifth as wide and the text is rewrapped anyway.
    """
    out: list[str] = []
    for line in text.splitlines():
        if (out and out[-1] and len(out[-1]) >= _WRAPPED_AT
                and line.strip() and not _LIST_START_RE.match(line)):
            out[-1] = out[-1].rstrip() + " " + line.strip()
            continue
        out.append(line)
    return "\n".join(out)


def said_above_dialog(screen: str) -> str:
    """The last thing Claude *said* above the dialog, read off the terminal.

    The transcript is the proper source for Claude's words, and everywhere
    else in this bot it is the one used. It cannot serve here: Claude Code
    writes an assistant record only once the tool call inside it has returned,
    and a question returns on a human. So the reasoning that the options are
    *about* reaches the transcript minutes after the choice was made — which
    is how a question kept arriving in the chat ahead of its own explanation.

    Which block counts is decided by what sits between it and the dialog:

    * a tool call that has not printed anything yet — Claude is asking for
      permission to run it, so nothing of this turn is on record; keep looking
      above it for what was said;
    * a tool call that *has* printed — the turn was recorded, and whatever was
      said above it has already been sent from the transcript. Say nothing.

    Blocks above the last prompt of the turn are old news for the same reason.
    """
    lines = _clean(screen.splitlines())
    floor = 0
    for i in range(len(lines) - 1, -1, -1):
        if _ECHOED_PROMPT_RE.match(lines[i]):
            floor = i + 1
            break
    # Never read into the dialog itself. It is bulleted inside — the review
    # step echoes every answer as "● question / → answer" — and its body is
    # prose too, so without this the question came back as its own preamble.
    span = _dialog_span(lines)
    ceiling = span[0] if span else len(lines)

    marks = [i for i in range(floor, ceiling) if _SAID_RE.match(lines[i])]
    for n in range(len(marks) - 1, -1, -1):
        start = marks[n]
        block = lines[start:marks[n + 1] if n + 1 < len(marks) else ceiling]
        if any(_TOOL_OUTPUT_MARK in ln for ln in block):
            return ""
        head = _SAID_RE.match(block[0])
        first = head.group("text") if head else block[0]
        if _TOOL_CALL_RE.match(first):
            continue
        # The block ends at the rule above the dialog, and "✻ Worked for 19s"
        # is a footnote to it rather than part of what was said.
        body = []
        for ln in block[1:]:
            if _HARD_SPINNER_RE.match(ln):
                break
            if _SEPARATOR_RE.match(ln) and ln.strip():
                break
            body.append(ln)
        said = unwrap(first + "\n" + textwrap.dedent("\n".join(body))).strip()
        while "\n\n\n" in said:
            said = said.replace("\n\n\n", "\n\n")
        if len(said) > _MAX_SAID:
            said = "…" + said[-_MAX_SAID:]
        return said
    return ""


def is_busy(screen: str) -> bool:
    """True while Claude is generating.

    Either half is proof: the interrupt hint, and the spinner line with its
    running timer. The hint alone used to be the test, and it misses the first
    seconds of every turn — long enough for a restart to walk over a prompt
    that had only just been sent.
    """
    low = screen.lower()
    if any(m in low for m in _BUSY_MARKERS):
        return True
    return any(_WORKING_RE.match(ln.strip()) for ln in screen.splitlines())


def read_activity(screen: str) -> str:
    """The spinner line while Claude works: "Fluttering… (2m 4s · ↓ 13.7k tokens)".

    Taken from the screen because the transcript cannot answer this: Claude
    flushes records in bursts, so a long turn writes nothing there for minutes
    while the terminal keeps counting. Reported verbatim rather than parsed —
    the wording changes between releases, and the numbers are the point.
    """
    for ln in screen.splitlines():
        hit = _ACTIVITY_RE.match(ln.strip())
        if not hit:
            continue
        body = hit.group(1)
        # The interrupt hint is advice for someone at the keyboard, not for a
        # chat, and "esc" cannot be pressed from a phone anyway.
        body = re.sub(r"\s*·?\s*esc to interrupt\)?", "", body).rstrip(" ·")
        if not body.endswith(")") and "(" in body:
            body += ")"
        return body[:120]
    return ""


def tail_text(screen: str, limit: int = 3000) -> str:
    """Screen tail with box-drawing noise stripped — for debug messages."""
    lines = [ln.rstrip() for ln in screen.splitlines()]
    keep = [ln for ln in lines if ln.strip() and not _SEPARATOR_RE.match(ln)]
    return "\n".join(keep)[-limit:]


# Lines that redraw on their own and say nothing about what is on screen: the
# status line, the input box, the "run it in the background" hint.
_STATUS_LINE_RE = re.compile(r"claude v[\d.]+\s*\||ctx:\S*\d+%|mode on \(shift\+tab")
_PROMPT_RE = re.compile(r"^\s*[❯>]\s*$")
_HINT_RE = re.compile(r"^\s*\(ctrl\+")
_DIGITS_RE = re.compile(r"\d+")


def quiet_signature(screen: str, lines: int = 14) -> str:
    """The screen reduced to what changes only when the screen *means* something new.

    A terminal never holds still: the spinner turns, the elapsed timer counts,
    the status line re-renders its percentages. Comparing raw screens therefore
    reports something new every second — which is how one prompt became thirty
    Telegram messages. Numbers are flattened rather than dropped, so "3 files"
    and "4 files" still read as the same line.
    """
    keep = []
    for ln in tail_text(screen, 4000).splitlines():
        if _STATUS_LINE_RE.search(ln) or _PROMPT_RE.match(ln) or _HINT_RE.match(ln):
            continue
        if _ACTIVITY_RE.match(ln.strip()):
            continue
        keep.append(_DIGITS_RE.sub("#", ln).strip())
    return "\n".join(keep[-lines:])


# --- status line -----------------------------------------------------------

_MODE_PATTERNS = (
    ("plan", "plan mode on"),
    ("acceptEdits", "accept edits on"),
    ("manual", "manual mode on"),
    ("auto", "auto mode on"),
)
_EFFORT_RE = re.compile(r"[◉●○]\s*(low|medium|high|xhigh|max)\b")
_MODEL_RE = re.compile(r"claude v[\d.]+\s*\|\s*([^|]+?)(?:\s{2,}|\s*\||$)")
_CTX_RE = re.compile(r"ctx:[^\s|]*?(\d+)%")
_TOKENS_RE = re.compile(r"([\d\s,]+?)\s*tokens\b")
# Rate-limit chips are rendered by the user's statusline script, whose labels
# are data-driven ("5h", "7d", "7d-Opus"), so match the shape, not the names.
_LIMIT_RE = re.compile(
    r"\b([A-Za-z0-9][A-Za-z0-9-]{0,10}):[\u2591\u2592\u2593\u2588\s]*?(\d+)%"
    r"(?:\s*reset:\s*([^|\n]+?))?(?=\s*(?:\||$))",
    re.MULTILINE,
)


@dataclass
class Status:
    model: str = ""
    effort: str = ""
    mode: str = ""
    context_pct: str = ""
    tokens: str = ""
    limits: dict[str, tuple[int, str]] = field(default_factory=dict)

    @property
    def mode_label(self) -> str:
        return {
            "auto": "auto",
            "manual": "manual",
            "acceptEdits": "accept edits",
            "plan": "plan",
        }.get(self.mode, self.mode or "?")


def read_status(screen: str) -> Status:
    """Pull model, effort and permission mode out of the status line."""
    st = Status()
    low = screen.lower()
    for name, needle in _MODE_PATTERNS:
        if needle in low:
            st.mode = name
            break
    m = _EFFORT_RE.search(screen)
    if m:
        st.effort = m.group(1)
    m = _MODEL_RE.search(screen)
    if m:
        st.model = m.group(1).strip()
    m = _CTX_RE.search(screen)
    if m:
        st.context_pct = m.group(1)
    m = _TOKENS_RE.search(screen)
    if m:
        st.tokens = m.group(1).strip()
    for label, pct, reset in _LIMIT_RE.findall(screen):
        if label.lower() in ("ctx", "v", "claude"):
            continue
        st.limits[label] = (int(pct), (reset or "").strip())
    return st
