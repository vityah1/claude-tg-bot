"""Exact session metrics, straight from the status-line payload.

Claude Code hands its status line a JSON document with real numbers. Reading
those back out of the rendered bars would be lossy — the bars are rounded to
10 % steps and the reset times to minutes — so `bin/statusline-tee.sh` mirrors
the payload into ~/.cache/ccbot/status/<session_id>.json and this module reads
it. Screen scraping stays as a fallback for when the tee is not installed.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import paths
from .i18n import N_, _

log = logging.getLogger("ccbot.status_feed")

FEED_DIR = paths.STATUS_DIR


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

# Beyond this the figures describe a session that has not redrawn in a while.
STALE_AFTER = 180.0

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
    cwd: str = ""
    transcript_path: str = ""
    ctx_pct: int | None = None
    ctx_window: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    limits: list[Limit] = field(default_factory=list)
    age: float = 0.0

    @property
    def stale(self) -> bool:
        return self.age > STALE_AFTER

    @property
    def ctx_tokens(self) -> int:
        """Tokens currently occupying the context window."""
        return self.input_tokens


def path_for(session_id: str) -> Path:
    return FEED_DIR / f"{session_id}.json"


def available() -> bool:
    """Whether the tee wrapper is feeding us anything at all."""
    return FEED_DIR.is_dir() and any(FEED_DIR.glob("*.json"))


def read(session_id: str) -> Usage | None:
    p = path_for(session_id)
    try:
        raw = p.read_text()
        age = time.time() - p.stat().st_mtime
    except OSError:
        return None
    try:
        d = json.loads(raw)
    except ValueError:
        return None

    ctx = d.get("context_window") or {}
    cur = ctx.get("current_usage") or {}
    used = ctx.get("used_percentage")
    cost = d.get("cost") or {}

    limits = []
    for key, val in (d.get("rate_limits") or {}).items():
        if not isinstance(val, dict):
            continue
        pct = val.get("used_percentage")
        if pct is None:
            continue
        limits.append(Limit(key=key, pct=round(float(pct)),
                            resets_at=val.get("resets_at")))
    # five_hour before seven_day before anything model-specific
    order = {"five_hour": 0, "seven_day": 1}
    limits.sort(key=lambda lim: (order.get(lim.key, 2), lim.key))

    return Usage(
        session_id=d.get("session_id") or session_id,
        session_name=d.get("session_name") or "",
        model=(d.get("model") or {}).get("display_name") or "",
        model_id=(d.get("model") or {}).get("id") or "",
        effort=(d.get("effort") or {}).get("level") or "",
        cwd=d.get("cwd") or "",
        transcript_path=d.get("transcript_path") or "",
        ctx_pct=round(float(used)) if used is not None else None,
        ctx_window=int(ctx.get("context_window_size") or 0),
        input_tokens=int(
            (cur.get("input_tokens") or 0)
            + (cur.get("cache_read_input_tokens") or 0)
            + (cur.get("cache_creation_input_tokens") or 0)
        ),
        output_tokens=int(ctx.get("total_output_tokens") or 0),
        cost_usd=float(cost.get("total_cost_usd") or 0.0),
        limits=limits,
        age=age,
    )


def cleanup(max_age_days: int = 3) -> int:
    """Drop payloads for sessions that are long gone."""
    if not FEED_DIR.is_dir():
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for f in FEED_DIR.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            continue
    return removed
