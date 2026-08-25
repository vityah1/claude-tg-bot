"""Runtime configuration, loaded from .env next to the project root."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Config:
    token: str
    allowed: frozenset[int]
    poll_interval: float = 1.5
    # Empty means "no shortlist": directories are discovered from the
    # user's own Claude history instead of being baked into the code.
    dirs: tuple[str, ...] = ()
    log_level: str = "INFO"

    @property
    def owner(self) -> int:
        """Chat that unsolicited notifications go to."""
        return next(iter(sorted(self.allowed)))


def load() -> Config:
    token = (os.getenv("TG_BOT_TOKEN") or "").strip()
    if not token:
        raise SystemExit(
            "TG_BOT_TOKEN is not set. Create a bot with @BotFather "
            "and put the token in .env"
        )
    raw = (os.getenv("TG_ALLOWED_USER_IDS") or "").replace(";", ",")
    ids = frozenset(int(p) for p in (x.strip() for x in raw.split(",")) if p)
    if not ids:
        raise SystemExit(
            "TG_ALLOWED_USER_IDS is not set — the bot would be open to everyone"
        )
    raw_dirs = (os.getenv("CCBOT_DIRS") or "").strip()
    dirs = tuple(d.strip() for d in raw_dirs.split(",") if d.strip())
    return Config(
        token=token,
        allowed=ids,
        poll_interval=float(os.getenv("CCBOT_POLL_INTERVAL") or 1.5),
        dirs=dirs,
        log_level=(os.getenv("CCBOT_LOG_LEVEL") or "INFO").upper(),
    )
