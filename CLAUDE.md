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
| Which Claude Code a session runs, and which is on disk | the payload's `version` / `claude --version` | `updates.py` |
| Interface language (gettext, `locales/`) | a middleware on each update + a context in the watcher tick | `i18n.py` |
| The laptop's Windows power mode | `powercfg.exe` / `reg.exe` over WSL interop | `winpower.py` |

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
version, restart); `rich.py` (Bot API 10.x rich messages); `media.py`,
`render.py` (ASCII→PNG, now only a fallback), `util.py`,
`logsetup.py`; `winpower.py` — the laptop's Windows power mode, reached
from WSL through interop and inert anywhere else.

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
- **User text goes through `tmux.submit_text()` only** (bracketed paste,
  then Enter, both under one lock per window). `send-keys -l` would submit a
  multi-line prompt at the first newline, and an unlocked paste interleaves
  with the next one: the buffer name used to be derived from the window alone,
  and `paste-buffer -d` deletes it, so two concurrent prompts became
  `no buffer ccbot-7` (11 of 13 forwarded messages, 2026-08-27).
- 🔴 **Incoming messages are batched, not forwarded one by one.** A forwarded
  conversation arrives as a message per bubble in a single poll, and aiogram
  runs an update per task — order is not guaranteed and neither is spacing.
  `CCBot._hold` queues them per chat, `_inbox_loop` waits for `_INBOX_QUIET`
  of quiet (and for every attachment download to finish), and `_flush_inbox`
  sends **one** prompt sorted by `message_id`, with the target resolved then
  (`_batch_target`: the first reply naming a session wins). A forwarded item
  keeps its author from `forward_origin`, which is also what tells a forwarded
  batch from the user's own typing (`media.build_batch_prompt`). Anything that
  sends a prompt outside this path is back to one Enter per message.
- 🔴 **An attachment is never filtered by format.** `_attachment()` takes every
  kind Telegram has (photo, document, video, audio, voice, video note,
  animation, sticker) and hands the path over whatever the MIME type says: a
  JSON, a log, an archive are all things Claude reads or unpacks himself, and
  the whitelist that used to be there ("I only take images and PDFs") answered
  a forwarded conversation dump with a refusal. The only limit left is
  Telegram's own 20 MB for a bot download, and the message for it says to send
  a path instead. The handler filter has to list every kind too — an unlisted
  one matches no handler at all and vanishes from the batch in silence.
  A file keeps the name it was sent with (`media.safe_name`, `-1` on a
  collision, never an invented extension); only the nameless kinds get the
  timestamp. `media.is_image()` is what decides whether the prompt says "image"
  or "file" — announcing a JSON as an image sends Claude looking for a picture.
- 🔴 **A session restart is `/exit` + `claude --resume <id> -n <name>` in the
  same window, and the window is never killed** (`CCBot._restart_session`).
  `--resume` keeps the session id — verified against the live CLI on
  2026-08-30: same `sessionId` in `claude agents --json`, same transcript file,
  appended to rather than replaced. That is what makes the restart cheap:
  the reader's offset stays valid, so nothing is replayed into the chat, and
  `adopt()` must *not* be called. The launch name has to come back exactly as
  it was, or the session is lost at its first `/clear` (see `Managed.name`).
  Unlike `_close`, a restart that fails leaves the session standing: no
  `kill_window` on any path.
- 🔴 **A restart checks that `claude` is on disk before it stops anything.**
  Claude Code updates itself by replacing the binary in place: for a second or
  two the path is missing, and briefly it is half-written (bash: `Exec format
  error`). `/exit` inside that window leaves the session at a shell prompt with
  nothing to resume it — which is what happened to one session on 2026-08-30,
  mid-`upd:all`. Hence the pre-flight check, plus a second `_launch_resume`
  attempt, plus a failure message that says the window is intact and pressing
  again will work (it does: with no `claude` running, the restart skips `/exit`
  and goes straight to the resume).
- 🔴 **A restart refuses a busy or waiting session, and asks twice.**
  `claude agents --json` is asked with `force=True` (the five-second cache
  would refuse a session that had *just* finished), and the screen is read on
  top of that — a session relaunched seconds ago is missing from the agent
  list entirely, and without the screen check a second press would walk over
  the prompt it had meanwhile been given. Neither source alone is enough: the
  agent list knows about dialogs `find_dialog()` cannot parse (`/model` is
  one), the screen knows about work the list has not caught up with.
- **`screen.is_busy()` needs the spinner line, not only the interrupt hint.**
  "esc to interrupt" joins the spinner a few seconds into a turn, so the hint
  alone reads the first seconds of every turn as idle (2.1.251:
  `✻ Smooshing… (1s · thinking)`). The line is matched by `_WORKING_RE`, which
  demands the ellipsis: `_ACTIVITY_RE` accepts the `●` that also heads Claude's
  prose, and without the ellipsis "● I ran it (3s later)" reads as hard at work.
