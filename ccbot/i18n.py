"""Interface language: one place that knows which locale is in effect.

gettext rather than JSON dictionaries, because `.po` is what translation
platforms (Crowdin, Weblate, POEditor) read natively — a translator adds a
language without touching Python — and because `ngettext` gets plural forms
right in languages that have more than two of them.

The base language is English: `msgid` in the source is English text, so a
contributor who does not read the translation still understands the code.

Two ways in, and both must work:

* handlers run under `LocaleMiddleware`, which sets the locale per update;
* the watcher is a background task with no update to look at, so it opens the
  context itself once per tick (`use`). ContextVars are copied when a task is
  created, so a language chosen later would otherwise never reach it.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from aiogram.types import TelegramObject
from aiogram.utils.i18n import I18n, ngettext
from aiogram.utils.i18n import gettext as _
from aiogram.utils.i18n import lazy_gettext as __
from aiogram.utils.i18n.middleware import I18nMiddleware

from .settings import Settings

log = logging.getLogger("ccbot.i18n")

__all__ = [
    "DEFAULT_LOCALE",
    "N_",
    "LocaleMiddleware",
    "_",
    "__",
    "i18n",
    "install",
    "language_name",
    "ngettext",
    "offered",
    "resolve",
    "use",
]


def N_(message: str) -> str:
    """Mark a string for translation without translating it yet.

    For tables built at import time — limit names, weekday names — where the
    language is not known and the value is looked up later. `pybabel extract`
    collects it; the lookup site passes the result through `_()`.
    """
    return message


LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"
DOMAIN = "bot"
# English is the base: its msgid *is* the source string, so a missing or stale
# en.mo degrades into correct English rather than into blanks.
DEFAULT_LOCALE = "en"

i18n = I18n(path=LOCALES_DIR, default_locale=DEFAULT_LOCALE, domain=DOMAIN)

# Every language is written the way its own speakers write it — a picker that
# says "Ukrainian" is useless to someone who cannot read English. Adding a
# language means adding one line here; unknown codes fall back to the code.
LANGUAGE_NAMES = {
    "en": "English",
    "uk": "Українська",
}


def offered() -> tuple[str, ...]:
    """Languages `/lang` may offer: the compiled ones, plus the base."""
    found = set(i18n.available_locales) | {DEFAULT_LOCALE}
    return tuple(sorted(found))


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get(code, code.upper())


def resolve(chosen: str | None, telegram_code: str | None) -> str:
    """Which language to speak: the setting, then the Telegram profile, then English.

    The profile is a good enough guess that asking at first contact would only
    be noise; `/lang` exists for when the guess is wrong. A profile language
    the bot has no translation for must land on English, not on blank strings,
    so anything unknown falls through.
    """
    known = offered()
    if chosen and chosen in known:
        return chosen
    if telegram_code:
        # Telegram sends IETF tags ("uk-UA", "pt-BR"); match the language part.
        base = telegram_code.replace("_", "-").split("-")[0].lower()
        if base in known:
            return base
    return DEFAULT_LOCALE


def install() -> None:
    """Make the i18n context global, and log what was actually loaded.

    Called once at startup, before any background task exists: `set_current`
    writes a ContextVar, and a task only inherits what was set before it was
    created. Without this the watcher's `_()` would raise LookupError.
    """
    i18n.set_current(i18n)
    log.info("locales loaded from %s: %s", LOCALES_DIR,
             ", ".join(offered()) or "none")


@contextmanager
def use(locale: str) -> Generator[None, None, None]:
    """Speak *locale* inside this block — for code no middleware wraps."""
    with i18n.context(), i18n.use_locale(locale):
        yield


class LocaleMiddleware(I18nMiddleware):
    """Per-update locale, read fresh so `/lang` takes effect immediately."""

    def __init__(self, settings: Settings):
        super().__init__(i18n=i18n)
        self.settings = settings

    async def get_locale(self, event: TelegramObject, data: dict[str, Any]) -> str:
        user = data.get("event_from_user")
        code = getattr(user, "language_code", None)
        # Kept for the watcher, which speaks first and has no user to ask.
        self.settings.remember_telegram_language(code)
        return resolve(self.settings.language, code)
