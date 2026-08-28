"""Saving Telegram attachments so Claude can read them by path.

The clipboard route is a dead end here: a headless host (WSL, a server, a
container) has no display server, so there is no system clipboard to paste
into. Files on disk work everywhere, and Claude's Read tool renders images
natively. Attachments land under the user's cache directory, which Claude may
need permission to read if the permission rules are narrow.

Nothing here judges the format. Whatever arrives is written down and its path
handed over: a JSON dump, a log, an archive and a voice message are all things
Claude can open, unpack or convert on his own, and a bot that guesses which
ones are "supported" only takes that away from him.
"""

from __future__ import annotations

import contextlib
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import paths
from .i18n import _, ngettext

MEDIA_ROOT = paths.MEDIA_DIR

# Attachments are a scratch area, not an archive.
MAX_AGE_DAYS = 14

# Only what Telegram sends without a usable file name of its own: a photo, a
# voice note, a video note. Everything else keeps the name it came with.
_EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
    "audio/ogg": ".oga",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}

# What "look at this image" is true of. A file outside this set is announced
# as a file, so Claude does not go looking for a picture in a JSON dump.
_IMAGE_SUFFIXES = frozenset({
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic",
})

# Anything that could turn a name into a path, a hidden file or a shell
# surprise. The name itself is worth keeping — it is often the only thing
# saying what the file is ("dialog-with-maksym.json").
_UNSAFE = re.compile(r"[^\w.\- ]", re.UNICODE)
_MAX_NAME = 80


def session_dir(session_id: str) -> Path:
    d = MEDIA_ROOT / session_id[:8]
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_name(name: str | None) -> str:
    """The sender's file name, reduced to something safe to create on disk.

    Nothing is added to it — a `Makefile` stays a `Makefile`. An extension
    guessed from the MIME type would be a guess written into the name Claude
    reads back, and Telegram's own idea of it (`documents/file_4.txt`) is no
    better informed than the person who named the file.
    """
    if not name:
        return ""
    cleaned = _UNSAFE.sub("_", Path(name).name).strip(" .")
    if not cleaned:
        return ""
    stem, dot, ext = cleaned.rpartition(".")
    return f"{stem[:_MAX_NAME]}.{ext[:16]}" if dot else cleaned[:_MAX_NAME]


def new_path(session_id: str, suffix: str, index: int = 0,
             name: str | None = None) -> Path:
    """Where an incoming attachment lands.

    A file the person sent has a name that means something, and the path is
    all Claude gets to see — so the name is kept, only made safe, and a second
    file of the same name gets a counter rather than overwriting the first.
    A photo has no name at all, hence the timestamp fallback; and the index is
    always there because two albums sent within the same second would
    otherwise collide on their first file.
    """
    d = session_dir(session_id)
    wanted = safe_name(name)
    if wanted:
        candidate, n = d / wanted, 1
        stem, ext = Path(wanted).stem, Path(wanted).suffix
        while candidate.exists():
            candidate = d / f"{stem}-{n}{ext}"
            n += 1
        return candidate
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return d / f"{stamp}-{index}{suffix}"


def is_image(path: Path) -> bool:
    return path.suffix.lower() in _IMAGE_SUFFIXES


def guess_suffix(file_path: str | None, mime: str | None, fallback: str = "") -> str:
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
    if all(is_image(p) for p in paths):
        noun = ngettext("this image", "these {count} images",
                        len(paths)).format(count=len(paths))
    else:
        # One JSON among the photos is enough to make "image" a lie, and a
        # mixed batch reads fine as files.
        noun = ngettext("this file", "these {count} files",
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
            mark = _("[image {n}]") if is_image(piece.path) else _("[file {n}]")
            who += " " + mark.format(n=seen)
        body = piece.text.strip()
        lines.append(f"{who}: {body}" if body else who)
    head = ngettext("Forwarded from Telegram, {count} message, oldest first:",
                    "Forwarded from Telegram, {count} messages, oldest first:",
                    len(pieces)).format(count=len(pieces))
    dump = head + "\n\n" + "\n".join(lines)
    if paths:
        return _paths_head(paths, numbered=True) + "\n\n" + dump
    return dump
