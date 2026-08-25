"""Where the bot keeps its files.

Follows the XDG Base Directory spec rather than scattering files next to the
code: the same checkout may be shared, read-only, or packaged, and a user's
own data has no business living inside it.

    config  ~/.config/ccbot/       settings a user edits or the bot writes
    data    ~/.local/share/ccbot/  state that must survive (sessions, routes)
    cache   ~/.cache/ccbot/        rebuildable (logs, attachments, payloads)
"""

from __future__ import annotations

import os
from pathlib import Path

APP = "ccbot"


def _base(env: str, default: Path) -> Path:
    raw = os.getenv(env)
    root = Path(raw).expanduser() if raw else default
    return root / APP


def config_dir() -> Path:
    return _base("XDG_CONFIG_HOME", Path.home() / ".config")


def data_dir() -> Path:
    return _base("XDG_DATA_HOME", Path.home() / ".local" / "share")


def cache_dir() -> Path:
    return _base("XDG_CACHE_HOME", Path.home() / ".cache")


def ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


CONFIG_FILE = config_dir() / "config.json"
STATE_DB = data_dir() / "state.db"
LOG_FILE = cache_dir() / "bot.log"
MEDIA_DIR = cache_dir() / "media"
STATUS_DIR = cache_dir() / "status"
