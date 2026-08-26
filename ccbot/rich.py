"""Bot API 10.x rich messages: monospace that scrolls, Markdown as it is.

Until June 2026 a phone wrapped a ``<pre>`` block instead of scrolling it, so
anything wider than roughly a phone had to leave as a PNG (``render.py``), and
Claude's answers — which are Markdown — went out as the asterisks around the
text. Bot API 10.1 replaced both workarounds: ``sendRichMessage`` takes either
a list of blocks or a whole Markdown document, the client draws the tables and
the code, and a preformatted block scrolls sideways.

Three things measured against the live API rather than read in the docs:

* one preformatted block accepted 30 000 characters, where the documentation
  promises 1024 — a full 200x50 pane needs no splitting;
* box-drawing glyphs (``─ │ ┼``) are not a letter wide in Telegram's monospace
  face, so a captured frame comes out ragged, while plain ASCII (``- | +``)
  lines up exactly. Hence `ascii_frame`, applied to everything read off a
  terminal;
* a rich message can be edited in place with ``editMessageText`` and carries
  an inline keyboard, so the watcher's edit-instead-of-repost rule and the
  dialog buttons both survive.

Text inside a block is never parsed, which is the point for terminal output:
an option label containing ``<b>`` is a label, not markup. Only `send` and
`edit` with ``markdown=`` parse anything, and that is reserved for what Claude
itself wrote.

Every call here returns None instead of raising when the API refuses. The
caller is expected to fall back to the plain-text path — an older client, or a
rolled-back API, must not turn into silence.
"""

from __future__ import annotations

import logging
import re

from aiogram import Bot
from aiogram.types import (
    InputRichBlockParagraph,
    InputRichBlockPreformatted,
    InputRichMessage,
    Message,
    RichTextBold,
    RichTextItalic,
)

log = logging.getLogger("ccbot.rich")

# Guards rather than API limits: a single block took 30 000 characters in a
# live test, and a whole message may hold 32 768.
PRE_LIMIT = 12000
DOC_LIMIT = 30000

# U+2500..U+257F, the box-drawing block. Horizontals and verticals have an
# ASCII twin; every corner, tee and cross becomes a '+'.
_HORIZONTAL = set("─━┄┅┈┉╌╍═╴╶╸╺╼╾")
_VERTICAL = set("│┃┆┇┊┋╎╏║╵╷╹╻╽╿")


def ascii_frame(text: str) -> str:
    """Replace box-drawing with ASCII so a captured frame stays square.

    Only the glyphs whose width betrays them are touched; block elements
    (``█ ░``), arrows and everything else are left exactly as the terminal
    drew them.
    """
    out = []
    for ch in text:
        if ch in _HORIZONTAL:
            out.append("-")
        elif ch in _VERTICAL:
            out.append("|")
        elif "─" <= ch <= "╿":
            out.append("+")
        else:
            out.append(ch)
    return "".join(out)


_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|~>])")


def md_escape(text: str) -> str:
    """Neutralise Markdown in a string the bot puts around Claude's own text."""
    return _MD_SPECIAL.sub(r"\\\1", text)


# What this module knows how to build. Wider than that and the block list
# needs the API's own union.
Block = InputRichBlockParagraph | InputRichBlockPreformatted
_Piece = str | RichTextBold | RichTextItalic


def para(first: _Piece, *rest: _Piece) -> InputRichBlockParagraph:
    """A paragraph of literal text, optionally with bold or italic pieces."""
    return InputRichBlockParagraph(text=[first, *rest] if rest else first)


def bold(text: str) -> RichTextBold:
    return RichTextBold(text=text)


def italic(text: str) -> RichTextItalic:
    return RichTextItalic(text=text)


def pre(text: str, language: str | None = None,
        limit: int = PRE_LIMIT) -> InputRichBlockPreformatted:
    """A scrollable monospace block, tail-trimmed by whole lines.

    Trimming keeps the end because that is where a terminal says what just
    happened; cutting mid-line would break the alignment the block exists for.
    """
    lines = [ln.rstrip() for ln in text.rstrip().splitlines()]
    body = "\n".join(lines)
    while len(body) > limit and lines:
        lines.pop(0)
        body = "…\n" + "\n".join(lines)
    return InputRichBlockPreformatted(text=body or " ", language=language)


def _document(blocks, markdown: str | None) -> InputRichMessage | None:
    if blocks:
        return InputRichMessage(blocks=list(blocks))
    if markdown and markdown.strip():
        return InputRichMessage(markdown=markdown[:DOC_LIMIT])
    return None


async def send(bot: Bot, chat_id: int, *, blocks=None, markdown: str | None = None,
               **kw) -> Message | None:
    """Send a rich message, or None if the API would not take it."""
    doc = _document(blocks, markdown)
    if doc is None:
        return None
    try:
        return await bot.send_rich_message(chat_id=chat_id, rich_message=doc, **kw)
    except Exception:
        log.warning("send_rich_message refused — falling back to plain text",
                    exc_info=True)
        return None


async def edit(bot: Bot, chat_id: int, message_id: int, *, blocks=None,
               markdown: str | None = None, **kw) -> bool:
    """Replace a rich message in place. False means the caller should repost."""
    doc = _document(blocks, markdown)
    if doc is None:
        return False
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                    rich_message=doc, **kw)
        return True
    except Exception:
        # Unchanged, too old, or not a rich message: none of that is worth a
        # word to the user, who is looking at the card already.
        log.debug("rich edit skipped", exc_info=True)
        return False
