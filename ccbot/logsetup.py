"""Logging to a rotating file, so past sessions can be reconstructed.

journalctl holds the same records, but a file is easier to grep from the phone
via /log and survives independently of systemd's retention.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import BaseMiddleware

from . import paths
from .i18n import _

LOG_PATH = paths.LOG_FILE
LOG_DIR = LOG_PATH.parent

_FMT = "%(asctime)s %(levelname)-7s %(name)-16s %(message)s"
_MAX_BYTES = 2 * 1024 * 1024
_BACKUPS = 3


def setup(level: int | str = logging.INFO) -> Path:
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(_FMT)

    root = logging.getLogger()
    root.setLevel(level)

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    rotating = RotatingFileHandler(
        LOG_PATH, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
    )
    rotating.setFormatter(fmt)
    root.addHandler(rotating)

    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    return LOG_PATH


def tail(lines: int = 60) -> str:
    """Last *lines* of the log, for showing in chat."""
    if not LOG_PATH.exists():
        return _("The log is empty.")
    try:
        with LOG_PATH.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 64 * 1024))
            data = fh.read().decode("utf-8", "replace")
    except OSError as exc:
        return _("Cannot read the log: {error}").format(error=exc)
    return "\n".join(data.splitlines()[-lines:])


class UpdateLogMiddleware(BaseMiddleware):
    """Log every incoming update before any handler sees it.

    Placed as an outer middleware, so it also records updates that are later
    dropped (unknown sender, unmatched handler) — exactly the cases that are
    invisible otherwise and hardest to reconstruct after the fact.
    """

    def __init__(self, allowed: frozenset[int] | set[int] | None = None):
        self.allowed = allowed or frozenset()
        self.log = logging.getLogger("ccbot.update")

    async def __call__(self, handler, event, data):
        try:
            self._describe(event)
        except Exception:            # logging must never break delivery
            self.log.exception("failed to describe update")
        return await handler(event, data)

    def _describe(self, event) -> None:
        upd = getattr(event, "event_type", "?")
        msg = getattr(event, "message", None)
        cb = getattr(event, "callback_query", None)
        if msg is not None:
            uid = msg.from_user.id if msg.from_user else None
            kind = ("text" if msg.text else
                    "photo" if msg.photo else
                    "document" if msg.document else "other")
            body = msg.text or msg.caption or ""
            allowed = "" if not self.allowed or uid in self.allowed else " DENIED"
            self.log.info(
                "in msg id=%s from=%s kind=%s reply_to=%s%s: %r",
                msg.message_id, uid, kind,
                getattr(msg.reply_to_message, "message_id", None),
                allowed, body[:160],
            )
        elif cb is not None:
            uid = cb.from_user.id if cb.from_user else None
            allowed = "" if not self.allowed or uid in self.allowed else " DENIED"
            self.log.info("in callback from=%s data=%s%s", uid, cb.data, allowed)
        else:
            self.log.info("in update type=%s", upd)
