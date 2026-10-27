"""The HTTP session the bot polls through, made to survive updates it cannot read.

aiogram validates a whole ``getUpdates`` answer as one object, so a single
update carrying something its models do not know fails the batch — and the
polling loop asks for the same offset again, forever. That is what stopped
the bot for good on 2026-10-27: a Premium rich message with a file in it
(a ``document`` block, Bot API 10.3) reached an aiogram that knew rich
messages but not that block, and every message sent after it queued up
behind it with nothing in the log but the same error every five seconds.

So a batch that fails is validated update by update, and an update that
fails has the parts the models reject cut out of it until it passes — the
paragraphs around an unknown block still arrive. What was cut is logged with
its raw JSON and handed to ``on_damaged``, because a message that lost a
part in silence is the same hole one level down.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.methods import GetUpdates, Response, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import Update
from pydantic import ValidationError

log = logging.getLogger(__name__)

# How many parts one update may lose before it is reduced to its id. Each
# cut is one validation error, and a block cut whole takes its errors with it.
_MAX_CUTS = 50


def _cut(raw: Any, loc: tuple) -> tuple[str, Any] | None:
    """Remove the deepest part of *raw* that *loc* still names.

    A pydantic location runs through union tags that are not keys of the data
    (``blocks.9.photo.photo.0``), so the walk stops at the first step the data
    does not have and removes the node it reached — a whole block rather than
    a field the block cannot live without. Returns the path and the node.
    """
    parent: Any = None
    key: Any = None
    node = raw
    walked: list[str] = []
    for step in loc:
        found = ((isinstance(node, dict) and step in node)
                 or (isinstance(node, list) and isinstance(step, int)
                     and 0 <= step < len(node)))
        if not found:
            break
        parent, key, node = node, step, node[step]
        walked.append(str(step))
    if parent is None:
        return None
    del parent[key]
    return ".".join(walked), node


def _label(node: Any, path: str) -> str:
    """How to name a removed part to a person: its type, else its field.

    A list index says nothing ("0" of ``message.photo.0``), so the nearest
    named step above it stands in.
    """
    if isinstance(node, dict) and isinstance(node.get("type"), str):
        return node["type"]
    named = [step for step in path.split(".") if step and not step.isdigit()]
    return named[-1] if named else "?"


def _message_of(raw: dict) -> int | None:
    """The id of the message an update carries, to reply to it."""
    for value in raw.values():
        if isinstance(value, dict) and isinstance(value.get("message_id"), int):
            return value["message_id"]
    return None


def _chat_of(raw: Any) -> int | None:
    """The first chat id anywhere in an update, to know whom to tell."""
    if isinstance(raw, dict):
        chat = raw.get("chat")
        if isinstance(chat, dict) and isinstance(chat.get("id"), int):
            return chat["id"]
        values = raw.values()
    elif isinstance(raw, list):
        values = raw
    else:
        return None
    for value in values:
        found = _chat_of(value)
        if found is not None:
            return found
    return None


def _repair(bot: Bot, raw: dict) -> tuple[Update, list[str]]:
    """Validate one update, cutting out what fails; returns it and the cuts."""
    labels: list[str] = []
    update_id = raw.get("update_id")    # before a cut can take it
    for _attempt in range(_MAX_CUTS):
        try:
            update = Update.model_validate(raw, context={"bot": bot})
        except ValidationError as exc:
            # The deepest error first: a shallow one may only be the echo of
            # a deeper failure, and cutting there loses a whole message for
            # one bad leaf.
            errors = sorted(exc.errors(), key=lambda e: -len(e["loc"]))
            cut = _cut(raw, tuple(errors[0]["loc"])) if errors else None
            if cut is None:
                break
            path, node = cut
            labels.append(_label(node, path))
            log.warning("update %s: cut %s (%s): %s",
                        update_id, path, errors[0]["msg"],
                        json.dumps(node, ensure_ascii=False)[:2000])
            continue
        # A cut that took the event itself (the whole message) left nothing
        # of it to deliver, which the notice has to say differently.
        if labels and not update.model_fields_set - {"update_id"}:
            labels.append("update")
        return update, labels
    # Nothing left to cut, or too much: the id alone still confirms the
    # update, so polling moves past it; aiogram skips an update with no event.
    log.error("update %s: unreadable, reduced to its id", update_id)
    labels.append("update")
    return Update.model_validate({"update_id": update_id},
                                 context={"bot": bot}), labels


class TolerantSession(AiohttpSession):
    """An aiohttp session whose ``getUpdates`` never fails on one bad update."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # chat id (None when the update names no chat), the id of the
        # message that lost parts (None if none) and what was cut.
        self.on_damaged: (Callable[[int | None, int | None, list[str]], None]
                          | None) = None

    def check_response(self, bot: Bot, method: TelegramMethod[TelegramType],
                       status_code: int, content: str) -> Response[TelegramType]:
        try:
            return super().check_response(bot, method, status_code, content)
        except Exception as exc:
            if not isinstance(method, GetUpdates):
                raise
            # Only a batch that parsed as JSON and said ok is worth repairing;
            # anything else is a real failure for the polling loop to retry.
            try:
                data = self.json_loads(content)
            except Exception:
                raise exc from None
            if not (HTTPStatus.OK <= status_code <= HTTPStatus.IM_USED
                    and isinstance(data, dict) and data.get("ok")
                    and isinstance(data.get("result"), list)):
                raise
        updates: list[Update] = []
        for raw in data["result"]:
            # Before the cuts can take them.
            chat_id, message_id = _chat_of(raw), _message_of(raw)
            update, labels = _repair(bot, raw)
            updates.append(update)
            if labels and self.on_damaged is not None:
                try:
                    self.on_damaged(chat_id, message_id, labels)
                except Exception:
                    log.exception("on_damaged failed")
        return Response[list[Update]](ok=True, result=updates)  # type: ignore[return-value]
