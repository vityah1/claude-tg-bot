"""Small helpers shared by the bot, the bridge and the watcher."""

from __future__ import annotations

import html
import re
from pathlib import Path

from .i18n import _, ngettext

TG_LIMIT = 4096
_SAFE = TG_LIMIT - 96          # room for wrappers and ellipses
# How long a hand-picked session name may be before it stops fitting a button.
NAME_LIMIT = 40


def slug(cwd: str) -> str:
    """Directory name, reduced to characters a launch name may hold."""
    base = Path(cwd).name or "root"
    return re.sub(r"[^A-Za-z0-9_.-]", "-", base)[:20] or "session"


def default_name(cwd: str, session_id: str) -> str:
    """The name the bot gives a session it just created."""
    return f"{slug(cwd)}-{session_id[:4]}"


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
    """A text gauge for /usage."""
    pct = max(0, min(100, pct))
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _thousands(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def gauges(usage) -> list[tuple[str, int, str]]:
    """Normalised (key, percent, human label) gauges of a session's metrics."""
    if not usage:
        return []
    out = [] if usage.ctx_pct is None else [
        ("ctx", usage.ctx_pct, _("session context"))]
    for lim in usage.limits:
        out.append((lim.key, lim.pct, _limit_note(lim.label, lim.reset_text)))
    return out


def _limit_note(label: str, reset: str) -> str:
    if reset:
        return _("{window} limit (resets {when})").format(window=label, when=reset)
    return _("{window} limit").format(window=label)


def account_line(email: str) -> str:
    """The account a session runs under, or nothing when it is not known."""
    return _("👤 Account: {email}").format(email=html.escape(email)) if email else ""


def usage_report(name: str, usage, account: str = "") -> str:
    """Full context/limit breakdown, shown on demand.

    The account goes right under the name: the limits below belong to it,
    not to the session.
    """
    lines = [f"📊 <b>{name}</b>"]
    if account:
        lines.append(account_line(account))
    lines.append("")
    head = len(lines)
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
    if len(lines) == head:
        lines.append(_("No metrics yet — a session reports them with its "
                       "first answer."))
    return "\n".join(lines)


def usage_suffix(usage, ctx_floor: int = 50, limit_floor: int = 75) -> str:
    """Compact tail appended to replies once usage starts to matter.

    Silent while there is plenty of room, so routine answers stay clean.
    """
    bits = []
    for key, pct, _note in gauges(usage):
        floor = ctx_floor if key == "ctx" else limit_floor
        if pct >= floor:
            short = "ctx" if key == "ctx" else key.replace("five_hour", "5h") \
                .replace("seven_day_opus", "7d-Opus").replace("seven_day", "7d")
            bits.append(f"{short} {pct}%")
    return "\n\n· " + " · ".join(bits) if bits else ""

