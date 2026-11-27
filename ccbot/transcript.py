"""Reading Claude Code session transcripts (`~/.claude/projects/*/<uuid>.jsonl`).

The live output of a session arrives as events of the session host; the
transcript is read for what outlives the process — where a session stopped
(`recap`), the captions of closed sessions, the version and the account a
session last ran under.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Tool calls are summarised, not dumped: the phone only needs to know what ran.
_TOOL_HINT_KEYS = ("command", "file_path", "pattern", "path", "url", "prompt", "description")
# Wrappers Claude injects as "user" messages; none of them is what a person
# would recognise as what the session is about.
_INJECTED_PREFIXES = ("<local-command", "<command-", "<system-", "<user-prompt",
                      "<session-", "Caveat:", "<task-notification",
                      # What Claude Code writes after compacting a session:
                      # it opens half the long ones and names none of them.
                      "This session is being continued from a previous",
                      "[Request interrupted by user")
# How far into a transcript to look for the opening prompt before giving up.
# A session opens with a file-history snapshot that routinely runs past 250 KB,
# so a smaller budget finds the preamble and nothing else.
PROMPT_SCAN_BYTES = 1024 * 1024
# Enough of the tail to be sure of catching a record with a version on it.
VERSION_SCAN_BYTES = 256 * 1024

_VERSION_RE = re.compile(r'"version"\s*:\s*"([^"]+)"')


@dataclass
class Event:
    kind: str          # "text" | "tool"
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
    # A skill's text loaded into the turn, a background task's notice: written
    # as user records, typed by nobody.
    if rec.get("isMeta") or rec.get("origin"):
        return None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list):
        # Attachments arrive as blocks; the typed text is in the first one.
        content = next((b.get("text") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"), None)
    return typed_text(content)


def typed_text(content: object) -> str | None:
    """*content* when a person could have typed it, not a command or an injection."""
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text or text.startswith("/") or text.startswith(_INJECTED_PREFIXES):
        return None
    return text


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
        elif btype == "thinking":
            # The reasoning itself is stored redacted (an empty string next to
            # a signature). What does arrive non-empty is the short progress
            # note Claude addresses to the user mid-turn ("Backfill is writing
            # in batches…"); Claude Code shows it as an ordinary message, so
            # dropping it lost whole paragraphs (2026-10-09, 4ae7b1ec).
            txt = (block.get("thinking") or "").strip()
            if txt:
                events.append(Event("text", txt))
        elif btype == "tool_use":
            events.append(Event("tool", _summarise_tool(block)))
    return events


@dataclass
class Recap:
    """Where a session stopped, read off the tail of its transcript."""

    prompt: str = ""                 # the last thing the user asked
    steps: list[str] = field(default_factory=list)   # tool lines since, newest last
    answer: str = ""                 # Claude's last words since that prompt


def recap(session_id: str, steps: int = 8, span: int = 800 * 1024) -> Recap:
    """The last prompt, the tool calls and the last answer after it.

    Read from the transcript, so it costs no tokens and is the same whether
    the session is running, resting or the bot has just restarted.
    """
    path = find_transcript(session_id)
    out = Recap()
    if path is None:
        return out
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - span))
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return out
    lines = data.splitlines()
    if size > span:
        lines = lines[1:]            # the first one is cut in half
    tools: list[str] = []
    for raw in lines:
        typed = prompt_text(raw)
        if typed:
            out.prompt, tools, out.answer = typed, [], ""
            continue
        for ev in parse_line(raw):
            if ev.kind == "tool":
                tools.append(ev.text)
            elif ev.kind == "text":
                out.answer = ev.text
    out.steps = tools[-steps:]
    return out


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
