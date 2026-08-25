"""User settings that the bot itself can change, stored as JSON.

Kept apart from .env on purpose: .env holds secrets and machine facts a human
writes once, while this file holds choices made through the bot at runtime
(project directories today, interface language later). Mixing the two would
mean rewriting a file that may contain a token.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from . import paths

log = logging.getLogger("ccbot.settings")

_DEFAULTS: dict[str, Any] = {
    "dirs": [],          # project roots offered when starting a session
    "language": None,    # None → follow the Telegram client's language
    # Last language_code seen on the Telegram profile. Remembered because the
    # watcher speaks first, with no update to read a language off: without
    # this, Claude's answers would arrive wrapped in English while every reply
    # to a typed message came back in the profile's language.
    "telegram_language": None,
}


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = path or paths.CONFIG_FILE
        self.data: dict[str, Any] = dict(_DEFAULTS)
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("config unreadable (%s), using defaults: %s", self.path, exc)
            return
        if isinstance(raw, dict):
            self.data = {**_DEFAULTS, **raw}

    def save(self) -> None:
        paths.ensure(self.path.parent)
        # Write via a temporary file so a crash cannot leave half a config.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with open(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            Path(tmp).replace(self.path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            raise

    # -- project directories ------------------------------------------
    @property
    def dirs(self) -> list[str]:
        return [d for d in self.data.get("dirs", []) if isinstance(d, str)]

    def add_dir(self, path: str) -> bool:
        p = str(Path(path).expanduser().resolve())
        dirs = self.dirs
        if p in dirs:
            return False
        dirs.append(p)
        self.data["dirs"] = dirs
        self.save()
        return True

    def remove_dir(self, path: str) -> bool:
        dirs = self.dirs
        if path not in dirs:
            return False
        self.data["dirs"] = [d for d in dirs if d != path]
        self.save()
        return True

    # -- language ------------------------------------------------------
    @property
    def language(self) -> str | None:
        lang = self.data.get("language")
        return lang if isinstance(lang, str) else None

    @language.setter
    def language(self, value: str | None) -> None:
        self.data["language"] = value
        self.save()

    @property
    def telegram_language(self) -> str | None:
        lang = self.data.get("telegram_language")
        return lang if isinstance(lang, str) else None

    def remember_telegram_language(self, code: str | None) -> None:
        """Note the client's own language, writing only when it actually moves.

        Every update carries it, so an unconditional save would rewrite the
        config file for each message the bot ever receives.
        """
        if not code or code == self.telegram_language:
            return
        self.data["telegram_language"] = code
        self.save()
