"""Small helpers shared by the bot and the watcher."""

from __future__ import annotations

import html
import re
from pathlib import Path

from . import winpower
from .i18n import _, ngettext

TG_LIMIT = 4096
_SAFE = TG_LIMIT - 96          # room for wrappers and ellipses
# How long a hand-picked session name may be before it stops fitting a button.
NAME_LIMIT = 40


def slug(cwd: str) -> str:
    """Directory name, reduced to characters tmux and Claude accept."""
    base = Path(cwd).name or "root"
    return re.sub(r"[^A-Za-z0-9_.-]", "-", base)[:20] or "session"


def default_name(cwd: str, session_id: str) -> str:
    """The name the bot gives a session it just created."""
    return f"{slug(cwd)}-{session_id[:4]}"


def window_name(name: str) -> str:
    """A session name reduced to something tmux can label a window with.

    Unicode is fine there; ':' and '#' are not — the first separates
    session:window in every target spec, the second starts a format string.
    """
    safe = re.sub(r"[\s:#]+", "-", name).strip("-")
    return safe[:24] or "session"


def clean_name(text: str) -> str:
    """A user-supplied session name, trimmed to one printable line."""
    name = " ".join((text or "").split())
    name = "".join(ch for ch in name if ch.isprintable())
    if len(name) > NAME_LIMIT:
        name = name[:NAME_LIMIT].rstrip() + "…"
    return name


