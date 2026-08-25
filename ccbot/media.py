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


def build_prompt(paths: list[Path], text: str) -> str:
    """Compose the message handed to Claude: file paths plus the user's text."""
    if not paths:
        return text
    listing = "\n".join(str(p) for p in paths)
    noun = ngettext("this image", "these {count} images",
                    len(paths)).format(count=len(paths))
    head = _("Take a look at {what}:\n{paths}").format(what=noun, paths=listing)
    return f"{head}\n\n{text}" if text.strip() else head
