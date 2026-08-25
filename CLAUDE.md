# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

A Telegram dispatcher for Claude Code sessions: it drives somebody else's TUI
from the outside — in through tmux, out through transcripts. The detailed "why
it works this way" lives in `README.md` (which is long and current; there is no
need to duplicate it here).

## Running and checking

- 🔴 **The bot already runs as the systemd user unit `claude-tg-bot`**
  (`Restart=always`). **Do not start a second `main.py`** — Telegram gives
  `getUpdates` to one poller only, and both sides start failing with
  `TelegramConflictError`. Check with `systemctl --user is-active claude-tg-bot`.
- There is no hot reload. After changing code:
  `systemctl --user restart claude-tg-bot`. Claude's sessions survive it
  (`KillMode=process` — only the bot dies, the tmux server stays). `/restart` in
  the chat does the same thing.
- Installing on a clean machine is `./install.sh` (a wrapper around
  `bin/setup.py`). `./install.sh --doctor` changes nothing and checks the whole
  install: tmux, claude, venv, `.env` (with a live `getMe`), the status-line
  hook, the unit, linger, the log. It is a faster way to understand a breakage
  than reading the log by hand.
- Log: `tail -n 100 ~/.cache/ccbot/bot.log` or
  `journalctl --user -u claude-tg-bot -n 100 --no-pager`; in the chat, `/log`.
  `CCBOT_LOG_LEVEL=DEBUG` in `.env` adds every tmux command.
- Python is `.venv/bin/python` only (3.14, the venv was made by `uv`; **it has
  no pip** — install packages with `uv pip install`). Dependencies: aiogram 3,
  python-dotenv, Pillow; dev tools in `requirements-dev.txt` (ruff,
  basedpyright, Babel). Babel is needed ONLY for working on translations; the
  bot itself reads the compiled `.mo` and does without it.
- 🔴 **Run `bin/check.sh` before committing** (ruff → basedpyright → import
  check → locales). The same script is installed as a pre-commit hook; after a
  clone, enable it once with `git config core.hooksPath bin/githooks`.
  Configuration lives in `pyproject.toml`. The gate has to stay green: do not
  silence a rule with `# noqa` when the cause can be fixed instead. The
  emergency bypass is `git commit -n`.
- There is no test directory in the repo (the docstring in `ccbot/screen.py`
  refers to `tests/samples/`, which does not exist). Check the parser against a
  live sample: `tmux capture-pane -p -t @N > <scratchpad>/dialog.txt`, then run
  it through `screen.find_dialog()` from a script in the scratchpad.
- Live data close at hand: `tmux ls` / `tmux list-windows -t ccbot`,
  `claude agents --json`, `~/.claude/projects/*/<uuid>.jsonl`,
  `~/.cache/ccbot/status/<session_id>.json`.

- 🔴 **Several agents often work in this repo at once** (the bot's sessions live
  in tmux windows of their own, including in this very directory). Before
  committing, look at `git log --oneline -3` and `git status`: a neighbouring
  session may already have committed the changes you just made to the tree.

## Architecture

Managed sessions are windows of a single tmux server (`session ccbot`), each
running `claude --session-id <uuid> -n <name>`. Control is two-way and
asymmetric:

| Direction | Mechanism | Module |
|---|---|---|
| In (prompts, option digits, `/`-commands, Esc, Shift+Tab) | `tmux paste-buffer` / `send-keys` | `tmux.py` |
| Out (the text of the answers) | incremental reads of `~/.claude/projects/*/<uuid>.jsonl` | `transcript.py` |
| Blocking dialogs, the spinner, the status line | `tmux capture-pane` + parser | `screen.py` |
| Exact metrics (context, limits, model, cost) | the status-line payload via `bin/statusline-tee.sh` | `status_feed.py` |
| The list of live sessions | `claude agents --json` | `sessions.py` |
| Interface language (gettext, `locales/`) | a middleware on each update + a context in the watcher tick | `i18n.py` |

The `Watcher._tick()` loop (`watcher.py`, every `CCBOT_POLL_INTERVAL` seconds,
1.5 by default): rebind after `/clear` → read the transcript into the buffer →
capture the screen → usage thresholds → flush the buffer (once it has been
quiet for ≥2 s, or a dialog appeared, or the buffer is older than 25 s) → the
"⏳ working" pulse → render the dialog with buttons.

The remaining modules: `bot.py` — aiogram handlers, callbacks, the session
lifecycle; `state.py` — SQLite (managed sessions, the active one per chat,
`msg_routes`, the history cache); `sessions.py` — one view over
managed/foreign/closed; `keyboards.py` — inline keyboards and the
`callback_data` format; `paths.py` — XDG paths; `settings.py` —
`~/.config/ccbot/config.json`; `service.py` — the bot's own process (uptime,
version, restart); `media.py`, `render.py` (ASCII→PNG), `util.py`,
`logsetup.py`.

