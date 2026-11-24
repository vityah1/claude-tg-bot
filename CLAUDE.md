# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

A Telegram dispatcher for Claude Code sessions. Every session is held by a
session host (`claude-tg-host`, one `ClaudeSDKClient` of the Claude Agent SDK
per session); the bot talks to it over a socket and turns its events into chat
messages and cards. The detailed "why it works this way" lives in `README.md`
(long and current; no need to duplicate it here).

## Running and checking

- 🔴 **The bot already runs as the systemd user unit `claude-tg-bot`**
  (`Restart=always`). **Do not start a second `main.py`** — Telegram gives
  `getUpdates` to one poller only, and both sides start failing with
  `TelegramConflictError`. Check with `systemctl --user is-active claude-tg-bot`.
- 🔴 **The session host is a second unit, `claude-tg-host`** (`host_main.py`,
  `ccbot/host/`): it holds every session. Restart it only when its own code
  (`ccbot/host/*`, the SDK pin) changed — a host restart ends every turn in
  progress (the chat gets «🔌 The session host was restarted» with «🔁 Send
  again» per cut-off prompt). Before one, ask it what is busy (`/service`).
  Code in `bridge.py` / `hostlink.py` / `bot.py` and the rest of `ccbot/`
  needs only the bot restart. Its log: `~/.cache/ccbot/host.log`.
- 🔴 **The bot's chat is the user's live workplace.** Live tests through it
  (Telegram Web) are allowed since 2026-11-24: he addresses his sessions by
  reply while they run. But a test reply makes the test session active (his
  plain message landed there that day), so: never write the `active` table by
  hand — it overrode his own choice — and close a test session as soon as the
  check is done. 🔴 Never create a test session from his chat: a launch makes
  it the active one, and his next plain message (he had just opened another
  session) went into it, twice on 2026-11-24. (2026-11-24, SDK migration)
- There is no hot reload. After changing code:
  `systemctl --user restart claude-tg-bot`. Sessions survive it (they live in
  the host; the bot replays the host journal from its last ack). `/restart` in
  the chat does the same thing.
- 🔴 **Restarting is your job, not the user's.** Once the change is tested,
  restart the bot yourself and report it done — never end with "the bot still
  runs the old code, it needs a restart" (2026-10-01, the user had to say so).
  Before restarting, check the log tail: an `in msg` from the last ~10 s with
  no `prompt ->` after it is still waiting in the in-memory inbox, and the
  restart drops it (a photo with a question was lost that way the same day).
  The same goes for a `held batch` (text kept out of a session with an open
  question) that has no `delivered` / `dropped` line yet.
- Installing on a clean machine is `./install.sh` (a wrapper around
  `bin/setup.py`). `./install.sh --doctor` changes nothing and checks the whole
  install: claude, venv, `.env` (with a live `getMe`), both units, the host
  socket, linger, the log. Faster than reading the log by hand.
- Log: `tail -n 100 ~/.cache/ccbot/bot.log` or
  `journalctl --user -u claude-tg-bot -n 100 --no-pager`; in the chat, `/log`.
  The host: `~/.cache/ccbot/host.log`. `CCBOT_LOG_LEVEL=DEBUG` in `.env` adds
  the `claude` stderr the host sees.
- Python is `.venv/bin/python` only (3.14, the venv was made by `uv`; **it has
  no pip** — install packages with `uv pip install`). Dependencies: aiogram 3,
  python-dotenv, `claude-agent-sdk` (pinned: an upgrade means a host restart);
  dev tools in `requirements-dev.txt` (ruff, basedpyright, Babel). Babel is
  needed ONLY for working on translations; the bot reads the compiled `.mo`.
- 🔴 **Run `bin/check.sh` before committing** (ruff → basedpyright → import
  check → locales). The same script is installed as a pre-commit hook; after a
  clone, enable it once with `git config core.hooksPath bin/githooks`.
  Configuration lives in `pyproject.toml`. The gate has to stay green: do not
  silence a rule with `# noqa` when the cause can be fixed instead. The
  emergency bypass is `git commit -n`.
