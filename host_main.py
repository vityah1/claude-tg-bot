"""Entry point of the session host: python host_main.py

Runs as its own systemd user unit (`claude-tg-host`), next to the bot. The bot
may be restarted at will; this process holds the Claude sessions and is
restarted only when it itself changes.
"""

import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler

from ccbot import paths
from ccbot.host.server import Host

_FMT = "%(asctime)s %(levelname)-7s %(name)-16s %(message)s"


def _logging() -> None:
    level = getattr(logging, (os.getenv("CCBOT_LOG_LEVEL") or "INFO").upper(), logging.INFO)
    paths.ensure(paths.cache_dir())
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter(_FMT)
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)
    rotating = RotatingFileHandler(paths.HOST_LOG_FILE, maxBytes=2 * 1024 * 1024,
                                   backupCount=3, encoding="utf-8")
    rotating.setFormatter(fmt)
    root.addHandler(rotating)
    # The SDK logs every control frame at DEBUG; INFO is enough here.
    logging.getLogger("claude_agent_sdk").setLevel(max(level, logging.INFO))


def main() -> None:
    _logging()
    asyncio.run(Host().serve())


if __name__ == "__main__":
    main()
