"""Saving Telegram attachments so Claude can read them by path.

The clipboard route is a dead end here: a headless host (WSL, a server, a
container) has no display server, so there is no system clipboard to paste
into. Files on disk work everywhere, and Claude's Read tool renders images
natively. Attachments land under the user's cache directory, which Claude may
need permission to read if the permission rules are narrow.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import paths
from .i18n import _, ngettext

MEDIA_ROOT = paths.MEDIA_DIR

# Attachments are a scratch area, not an archive.
MAX_AGE_DAYS = 14

_EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
}


def session_dir(session_id: str) -> Path:
    d = MEDIA_ROOT / session_id[:8]
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_path(session_id: str, suffix: str, index: int = 0) -> Path:
    # Always index: two albums sent within the same second would otherwise
    # collide on their first file.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return session_dir(session_id) / f"{stamp}-{index}{suffix}"


def guess_suffix(file_path: str | None, mime: str | None, fallback: str = ".jpg") -> str:
    if file_path:
        suf = Path(file_path).suffix
        if suf:
            return suf
    if mime and mime in _EXT_BY_MIME:
        return _EXT_BY_MIME[mime]
    return fallback


def cleanup(max_age_days: int = MAX_AGE_DAYS) -> int:
    """Drop attachments older than *max_age_days*. Returns how many were removed."""
    if not MEDIA_ROOT.exists():
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for path in MEDIA_ROOT.rglob("*"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    for d in MEDIA_ROOT.iterdir():
        if d.is_dir() and not any(d.iterdir()):
            with contextlib.suppress(OSError):
                d.rmdir()
    return removed


@dataclass(frozen=True)
class Piece:
    """One Telegram message on its way into a prompt.

    *sender* is filled in for a forwarded message and empty for one the user
    wrote themselves — which is what decides whether the prompt keeps the
    names. A forwarded conversation without them is unreadable: two people
    answering each other become one voice contradicting itself.
    """

    text: str = ""
    path: Path | None = None
    sender: str = ""


def _paths_head(paths: list[Path], numbered: bool = False) -> str:
    listing = "\n".join(f"{i}. {p}" for i, p in enumerate(paths, 1)) if numbered \
        else "\n".join(str(p) for p in paths)
    noun = ngettext("this image", "these {count} images",
                    len(paths)).format(count=len(paths))
    return _("Take a look at {what}:\n{paths}").format(what=noun, paths=listing)


def build_prompt(paths: list[Path], text: str) -> str:
    """Compose the message handed to Claude: file paths plus the user's text."""
    if not paths:
        return text
    head = _paths_head(paths)
    return f"{head}\n\n{text}" if text.strip() else head


def build_batch_prompt(pieces: list[Piece]) -> str:
    """Fold a burst of messages into the single prompt Claude receives.

    Nothing forwarded means nothing to attribute, so the texts are simply
    joined and the attachments listed as before — a lone message comes out
    byte for byte as the person typed it. A forwarded batch instead becomes a
    transcript: every line says who wrote it, and an attachment is referenced
    by number from the line it arrived with, so the picture stays attached to
    the sentence that asked about it.
    """
    paths = [p.path for p in pieces if p.path]
    if not any(p.sender for p in pieces):
        joined = "\n\n".join(t for t in (p.text.strip() for p in pieces) if t)
        return build_prompt(paths, joined)

    lines, seen = [], 0
    for piece in pieces:
        who = piece.sender or _("me")
        if piece.path:
            seen += 1
            who += " " + _("[image {n}]").format(n=seen)
        body = piece.text.strip()
        lines.append(f"{who}: {body}" if body else who)
    head = ngettext("Forwarded from Telegram, {count} message, oldest first:",
                    "Forwarded from Telegram, {count} messages, oldest first:",
                    len(pieces)).format(count=len(pieces))
    dump = head + "\n\n" + "\n".join(lines)
    if paths:
        return _paths_head(paths, numbered=True) + "\n\n" + dump
    return dump
