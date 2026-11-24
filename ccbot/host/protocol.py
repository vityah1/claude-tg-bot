"""Wire format between the bot and the session host. Standard library only.

Newline-delimited JSON over a Unix socket. The bot opens with
`{"op": "hello", "last_seq": N}`; the host answers `{"type": "welcome", ...}`,
replays every journalled event after N, then streams new ones. Commands carry
an `id`, and their answer is `{"type": "reply", "re": id, "ok": bool, ...}`.

Journalled events have a `seq` that grows across host restarts; the bot acks
the highest one whose effect has reached the chat, and that is where a replay
starts after the bot (or the socket) comes back. `epoch` changes with every
host start, which is how the bot tells "the socket dropped" from "the host
was restarted and lost its in-flight turns".
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .. import paths

PROTO = 1
# A frame bigger than this is a bug, not a message: the host cuts tool input
# and text before it gets anywhere near it.
MAX_FRAME = 8 * 1024 * 1024


def runtime_dir() -> Path:
    """Where the socket lives: tmpfs, private to the user, gone on reboot."""
    raw = os.getenv("XDG_RUNTIME_DIR")
    base = Path(raw) if raw else paths.cache_dir().parent
    return base / paths.APP


def socket_path() -> Path:
    raw = os.getenv("CCBOT_HOST_SOCKET")
    return Path(raw).expanduser() if raw else runtime_dir() / "host.sock"


def encode(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
            + "\n").encode()


def decode(line: bytes) -> dict[str, Any]:
    obj = json.loads(line)
    if not isinstance(obj, dict):
        raise ValueError("frame is not an object")
    return obj
