"""Reading Claude Code session transcripts (`~/.claude/projects/*/<uuid>.jsonl`).

Assistant output is taken from here rather than scraped off the terminal: the
transcript is clean UTF-8 with no escape sequences, no line wrapping and no
scrollback limit. The terminal is only consulted for blocking dialogs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Tool calls are summarised, not dumped: the phone only needs to know what ran.
_TOOL_HINT_KEYS = ("command", "file_path", "pattern", "path", "url", "prompt", "description")
# Wrappers Claude injects as "user" messages; none of them is what a person
# would recognise as what the session is about.
_INJECTED_PREFIXES = ("<local-command", "<command-", "<system-", "<user-prompt",
                      "<session-", "Caveat:")
# How far into a transcript to look for the opening prompt before giving up.
# A session opens with a file-history snapshot that routinely runs past 250 KB,
# so a smaller budget finds the preamble and nothing else.
PROMPT_SCAN_BYTES = 1024 * 1024
# Enough of the tail to be sure of catching a record with a version on it.
VERSION_SCAN_BYTES = 256 * 1024

_VERSION_RE = re.compile(r'"version"\s*:\s*"([^"]+)"')


@dataclass
class Event:
    kind: str          # "text" | "tool" | "title"
    text: str


def find_transcript(session_id: str) -> Path | None:
    """Locate a session's transcript by id.

    Globbing beats rebuilding the directory name: the encoding maps both '/'
    and '_' to '-', so '/a/b_c' and '/a/b-c' collide and cannot be inverted.
    """
    hits = sorted(PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
    return hits[0] if hits else None


def prompt_text(line: str) -> str | None:
    """The user's typed message as written, line breaks and all.

    Kept apart from `prompt_from_line` because the layout carries meaning: an
    attachment prompt is a heading, then paths, then the caption, and the
    caption cannot be told from the paths once it has all been flattened onto
    one line (`sessions.clean_prompt`).
    """
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    if rec.get("type") != "user" or rec.get("isSidechain") or rec.get("toolUseResult"):
        return None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list):
        # Attachments arrive as blocks; the typed text is in the first one.
        content = next((b.get("text") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"), None)
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text or text.startswith("/") or text.startswith(_INJECTED_PREFIXES):
        return None
    return text


def prompt_from_line(line: str, limit: int = 60) -> str | None:
    """The user's typed message, flattened onto one line for a caption."""
    text = prompt_text(line)
    return " ".join(text.split())[:limit] if text else None


def first_prompt(session_id: str, limit: int = 60) -> str | None:
    """What the session was asked first — a label a person can recognise.

    Only the head of the transcript is read: if the opening prompt is not in
    the first megabyte, it is not going to be found at all.
    """
    path = find_transcript(session_id)
    if path is None:
        return None
    try:
        with path.open("rb") as fh:
            read = 0
            for raw in fh:
                read += len(raw)
                if read > PROMPT_SCAN_BYTES:
                    break
                line = raw.decode("utf-8", "replace")
                if '"type":"user"' in line.replace(" ", ""):
                    found = prompt_from_line(line, limit)
                    if found:
                        return found
    except OSError:
        return None
    return None


def _summarise_tool(block: dict) -> str:
    name = block.get("name", "tool")
    inp = block.get("input") or {}
    for key in _TOOL_HINT_KEYS:
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            hint = " ".join(val.split())
            if len(hint) > 120:
                hint = hint[:120] + "…"
            return f"{name}: {hint}"
    return name


def parse_line(raw: str) -> list[Event]:
    """Turn one JSONL line into user-visible events (may be empty)."""
    try:
        rec = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []

    rtype = rec.get("type")
    if rtype == "ai-title":
        title = rec.get("aiTitle")
        return [Event("title", title)] if title else []

    if rtype != "assistant" or rec.get("isSidechain"):
        return []

    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return [Event("text", content)] if content.strip() else []
    if not isinstance(content, list):
        return []

    events: list[Event] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            txt = (block.get("text") or "").strip()
            if txt:
                events.append(Event("text", txt))
        elif btype == "tool_use":
            events.append(Event("tool", _summarise_tool(block)))
        # "thinking" blocks are deliberately skipped
    return events


class TranscriptReader:
    """Incremental tail of one session's transcript."""

    def __init__(self, session_id: str, offset: int = 0):
        self.session_id = session_id
        self.offset = offset
        self._path: Path | None = None

    @property
    def path(self) -> Path | None:
        if self._path is None or not self._path.exists():
            self._path = find_transcript(self.session_id)
        return self._path

    def seek_to_end(self) -> None:
        """Ignore everything written so far (used when adopting a session)."""
        p = self.path
        self.offset = p.stat().st_size if p else 0

    def read_new(self) -> list[Event]:
        p = self.path
        if p is None:
            return []
        try:
            size = p.stat().st_size
        except OSError:
            return []
        if size < self.offset:      # transcript replaced — start over
            self.offset = 0
        if size == self.offset:
            return []

        events: list[Event] = []
        with p.open("rb") as fh:
            fh.seek(self.offset)
            data = fh.read()
            # Keep a trailing partial line for the next poll.
            last_nl = data.rfind(b"\n")
            if last_nl == -1:
                return []
            chunk, self.offset = data[:last_nl], self.offset + last_nl + 1
        for raw in chunk.decode("utf-8", "replace").splitlines():
            if raw.strip():
                events.extend(parse_line(raw))
        return events


def last_version(session_id: str) -> str:
    """The Claude Code build that wrote to this transcript most recently.

    A fallback for `updates.running()` on an install with no status-line tee.
    Only the tail is read, and only for the *last* match: every record carries
    the version of the process that wrote it, so an older one further up says
    nothing about the process alive now.
    """
    path = find_transcript(session_id)
    if path is None:
        return ""
    try:
        with path.open("rb") as fh:
            size = path.stat().st_size
            fh.seek(max(0, size - VERSION_SCAN_BYTES))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    found = _VERSION_RE.findall(tail)
    return found[-1] if found else ""