- **A new bot `/command` means four places**: the handler in `_register()`, the
  `OWN_COMMANDS` set, the `CCBot.MENU` list, and the text of `help_text()`.
  Forget `OWN_COMMANDS` and the command silently travels to Claude as text.
- **`callback_data` is limited to 64 bytes**: the session id is cut to 8
  characters (`sid8`) and resolved back by prefix (`_resolve`, `_find_foreign`,
  `_find_closed`).
- 🔴 **A closed session is one that nothing owns, and history is per
  directory.** `sessions.closed_all()` drops a transcript whose id is live in
  `claude agents --json` **or** present in `Store.all_managed()`: after
  `/clear` the running process writes under a new id, so its old file looks
  closed, and resuming it would take over the database row of a session still
  sitting in a tmux window. The list is filtered by exact `cwd` and paged
  (`HISTORY_PAGE`) because one project holds dozens of transcripts — 88 in
  `pay4say` on 2026-09-01, of which a global list of twelve showed four. A
  card is therefore resolved by `sessions.closed_view()`, a direct glob on the
  file name (the id *is* the file name), never by scanning the newest N; and
  the directory in `callback_data` is a hash of the path (`sessions.dir_key`),
  never its position in the list, which moves as sessions open and close.
- 🔴 **A name the user gave outlives the session that had it.** `/rename`
  writes both to `managed.custom_name` and to `session_names`, which
  `Store.remove()` does *not* touch — a name kept only in `managed` is lost
  exactly when it becomes useful, in the list of closed sessions. `rebind`
  moves it to the new id (the work continues there, `/clear` or no `/clear`),
  and `Store.add()` reads it back, so a resumed session comes up under its own
  name. Everything else about the row is disposable; this is not.
- 🔴 **A row's caption has four sources, in this order**
  (`sessions._closed_view`): the saved name, then `claude -n` **when a human
  wrote it** (`hand_written` rejects the bot's own `folder-1a2b`), then
  `ai-title`, then the first prompt. The order matters because the last one is
  the usual case: Claude Code all but stopped writing `ai-title` in late
  August 2026 — 0 of 9 sessions on 30.08, 1 of 16 on 31.08 — and it writes it
  near the *start* of the transcript, so a tail-only scan finds almost none.
- 🔴 **A prompt is cleaned before it becomes a caption**
  (`sessions.clean_prompt`, on text that still has its line breaks — hence
  `transcript.prompt_text` next to `prompt_from_line`). The bot's own wrapping
  is what would otherwise be shown: an attachment prompt opens with a heading
  and the media paths, a forwarded batch with "Forwarded from Telegram, 8
  messages". Six of the newest twenty-four pay4say rows read as a media path
  on 2026-09-01. The headings are matched **structurally** (a line that is a
  path; a heading followed by paths) and never by their wording — they are
  translated, and an English pattern would slip straight past a Ukrainian
  install.
- **Three kinds of session.** `managed` — in tmux, full control; `foreign` —
  somebody else's terminal, view only (stdin is out of reach; "move" means
  SIGTERM + `--resume`); `closed` — a transcript, brought back with `--resume`.
- **`screen.py` is the only place that knows what the TUI looks like.** A dialog
  is recognised by its footer, and there are two footers: `Enter to select…`
  (choosing an option) and `Esc to cancel · Tab to amend · ctrl+e to explain` (a
  permission request from a tool or a `PreToolUse` hook). Options are searched
  **bottom-up from the footer to "1."** — top-down, the parser latches onto
  Claude's numbered prose. A Claude Code update breaks this file specifically.