def split_text(text: str, limit: int = _SAFE) -> list[str]:
    """Split on paragraph/line boundaries so messages stay readable."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    buf = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:            # a single very long line
            if buf:
                chunks.append(buf.rstrip())
                buf = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(buf) + len(line) > limit:
            chunks.append(buf.rstrip())
            buf = line
        else:
            buf += line
    if buf.strip():
        chunks.append(buf.rstrip())
    return chunks


def as_pre(text: str, limit: int = _SAFE) -> str:
    """Wrap terminal output in a monospace block, tail-trimmed to fit."""
    text = text.rstrip()
    if len(text) > limit:
        text = "…\n" + text[-limit:]
    return f"<pre>{html.escape(text)}</pre>"


def bar(pct: int, width: int = 10) -> str:
    """Text gauge mirroring the one in the terminal status line."""
    pct = max(0, min(100, pct))
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _thousands(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def gauges(usage, status) -> list[tuple[str, int, str]]:
    """Normalised (key, percent, human label) gauges from either source."""
    if usage:
        out = [] if usage.ctx_pct is None else [
            ("ctx", usage.ctx_pct, _("session context"))]
        for lim in usage.limits:
            out.append((lim.key, lim.pct, _limit_note(lim.label, lim.reset_text)))
        return out
    out = []
    if status and status.context_pct:
        out.append(("ctx", int(status.context_pct), _("session context")))
    for label, (pct, reset) in (status.limits if status else {}).items():
        out.append((label, pct, _limit_note(label, reset)))
    return out


def _limit_note(label: str, reset: str) -> str:
    if reset:
        return _("{window} limit (resets {when})").format(window=label, when=reset)
    return _("{window} limit").format(window=label)


def usage_report(name: str, usage, status=None) -> str:
    """Full context/limit breakdown, shown on demand."""
    lines = [f"📊 <b>{name}</b>", ""]
    if usage:
        if usage.ctx_pct is not None:
            room = _thousands(usage.ctx_window) if usage.ctx_window else "?"
            gauge = _("context")
            lines.append(
                f"<code>{gauge} {bar(usage.ctx_pct)} {usage.ctx_pct:>3}%</code>"
            )
            lines.append("     " + ngettext(
                "{used} of {total} token", "{used} of {total} tokens",
                usage.ctx_tokens,
            ).format(used=_thousands(usage.ctx_tokens), total=room))
        for lim in usage.limits:
            tail = ("  · " + _("resets {when}").format(when=lim.reset_text)
                    if lim.reset_text else "")
            lines.append(f"<code>{lim.label:<12} {bar(lim.pct)} {lim.pct:>3}%</code>{tail}")
        extra = []
        if usage.model:
            extra.append(usage.model)
        if usage.effort:
            extra.append(f"◉ {usage.effort}")
        if usage.cost_usd:
            extra.append(f"${usage.cost_usd:.2f}")
        if extra:
            lines += ["", " · ".join(extra)]
        if usage.stale:
            lines.append("\n<i>" + _(
                "{minutes} min stale — this session has not redrawn its "
                "status line in a while"
            ).format(minutes=f"{usage.age / 60:.0f}") + "</i>")
        return "\n".join(lines)

    # Fallback: the tee wrapper is not installed, so scrape the status line.
    for _key, pct, note in gauges(None, status):
        lines.append(f"<code>{note[:12]:<12} {bar(pct)} {pct:>3}%</code>")
    if len(lines) == 2:
        lines.append(_("No metrics yet — the status line has not been drawn."))
    else:
        lines.append("\n<i>" + _(
            "read off the status line; exact figures appear once the "
            "statusline wrapper is in place"
        ) + "</i>")
    return "\n".join(lines)


def power_report(state: winpower.Power | None) -> str:
    """The power card: both overlays, and which of them is live.

    Both are shown because on a laptop they differ — Windows ships "best
    performance" on battery here — and a card naming only one would read as if
    the other had moved too.
    """
    if state is None:
        return _("⚡ <b>Power mode</b>\n\nWindows is out of reach from here — "
                 "this needs the bot to be running inside WSL, with interop "
                 "enabled.")
    charge = f" {state.battery_pct}%" if state.battery_pct is not None else ""
    lines = [_("⚡ <b>Power mode</b>"), ""]
    for icon, key, live, note in (
        ("🔌", state.ac, state.on_mains is True, _("mains")),
        ("🔋", state.dc, state.on_mains is False, _("battery") + charge),
    ):
        name = _(winpower.LABEL[key]) if key else _("a mode the bot does not know")
        if live:
            lines.append(f"{icon} <b>{name}</b> · {note} · " + _("in force now"))
        else:
            lines.append(f"{icon} {name} · {note}")
    lines.append("")
    lines.append("<i>" + _(
        "Windows' own power mode: it moves the CPU's energy preference and its "
        "turbo, and the fans follow the temperature. The vendor's Quiet and "
        "Performance modes are not reachable from here — those go through the "
        "Control Center driver."
    ) + "</i>")
    return "\n".join(lines)


def usage_suffix(usage, status=None, ctx_floor: int = 50, limit_floor: int = 75) -> str:
    """Compact tail appended to replies once usage starts to matter.

    Silent while there is plenty of room, so routine answers stay clean.
    """
    bits = []
    for key, pct, _note in gauges(usage, status):
        floor = ctx_floor if key == "ctx" else limit_floor
        if pct >= floor:
            short = "ctx" if key == "ctx" else key.replace("five_hour", "5h") \
                .replace("seven_day_opus", "7d-Opus").replace("seven_day", "7d")
            bits.append(f"{short} {pct}%")
    return "\n\n· " + " · ".join(bits) if bits else ""


def as_pre_lines(text: str, max_lines: int = 28, limit: int = 3000) -> str:
    """Monospace block trimmed by whole lines.

    Telegram renders <pre> in a fixed-width font and lets the reader scroll it
    sideways, so lines must never be re-wrapped — cutting mid-line would break
    the alignment that makes ASCII art readable.
    """
    lines = [ln.rstrip() for ln in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    clipped = False
    if len(lines) > max_lines:
        lines, clipped = lines[:max_lines], True
    body = "\n".join(lines)
    while len(body) > limit and lines:
        lines.pop()
        clipped = True
        body = "\n".join(lines)
    if clipped:
        body += "\n…"
    return f"<pre>{html.escape(body)}</pre>"