## The bot's state

`~/.local/share/ccbot/state.db` is SQLite the bot maintains itself (no ORM and
no Alembic): the tables `managed` (managed sessions), `active` (the active
session per chat), `msg_routes` (which message belongs to which session) and a
cache of the history scan. The schema is created at startup with
`CREATE TABLE IF NOT EXISTS`, and structural changes are hand-written
`ALTER TABLE` statements inside `Store` (`_add_custom_name`, for instance). A
"migration" here is a line of code that runs the first time a new version
starts.

The consequence to keep in mind: **the database is shared by every build that
is running**. New code that adds a column changes it for the older process that
is still alive, too. That is why rows are read tolerantly
(`_row_to_managed`), and why the bot has to be restarted after a schema change.

## Invariants that are easy to break

- **`Managed.name` is the launch name (`claude -n`) and it is untouchable.**
  `/clear` does not restart Claude, but it does hand out a **new session_id**,
  and the only thread back is a `(name, cwd)` match in `claude agents --json`
  (`Watcher._rebind_cleared` → `Store.rebind`). The user-facing name lives
  separately, in `custom_name`. Change `name` and the session is lost after the
  very first `/clear`.
- **session_id is not stable.** Never cache it outside `Store`; after a rebind
  the offset resets to zero and `title` to NULL.
- **Register every outgoing message with `store.remember_message()`**
  (`Watcher._say`/`_say_html`, `CCBot._ack`). That is what makes reply
  addressable; a message that was not recorded is "blind", and a reply to it
  goes to the active session rather than the one on the screen.
- **Take the target of an action from `CCBot._target(m)`, not `_active()`** — it
  accounts for the reply and makes that session active. This applies to commands
  as well: a `/clear` sent as a reply to one session's message has to clear that
  session's context.
- **User text goes through `tmux.paste_text()` only** (bracketed paste).
  `send-keys -l` would submit a multi-line prompt at the first newline.
- **A new bot `/command` means four places**: the handler in `_register()`, the
  `OWN_COMMANDS` set, the `CCBot.MENU` list, and the text of `help_text()`.
  Forget `OWN_COMMANDS` and the command silently travels to Claude as text.
- **`callback_data` is limited to 64 bytes**: the session id is cut to 8
  characters (`sid8`) and resolved back by prefix (`_resolve`, `_find_foreign`,
  `_find_closed`).
- **Three kinds of session.** `managed` — in tmux, full control; `foreign` —
  somebody else's terminal, view only (stdin is out of reach; "move" means
  SIGTERM + `--resume`); `closed` — a transcript, brought back with `--resume`.
- **`screen.py` is the only place that knows what the TUI looks like.** A dialog
  is recognised by its footer, and there are two footers: `Enter to select…`
  (choosing an option) and `Esc to cancel · Tab to amend · ctrl+e to explain` (a
  permission request from a tool or a `PreToolUse` hook). Options are searched
  **bottom-up from the footer to "1."** — top-down, the parser latches onto
  Claude's numbered prose. A Claude Code update breaks this file specifically.
- **One dialog has no footer at all**: the review step of a multi-part
  `AskUserQuestion`, which draws its section tabs
  (`←  ☒ Language  ☐ Style  ✔ Submit  →`), echoes the answers, and ends with
  `Ready to submit your answers?` and two options. `_submit_footer_index`
  synthesises the footer position, and it demands *all* of: no mode/status line
  anywhere on the pane (an open dialog covers them, so seeing one proves the
  bottom belongs to the ordinary UI), the tabs, the marker, and an option list
  that opens at "1." and follows the marker directly. Every one of those
  strings gets printed in this repository while it is being worked on — a
  heredoc full of this very documentation was reported as two questions in the
  chat on 2026-08-25, and the digits the user pressed landed in the session's
  input line. Weaken any of those conditions and it happens again.
- 🔴 **A dialog redraws in stages.** The row that was chosen leaves the screen
  before the rest of the dialog does, so a frame captured mid-redraw looks like
  a new question with options missing. `Watcher._tick_session` therefore
  reports no *new* dialog within `_DIALOG_SETTLE` of the bot's own keypress
  (`note_input`), the same mark `_report_blocked` uses.
- **The reasoning behind a question is on the screen and nowhere else.** Claude
  Code writes an assistant record only when the tool call inside it returns,
  and `AskUserQuestion` returns on a human — so the analysis the options are
  about reaches the transcript *after* the answer was given. `Watcher._preface`
  reads it off the terminal (`screen.said_above_dialog`, 400 lines of
  scrollback) before rendering the dialog, and `SessionRuntime.echoes_screen`
  drops the transcript's own copy when it finally arrives (word overlap, not
  equality: the terminal renders markdown and wraps to its width). A tool call
  that has already printed output means the turn *was* recorded, so nothing is
  read above it.
