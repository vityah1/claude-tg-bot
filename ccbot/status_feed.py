"""Exact session metrics: context, rate limits, model, cost, version.

The session host reports them as events, the bridge keeps the latest per
session and answers `read()` through the `sdk_usage` hook, so every reader —
the card, /usage, the alerts, the version check — asks here.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .i18n import N_, _

log = logging.getLogger("ccbot.status_feed")


def _tz() -> tzinfo | None:
    """Which clock reset times are quoted in: the host's, unless told otherwise.

    None means local time, which is right for a laptop. A bot on a rented
    server would otherwise announce resets in UTC to someone living three
    zones away — CCBOT_TZ is the way out of that.
    """
    name = (os.getenv("CCBOT_TZ") or "").strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("CCBOT_TZ=%r is not a known timezone — using local time", name)
        return None


TZ = _tz()

# Looked up long after import, so marked here and translated at the use site.
_LIMIT_LABELS = {
    "five_hour": N_("5 hours"),
    "seven_day": N_("7 days"),
    "seven_day_opus": N_("7 days · Opus"),
}
# TRANSLATORS: weekday names, abbreviated as far as your language allows.
_DOW = (N_("Mon"), N_("Tue"), N_("Wed"), N_("Thu"),
        N_("Fri"), N_("Sat"), N_("Sun"))


def _fmt_reset(epoch: float | None) -> str:
    if not epoch:
        return ""
    try:
        dt = datetime.fromtimestamp(float(epoch), TZ)
    except (OSError, ValueError, TypeError):
        return ""
    now = datetime.now(TZ)
    delta_days = (dt.date() - now.date()).days
    if delta_days == 0:
        return _("today {time}").format(time=f"{dt:%H:%M}")
    if delta_days == 1:
        return _("tomorrow {time}").format(time=f"{dt:%H:%M}")
    return _("{weekday} {time}").format(weekday=_(_DOW[dt.weekday()]),
                                        time=f"{dt:%H:%M}")


@dataclass
class Limit:
    key: str
    pct: int
    resets_at: float | None = None

    @property
    def label(self) -> str:
        msgid = _LIMIT_LABELS.get(self.key)
        return _(msgid) if msgid else self.key

    @property
    def reset_text(self) -> str:
        return _fmt_reset(self.resets_at)


@dataclass
class Usage:
    session_id: str = ""
    session_name: str = ""
    model: str = ""
    model_id: str = ""
    effort: str = ""
    version: str = ""      # the Claude Code build this session is running
    cwd: str = ""
    transcript_path: str = ""
    ctx_pct: int | None = None
    ctx_window: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    limits: list[Limit] = field(default_factory=list)

    @property
    def ctx_tokens(self) -> int:
        """Tokens currently occupying the context window."""
        return self.input_tokens


# The bridge's answer for a session (set by the bot at start-up).
sdk_usage: Callable[[str], Usage | None] | None = None


def read(session_id: str) -> Usage | None:
    """The session's latest metrics, or None before it has reported any."""
    return sdk_usage(session_id) if sdk_usage is not None else None

