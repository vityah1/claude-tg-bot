"""Entry point: python main.py"""

import asyncio
import logging

from ccbot.bot import CCBot
from ccbot.config import load
from ccbot.logsetup import setup


def main() -> None:
    cfg = load()
    path = setup(cfg.log_level)
    logging.getLogger("ccbot").info("logging to %s at %s", path, cfg.log_level)
    asyncio.run(CCBot(cfg).run())


if __name__ == "__main__":
    main()