- 🔴 **An unrecognised dialog has no right to be silence.** When
  `claude agents --json` says `waiting` and `find_dialog()` returned None,
  `Watcher._report_blocked` sends the screen with buttons (digits/arrows/Esc)
  that work without any parsing. When adding a new kind of screen, keep that
  safety net.
- 🔴 **Do NOT build deduplication of repeated messages on the raw screen.** The
  terminal does not stand still: the spinner turns, the timer ticks, the status
  line repaints percentages, and the output scrolls on top of that. A signature
  taken from `capture-pane` changes every tick — that is how one question turned
  into 28 chat messages (2026-08-24). The boundary of an episode is the
  **session state** (`agent_status`), not the content; for comparing content
  there is `screen.quiet_signature()`, which drops volatile lines and flattens
  numbers. After that, only `edit_message_text` (an edit raises no
  notification); a new message is for a new episode only.
- 🔴 **`bin/setup.py` runs on the system Python, BEFORE the venv exists.** So it
  contains stdlib only: no `aiogram`, no `dotenv`, and no imports from `ccbot/`.
  Importing from the package looks convenient (`paths.py` is right there), but
  it kills the installer on exactly the machine it was written for. For the same
  reason it parses `.env` with a short loop of its own rather than with
  `dotenv`.
- **The install steps live in two places deliberately**: `bin/setup.py` and the
  "Manual install" section of the README. Change one and look at the other, or a
  hand-built install will drift away from a scripted one.
- **The bot's own files go through `paths.py` only** (XDG: config/data/cache).
  It writes nothing next to the code; `.env` is the single configuration file a
  human edits.
- **Metrics: `status_feed` first, `screen.read_status` as the fallback.** Do not
  parse bars off the screen where a payload exists.
- **Telegram limits**: long text goes through `util.split_text`, terminal output
  through `as_pre`/`as_pre_lines`. A mobile client **does not scroll** `<pre>`,
  it wraps it, so an ASCII diagram wider than 36 columns is sent as an image
  (`render.text_to_png`).
- **`c.message` is not guaranteed.** For a card older than 48 hours Telegram
  serves `InaccessibleMessage` (or `None`) — it can be neither edited nor
  replied to. Take the message through `_editable()` only, never `c.message.…`.
- **Read the database schema tolerantly.** `Store._row_to_managed` discards
  unknown columns: the database outlives the process, and a newer build that
  added a column would otherwise kill every older bot still running (which is
  exactly what happened on 2026-08-24).
- 🔴 **No error goes silent.** An exception in a handler is caught by
  `@dp.errors` (`bot.py`) and answered in the chat; an exception in a watcher
  tick goes through `Watcher._report_failure` (once an hour for the same
  breakage, plus "✅ working again" once it recovers). A new background loop or
  separate task must report the same way — otherwise a breakage looks like
  Claude being quiet.
- 🔴 **UI strings go through gettext only (`ccbot/i18n.py`).** The base language
  of the code is English; Ukrainian lives in `locales/uk`. New text for the chat
  means `_()` (or `ngettext()` when it contains a number), then
  `pybabel extract` → `update` → translate → `compile`. The commands are in the
  README, under "Adding a language". Comments, docstrings and **logs** are in
  English and WITHOUT `_()`: they are for the developer.
- **`_()` at module level is not allowed** — the constant is evaluated at import
  time, when the language is not known yet, and the first language sticks
  forever. For text that used to be a constant, make it a function
  (`help_text()`, `restart_ask()`, `no_supervisor()`); for tables looked up by
  key (`MENU`, `_LIMIT_LABELS`, `_DOW`, `_STATUS_LABELS`), use `N_()` at the
  definition and `_()` where they are read.
- 🔴 **`_` can no longer be used as a throwaway variable** (`for _ in
  range(...)`, `_, a, b = ...`): it shadows the gettext import and the function
  dies with `UnboundLocalError` on the first `_("…")`. Ruff catches only the
  loop form, never tuple unpacking.
- **Placeholders are named** (`{name}`, not `{}`), emoji go inside the string,
  and format specifications (`{n:02d}`) stay outside it: everything a translator
  can see is something a translator can break. HTML travels with the string as a
  whole.
- **The watcher has a locale context of its own.** A ContextVar is copied when
  the task is created, so a background loop would never see a language chosen
  later — `_loop()` opens `use_locale(self._locale())` on every tick. Any new
  background task that writes to the chat has to do the same.