- 🔴 **The model list and the effort levels are never written down in the bot.**
  They are read off `/model` and `/effort`, which are dialogs of their own
  shape (`screen._model_dialog` / `_effort_dialog`, measured on 2.1.258) and
  used to reach the chat as "a screen I do not understand". Both are worth
  knowing exactly:
  * The picker's footer is `Enter to set as default · s to use this session
    only · Esc to cancel`, and its rows carry a label and a description on one
    line, held apart by padding, with a `✔` on the one in force. **A digit
    commits *and* saves the pick as the default for new sessions** — the same
    thing `/model <alias>` does, which is what the old hard-coded buttons sent.
    "This session only" is `s`, and `s` acts on **wherever the cursor stands**,
    so that route has to walk the cursor there first (`CCBot._pick_model`).
  * The slider's footer is `←/→ to adjust · Enter to confirm · s for this
    session only`, it has **no digits at all**, and the level in force is the
    one the `▲` sits over — matched by *column* against the centres of the
    names under it. It does not wrap at the ends. **The set of levels is not
    fixed**: `ultracode` was on it in one session and absent in another
    (2026-09-02, the same build), which is the whole reason the list is read
    rather than declared.
  * The line under the names is a gloss that belongs to nothing in particular
    — sometimes a zone of the scale ("xhigh + workflows" under `ultracode`),
    sometimes a warning about the level the marker is on ("May use excessive
    tokens…" on `max`). It goes under the list as `Dialog.note`; attributing
    it to a level by column puts it on the wrong one.
  * Both are guarded by `_ordinary_bottom`: seeing the mode/status line
    anywhere on the pane means the bottom belongs to the ordinary UI, however
    much the text above it looks like these footers. This file quotes both of
    them, and a session working on this repository prints them.
- 🔴 **"Type something" and "Chat about this" are not the same escape hatch**
  (measured on 2.1.252). The first is an **input line inside the dialog**: the
  digit only moves the cursor onto that row, the text that follows is typed
  into it, and Enter answers the question — one turn, and the transcript reads
  `User answered … → <text>`. The second **answers nothing**: the digit alone
  rejects the question with an instruction that tells Claude to *"start by
  asking them what they would like to clarify"*, so Claude asks first and any
  text sent with the press is only read after that answer. Collecting the text
  up front and pressing "Chat about this" therefore cost a round trip and read
  as "my message was ignored" (2026-09-01: cf0ab576, the reject at 13:53:35,
  Claude's "Що саме уточнити?" at 13:53:38, the text delivered at 13:53:39).
  So the two rows take different routes: `dt:` collects the text and types it
  into the row, `dc:` presses and stops, saying that the next message goes
  there.
- 🔴 **Answering a question from its card makes that session active**
  (`CCBot._focus`). What the user types next belongs to the thread they were
  just answering, and the active session is what plain text follows. Without
  it a clarification typed after a dialog button went wherever the active
  session happened to point: on 2026-09-01 the second copy of a pay4say
  clarification (cf0ab576) landed in a different pay4say session (05caad4f) 13
  seconds later, which started working on a question never put to it. Every
  dialog route — `d:`, `dt:`, `dc:`, `dm:`, `sub:`, `nav:` — focuses, and the
  move is announced, because a silent change of target is how the next message
  goes missing.
- 🔴 **A multi-select question answers on a row that has no digit.** Its
  options are checkboxes ("1. [✔] Fix parser"), a digit *ticks* one, and the
  ticked set leaves only when the unnumbered row under the list is pressed:
  `Submit` on the last section, `Next` while sections remain (verified on the
  live TUI, 2.1.252). Three things follow. The checkbox is parsed out of the
  label into `Option.checked`, because a label that carries its own box changes
  on every press and `Watcher._tick_session` would read each press as a new
  question — hence the box lives in `state`, not in `sig`. The row is found
  before the description branch (it is indented *deeper* than the options, so
  it used to be filed away as the description of the last one) and its position
  is kept in navigation order (`Dialog.submit_index` / `cursor_index`), which
  is what `CCBot._submit_dialog` walks the cursor along, re-reading the screen
  after every move. And the review page it leads to ("Ready to submit your
  answers?") is answered by the bot: the chat has just confirmed the same
  thing, so a card repeating it would make one answer cost two taps.
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
- 🔴 **Rich messages are the normal path now** (`ccbot/rich.py`, Bot API 10.1,
  June 2026). A preformatted block **scrolls** sideways, so the old rule — "a
  phone wraps `<pre>`, therefore anything wider than 36 columns becomes a PNG" —
  is gone. Terminal output goes out as `rich.pre()`, Claude's answers as
  `markdown=` (he writes Markdown; Telegram now renders it), and a question as
  a list of blocks. Text inside a block is **never parsed**, which is why an
  option label containing `<b>` no longer needs escaping.
  Three things measured against the live API, not the docs: a single block took
  **30 000** characters (the docs claim 1024), a rich message can be **edited in
  place** and carries an inline keyboard, and box-drawing (`─ │ ┼`) is **not a
  letter wide** in Telegram's monospace face — a captured frame comes out
  ragged, plain ASCII does not, hence `rich.ascii_frame()` on everything read
  off a terminal.
- **Every rich send has a plain-text fallback and must keep it.** `rich.send`
  returns None instead of raising, and the caller then uses the old path
  (`_say_html`, `as_pre`, and the PNG for a wide diagram). An older client must
  not turn into silence. `util.split_text`, `as_pre`/`as_pre_lines` and
  `render.text_to_png` are that fallback — they are not dead code.
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
