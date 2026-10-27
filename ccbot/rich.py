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
from typing import NamedTuple

from aiogram import Bot
from aiogram.types import (
    InputRichBlockParagraph,
    InputRichBlockPreformatted,
    InputRichMessage,
    Message,
    RichBlockCaption,
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


# ------------------------------------------------------------------ incoming
# A Premium client sends formatted text as a rich message too: no ``text``,
# no ``caption``, no ``photo`` — only ``rich_message.blocks``, with the
# pictures as blocks among the paragraphs. A bot that reads the classic
# fields sees an empty message, and one such message vanished without a word
# on 2026-10-20 (13 paragraphs and a screenshot, id 37497).

# The block kinds that carry a file: the attribute it sits under, the MIME
# type to assume and the suffix for a file Telegram gives no name.
_MEDIA_BLOCKS = {
    "photo": ("photo", "image/jpeg", ".jpg"),
    "video": ("video", "video/mp4", ".mp4"),
    "animation": ("animation", "video/mp4", ".mp4"),
    "audio": ("audio", "audio/mpeg", ".mp3"),
    "voice_note": ("voice_note", "audio/ogg", ".oga"),
    # A file of any format (Bot API 10.3): its own MIME type and name, if it
    # has them, win — the same fallback as a classic document's.
    "document": ("document", "", ".bin"),
}

# Inline styles Markdown can say; anything else keeps just its text.
_INLINE_WRAP = {
    "bold": "**",
    "italic": "*",
    "strikethrough": "~~",
    "code": "`",
}


class Media(NamedTuple):
    """One file found among the blocks: the Telegram object and how to name it."""

    obj: object
    mime: str
    suffix: str


def _kind(node) -> str:
    return str(getattr(getattr(node, "type", None), "value",
                       getattr(node, "type", "")) or "")


def _inline(node) -> str:
    """One piece of rich text as Markdown, the way Claude reads it best."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_inline(n) for n in node)
    kind = _kind(node)
    if kind == "custom_emoji":
        return str(getattr(node, "alternative_text", "") or "")
    if kind == "mathematical_expression":
        return f"${getattr(node, 'expression', '')}$"
    if kind == "anchor":
        return ""
    inner = _inline(getattr(node, "text", None))
    if kind == "url":
        url = str(getattr(node, "url", "") or "")
        if not inner.strip():
            return url
        return f"[{inner}]({url})" if url and inner != url else inner
    wrap = _INLINE_WRAP.get(kind)
    core = inner.strip()
    if not wrap or not core:
        return inner
    if kind == "code" and "`" in core:
        wrap = "`` "
        close = " ``"
    else:
        close = wrap
    # Markdown ignores "**word **": the spaces a client leaves inside a run
    # go outside the markers.
    lead = inner[:len(inner) - len(inner.lstrip())]
    trail = inner[len(inner.rstrip()):]
    return f"{lead}{wrap}{core}{close}{trail}"


def _caption(block) -> list[str]:
    cap = getattr(block, "caption", None)
    if cap is None:
        return []
    # A table's caption is plain rich text; a media block's is a
    # RichBlockCaption with a credit line of its own.
    if isinstance(cap, RichBlockCaption):
        text, credit = _inline(cap.text), _inline(cap.credit)
    else:
        text, credit = _inline(cap), ""
    return [t for t in (text, f"— {credit}" if credit else "") if t.strip()]


def _table(cells) -> list[str]:
    """Rows as a Markdown table; a spanned cell repeats as empty columns."""
    rows = []
    for row in cells or []:
        out_row = []
        for c in row:
            text = " ".join(_inline(getattr(c, "text", None)).split())
            out_row.append(text.replace("|", "\\|"))
            out_row += [""] * (max(1, int(getattr(c, "colspan", 1) or 1)) - 1)
        rows.append(out_row)
    width = max((len(r) for r in rows), default=0)
    if not width:
        return []
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return out


def _blocks(blocks, media: list) -> list[str]:
    lines: list[str] = []
    for block in blocks or []:
        lines.extend(_block(block, media))
    return lines


def _block(block, media: list) -> list[str]:
    """One block as Markdown lines; its file, if any, goes into *media*."""
    kind = _kind(block)
    if kind in _MEDIA_BLOCKS:
        attr, mime, suffix = _MEDIA_BLOCKS[kind]
        obj = getattr(block, attr, None)
        if isinstance(obj, list):       # a photo comes as its sizes
            obj = obj[-1] if obj else None
        if obj:
            media.append(Media(obj, mime, suffix))
        return _caption(block)
    if kind == "paragraph" or kind == "footer" or kind == "thinking":
        return [_inline(getattr(block, "text", None))]
    if kind == "heading":
        size = int(getattr(block, "size", 2) or 2)
        return ["#" * max(1, min(size, 6)) + " " + _inline(block.text)]
    if kind == "pre":
        lang = getattr(block, "language", None) or ""
        return [f"```{lang}", _inline(block.text), "```"]
    if kind == "divider":
        return ["---"]
    if kind == "mathematical_expression":
        return [f"$${getattr(block, 'expression', '')}$$"]
    if kind == "anchor":
        return []
    if kind == "list":
        out = []
        for item in getattr(block, "items", None) or []:
            mark = getattr(item, "label", "") or "-"
            if getattr(item, "has_checkbox", False):
                mark += " [x]" if getattr(item, "is_checked", False) else " [ ]"
            body = _blocks(getattr(item, "blocks", None), media) or [""]
            out.append(f"{mark} {body[0]}")
            out += ["  " + ln for ln in body[1:]]
        return out
    if kind in ("blockquote", "expandable_blockquote", "pullquote"):
        body = (_blocks(block.blocks, media) if kind == "blockquote"
                else [_inline(getattr(block, "text", None))])
        credit = _inline(getattr(block, "credit", None))
        if credit:
            body.append(f"— {credit}")
        return ["> " + ln if ln else ">" for ln in body]
    if kind == "details":
        return [_inline(getattr(block, "summary", None)),
                *_blocks(getattr(block, "blocks", None), media)]
    if kind == "table":
        return _table(getattr(block, "cells", None)) + _caption(block)
    if kind == "map":
        loc = getattr(block, "location", None)
        where = (f"📍 {loc.latitude}, {loc.longitude}" if loc is not None else "")
        return [where, *_caption(block)]
    # A collage, a slideshow, or a kind newer than this code: whatever it
    # nests and says is still worth more than nothing.
    out = _blocks(getattr(block, "blocks", None), media)
    text = getattr(block, "text", None)
    if text is not None:
        out.insert(0, _inline(text))
    return out + _caption(block)


def read(rich_message) -> tuple[str, list[Media]]:
    """What a received rich message says, and the files it carries.

    The text comes back as Markdown, one block per line and runs of blank
    lines folded into one; the files in reading order, each with the MIME
    type and suffix to assume when Telegram does not say.
    """
    media: list[Media] = []
    lines = _blocks(getattr(rich_message, "blocks", None), media)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(ln.rstrip() for ln in lines))
    return text.strip(), media