- There is no test directory in the repo; checks are scripts in the
  scratchpad. Live data close at hand: `/service` (host status),
  `~/.local/share/ccbot/host.db` (the host's sessions and event journal),
  `~/.local/share/ccbot/state.db`, `~/.claude/projects/*/<uuid>.jsonl`,
  `claude agents --json`.
- 🔴 **Several agents often work in this repo at once** (the bot's sessions,
  including ones in this very directory). Before committing, look at
  `git log --oneline -3` and `git status`: a neighbouring session may already
  have committed the changes you just made to the tree.

## Architecture

| Part | Mechanism | Module |
|---|---|---|
| The session host | a process of its own; one actor task per session owns its `ClaudeSDKClient`; SQLite journal of events with a growing `seq` | `host_main.py`, `host/server.py`, `host/actor.py`, `host/store.py` |
| Bot ↔ host | NDJSON over a Unix socket (`$XDG_RUNTIME_DIR/ccbot/host.sock`); hello with `last_seq` → replay; commands with ids and deadlines | `host/protocol.py`, `hostlink.py` |
| Host events → chat | text buffered and flushed, cards from `can_use_tool` (questions, permissions, plans), pulse, usage alerts, acks | `bridge.py` |
| Chat → sessions | handlers, inbox batching, routing, held text, pickers | `bot.py`, `keyboards.py` |
| Metrics (context, limits, model, cost) | host `metrics` events, kept by the bridge, read through `status_feed.read()` (the `sdk_usage` hook) | `status_feed.py` |
| What outlives a process | recap («📜 Where it stopped», `/screen`), closed-session captions, version, account — read from `~/.claude/projects/*/<uuid>.jsonl` | `transcript.py`, `sessions.py`, `account.py` |
| Claude Code on disk vs. in a session | `claude --version` / the session's reported version; a 10-min check in the watcher loop | `updates.py`, `watcher.py` |
| `/login` | `claude auth login` under a pty; the link to the chat, the code back | `authlogin.py` |
| Interface language (gettext, `locales/`) | a middleware on each update + a context in the bridge/watcher loops | `i18n.py` |

Why a separate host: a `claude` child dies with the process that started it,
so the bot restarts freely while the host restarts only for its own code.

`watcher.py` is slim: the chat-output helpers the bridge shares (`_emit`,
`_say`, `_say_rich`, `_say_html`), usage alerts (`_check_usage`), `_locale`,
and a loop that announces a newer Claude Code on disk (resting sessions
excluded). The rest: `state.py` — SQLite (managed sessions, the active one per
chat, `msg_routes`, aliases, names, the history cache, `request_cards`,
`meta`); `sessions.py` — two kinds, managed and closed (`live_agents` keeps
running sessions out of the closed list); `paths.py` — XDG paths;
`settings.py` — `~/.config/ccbot/config.json`; `service.py` — the bot's own
process; `rich.py` (Bot API 10.x rich messages); `media.py`; `transport.py`
(the polling session that survives an unparseable update); `util.py`;
`logsetup.py`.

## The bot's state

`~/.local/share/ccbot/state.db` is SQLite the bot maintains itself (no ORM and
no Alembic); the host keeps its own `host.db` (one writer per file). The
schema is created at startup with `CREATE TABLE IF NOT EXISTS`, and structural
changes are hand-written `ALTER TABLE` statements inside `Store`
(`_add_custom_name`, `_add_backend`). A "migration" here is a line of code that
runs the first time a new version starts. `managed` keeps the columns
`window_id` / `offset` / `backend` only for older builds: they are written as
`''` / `0` / `'sdk'` and never read.

**The database is shared by every build that is running.** New code that adds
a column changes it for the older process that is still alive, too. That is why
rows are read tolerantly (`_row_to_managed`), and why the bot has to be
restarted after a schema change.

## Invariants that are easy to break

- 🔴 **Every `ClaudeAgentOptions` the host builds carries
  `system_prompt={"type": "preset", "preset": "claude_code"}`.** The SDK passes
  `--system-prompt ""` when `system_prompt` is unset, so a session runs without
  Claude Code's system prompt — they did from 2026-11-24 until commit 3c1accb
  (142 vs 3352 tokens). That includes the `/btw` fork (`cmd_side`).
- 🔴 **An SDK event is acknowledged only once it has reached the chat.** Text
  waits in the bridge's buffer with its `seq`; the ack is the lowest buffered
  seq minus one, so a bot restart replays exactly what had not gone out. A card
  already sent for a request is found in `request_cards` and not sent again.
  The host takes one client: a second connection replaces the first, so never
  point a test client at the live socket.
- **`Managed.name` is the launch name (`claude -n`) and it is untouchable.**
  The host knows a session by it (`Store.by_name`), never by an id. The
  user-facing name lives in `custom_name`.
- **session_id is not stable.** `/clear` hands out a new id on the same
  process: the host emits `session_reset` → `bridge._session_reset` →
  `Store.rebind`. Never cache an id outside `Store`.
- 🔴 **A button carries the id it was printed with, so an id a session has
  left behind has to keep resolving.** `Store.rebind` records the move in
  `session_aliases`; `Store.find_prefix` (behind `CCBot._resolve`, which every
  callback goes through) falls back to it, and `Store.follow` does the same
  for ids written down outside the row (a pending rename, held text). Aliases
  are re-pointed on a second clear, so the *first* card still works, and
  expire after 30 days.
- **Register every outgoing message with `store.remember_message()`**
  (`Watcher._say*`, `bridge._say`, `CCBot._ack`). That is what makes reply
  addressable; a message that was not recorded is "blind", and a reply to it
  goes to the active session rather than the one on the screen.
- **Take the target of an action from `CCBot._target(m)`, not `_active()`** — it
  accounts for the reply and makes that session active. This applies to
  commands as well: a `/clear` sent as a reply clears *that* session.
- 🔴 **Incoming messages are batched, not forwarded one by one.** A forwarded
  conversation arrives as a message per bubble in a single poll, and aiogram
  runs an update per task — order is not guaranteed. `CCBot._hold` queues them
  per chat, `_inbox_loop` waits for `_INBOX_QUIET` of quiet (and for every
  attachment download), and `_flush_inbox` sends **one** prompt sorted by
  `message_id`, with the target resolved then (`_batch_target`: the first
  reply naming a session wins). A forwarded item keeps its author from
  `forward_origin` (`media.build_batch_prompt`).
- 🔴 **A forwarded batch is not sent until the chat says where it goes.**
  `_flush_inbox` parks it (`_Parked`) and `_ask_route` asks — «▶️ Current»,
  «📋 Another session» (➕ a new one, 🕘 a closed one), «🗑 Discard».
  Anything sent while the question is open **joins that batch** and the
  question is re-asked at the bottom. «New» and «Recent» only set
  `_Parked.await_new` and hand over to the ordinary pickers; `_create`
  delivers the batch itself (`_deliver_after_launch`) — the host answers
  `create` once the session is up, so there is one delivery path. A batch that
  replies to a session's message is **never** asked about. Own typing goes
  straight to the active session — **unless there is none**: then it is
  parked too (`CCBot._homeless`), ➕ first, no «Current»; only a lone Claude
  `/command` gets "no active session".
- 🔴 **Text is never sent into a session with an open question.**
  `_dispatch` → `_try_send` is the only road user text takes:
  `_question_open` (= `bridge.question_open`) first, then
  `_send_prompt(guard=True)`; the host refuses a guarded send with
  `request_open`, raised as `bot.Refused`. Blocked text becomes a `_Held`
  (one per session, a list of batches) with a card: «✏️ This answers the
  question» (only for one batch and the same question part the card was drawn
  for — `bridge.answer_text`, shared with the card's ✏️), «💬 Close the
  question and send» (declines the open request via `bridge.dismiss`), «⏳ Send
  after the answer» (`_held_loop`, after `_HELD_CLEAR` with no question) and
  «🗑 Don't send». Text sent meanwhile joins the hold and the card is asked
  again at the bottom. Command buttons (`/clear`, `/compact`, `/context`) go
  through `_send_command` and refuse the same way.
- 🔴 **«💬 Discuss» answers nothing**: it denies the question with an
  instruction to ask what to clarify first, so the next message is read as the
  reply to that.
- 🔴 **Answering a question from its card makes that session active**
  (`CCBot._focus`, announced by `_say_focused`): what the user types next
  belongs to that thread (2026-09-01, a clarification landed in another
  session). Every card route focuses.
- **A choice that has come true leaves a record of itself, not the choice.**
  A picker is edited into one line saying what was picked (`CCBot._settle`,
  only on success — a failed launch keeps the list). A question card trades
  its buttons for one inert `done` button naming the answer (`done_kb`); a
  question closed any other way gets «⏹ Question closed».
- 🔴 **«🔔 Show the question» is posted by the bridge tick, not the handler,
  and only on a tap.** The button (shown while a card stands) calls
  `bridge.request_repost`; the next tick posts the card at the bottom and
  trades the old one's buttons for «⬇️ Shown again below». Picking a session
  from the list must not post the question by itself (2026-11-10).
- 🔴 **A launch ends on the session's own card, never on a bare line.** Every
  way into a session — ➕ new, ▶️ resume from 🕘 history or a search — goes
  through `_create` → `_launch_card`: the card a row of `/sessions` opens, the
  name the session was picked by (`title=`, set before the card is built),
  «🟢 Text now goes to …» and the previously active session named as still
  working. Both the card and the settled picker line are registered to the new
  session (2026-10-03, a bare «✅ Resumed» sent two messages astray).
- **A new bot `/command` means four places**: the handler in `_register()`, the
  `OWN_COMMANDS` set, the `CCBot.MENU` list, and the text of `help_text()`.
  Forget `OWN_COMMANDS` and the command silently travels to Claude as text.
  🔴 **A command that acts on a session also gets a button on the session
  card** (`session_kb` → `_session_action`): the user drives the bot from the
  phone by tapping (2026-10-29: "do you think I have to type it by hand?").
- **`callback_data` is limited to 64 bytes**: the session id is cut to 8
  characters (`sid8`), a request id likewise (`rq:<rid8>:…`), and resolved
  back by prefix.
- 🔴 **A closed session is one that nothing owns, and history is per
  directory.** `sessions.closed_all()` drops a transcript whose id is live in
  `claude agents --json`, present in `Store.all_managed()`, or hidden (a
  `/btw` fork). The list is filtered by exact `cwd` and paged
  (`HISTORY_PAGE`); a card is resolved by `sessions.closed_view()`, a direct
  glob on the file name, and the directory in `callback_data` is a hash of the
  path (`sessions.dir_key`), never its position in the list.
- 🔴 **A name the user gave outlives the session that had it.** `/rename`
  writes both `managed.custom_name` and `session_names`, which `Store.remove()`
  does *not* touch; `rebind` moves it to the new id, and `Store.add()` reads it
  back, so a resumed session comes up under its own name.
- 🔴 **A row's caption has four sources, in this order**
  (`sessions._closed_view`): the saved name, then `claude -n` **when a human
  wrote it** (`hand_written` rejects the bot's own `folder-1a2b`), then
  `ai-title`, then the first prompt — the usual case, since Claude Code all but
  stopped writing `ai-title` in late August 2026.
- 🔴 **A prompt is cleaned before it becomes a caption**
  (`sessions.clean_prompt`, on text that still has its line breaks — hence
  `transcript.prompt_text`). The bot's own wrapping (attachment heading and
  media paths, "Forwarded from Telegram, 8 messages") is matched
  **structurally**, never by its wording — it is translated.
- 🔴 **A non-empty `thinking` block is a message to the user, not reasoning**
  (the reasoning itself is stored redacted): `transcript.parse_line` reads it
  as text, and the recap must too (2026-10-09, a lost paragraph).
- 🔴 **A session's account is read from its own transcript, never from
  `~/.claude.json`** (that names the last login; a session moves to it only
  with its next request). `ccbot/account.py` takes the newest of
  `session_context` (`userEmail`) and `credential_org` (an org id written
  **alone** after a login elsewhere), resolved through known org→address pairs
  (`~/.cache/ccbot/account-orgs.json`); unresolved or shared by several
  addresses, it shows nothing rather than the stale address. A record older
  than the process `startedAt` is dropped.
- 🔴 **An attachment is never filtered by format.** `_attachment()` takes every
  kind Telegram has and hands the path over whatever the MIME type says (a
  whitelist once answered a forwarded JSON with a refusal). The only limit is
  Telegram's 20 MB bot download. The handler filter has to list every kind too
  — an unlisted one vanishes from the batch in silence. A file keeps the name
  it was sent with (`media.safe_name`, `-1` on a collision); `media.is_image()`
  decides whether the prompt says "image" or "file".
- 🔴 **A message with no `text` may still be words: Premium sends formatted
  text as `rich_message`.** `rich.read()` turns the blocks into Markdown plus
  the files they carry (`_Item.paths`). The last handler, `@dp.message()` with
  no filter, answers any kind still unhandled (service ones in
  `_SERVICE_KINDS` are only logged) — keep it last and keep it.
- 🔴 **One update aiogram cannot parse must not stop polling.** aiogram
  validates a `getUpdates` answer as a whole, so one unknown kind froze the bot
  for 15 minutes (2026-10-27). `transport.TolerantSession` validates such a
  batch update by update, cuts out what the models reject, logs the raw JSON
  and tells the chat what was left out (`CCBot._say_damaged`). Still bump
  aiogram when Telegram adds a block kind.
- 🔴 **A connect that drops is retried in the transport, and only a connect.**
  `TolerantSession.make_request` repeats a request whose `ClientConnectorError`
  came before a byte left (0.5 / 1.5 / 3 s), never a timeout or a hang-up
  mid-answer (a repeated `sendMessage` is a duplicate); only `getUpdates` /
  `getFile` / `getMe` are repeated after a reset too. Downloads repeat on their
  own until the first byte. The "➡️ Sent to" after a delivered prompt
  (`_send_batch`) never raises — a failed one is re-sent later (`_ack_later`);
  `_ack` itself still raises, it also carries refusals.
- 🔴 **Rich messages are the normal path** (`ccbot/rich.py`, Bot API 10.1):
  terminal-ish output as `rich.pre()`, Claude's answers as `markdown=`, cards
  as blocks; text inside a block is never parsed. Measured on the live API: a
  block took 30 000 characters, a rich message can be edited in place and
  carries an inline keyboard, and box-drawing is not a letter wide in
  Telegram's monospace face (`rich.ascii_frame()`).
- **Every rich send has a plain-text fallback and must keep it.** `rich.send`
  returns None instead of raising, and the caller then uses the old path
  (`_say_html`, `util.split_text`, `as_pre`). An older client must not turn
  into silence.
- **`c.message` is not guaranteed.** For a card older than 48 hours Telegram
  serves `InaccessibleMessage` (or `None`). Take the message through
  `_editable()` only, never `c.message.…`.
- **Read the database schema tolerantly.** `Store._row_to_managed` discards
  unknown columns (2026-08-24: a newer build's column killed an older bot).
- 🔴 **No error goes silent.** An exception in a handler is caught by
  `@dp.errors` (`bot.py`) and answered in the chat; a bridge tick or watcher
  loop failure is reported (`_report_failure`, once an hour for the same
  breakage), a host that stays down past `_DOWN_GRACE` is announced, and so is
  its return. A new background loop or task must report the same way.
- 🔴 **`bin/setup.py` runs on the system Python, BEFORE the venv exists.** So it
  contains stdlib only: no `aiogram`, no `dotenv`, no imports from `ccbot/`
  (it mirrors `paths.py` and `protocol.socket_path` instead) and parses `.env`
  with a loop of its own.
- **The install steps live in two places deliberately**: `bin/setup.py` and the
  "Manual install" section of the README. Change one and look at the other.
- **The bot's own files go through `paths.py` only** (XDG: config/data/cache).
  It writes nothing next to the code; `.env` is the single configuration file a
  human edits.
- 🔴 **UI strings go through gettext only (`ccbot/i18n.py`).** The base language
  of the code is English; Ukrainian lives in `locales/uk`. New text for the chat
  means `_()` (or `ngettext()` when it contains a number), then
  `pybabel extract` → `update` → translate → `compile` (README, "Adding a
  language"). Comments, docstrings and **logs** are in English and WITHOUT
  `_()`.
- **`_()` at module level is not allowed** — the constant is evaluated at import
  time, and the first language sticks forever. Make it a function
  (`help_text()`, `restart_ask()`), or use `N_()` at the definition of a table
  looked up by key (`MENU`, `_STATUS_LABELS`) and `_()` where it is read.
- 🔴 **`_` can no longer be used as a throwaway variable** (`for _ in
  range(...)`, `_, a, b = ...`): it shadows the gettext import and the function
  dies with `UnboundLocalError` on the first `_("…")`. Ruff catches only the
  loop form, never tuple unpacking.
- **Placeholders are named** (`{name}`, not `{}`), emoji go inside the string,
  and format specifications (`{n:02d}`) stay outside it. HTML travels with the
  string as a whole.
- **Background loops open a locale context of their own.** A ContextVar is
  copied when the task is created, so a loop would never see a language chosen
  later — the bridge's `_loop` and the watcher's `_loop` open
  `use_locale(watcher._locale())` on every tick. Any new background task that
  writes to the chat has to do the same.
