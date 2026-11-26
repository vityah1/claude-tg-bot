# CLAUDE.md

Guidance for Claude Code working in this repository.

A Telegram dispatcher for Claude Code sessions. Every session is held by a
session host (`claude-tg-host`, one `ClaudeSDKClient` of the Claude Agent SDK
per session); the bot talks to it over a socket and turns its events into chat
messages and cards. The long "why" lives in `README.md` — don't duplicate it.

## Running and checking

- 🔴 **The bot runs as the systemd user unit `claude-tg-bot`** (`Restart=always`).
  Never start a second `main.py`: Telegram gives `getUpdates` to one poller
  only (`TelegramConflictError` on both sides).
- 🔴 **The host is a second unit, `claude-tg-host`** (`host_main.py`,
  `ccbot/host/`). Restart it only when its own code or the SDK pin changed: a
  host restart ends every turn in progress (the chat gets «🔌 … restarted» with
  «🔁 Send again»). Check `/service` for busy sessions first. Everything else
  in `ccbot/` needs only the bot restart. Log: `~/.cache/ccbot/host.log`.
- 🔴 **Restarting the bot is your job**: after a tested change, restart it and
  say so (2026-10-01). Before that, check the log tail — an `in msg` from the
  last ~10 s with no `prompt ->`, a `held batch` with no `delivered` /
  `dropped`, or a `delayed batch` with no `delivered` / `cancelled`, is still
  in memory and a restart drops it. Sessions survive a bot
  restart (the bot replays the host journal from its last ack).
- 🔴 **The bot's chat is the user's live workplace.** Live tests through it are
  allowed (he addresses his sessions by reply), but: never create a test
  session from his chat (a launch makes it active, and his next plain message
  went into it, 2026-11-24), never write the `active` table by hand (it
  overrode his choice), close a test session as soon as the check is done.
- Install: `./install.sh` (wraps `bin/setup.py`); `./install.sh --doctor`
  checks everything (claude, venv, `.env` with a live `getMe`, both units, the
  host socket, linger, the log) and changes nothing.
- Logs: `~/.cache/ccbot/bot.log` (or `journalctl --user -u claude-tg-bot`, or
  `/log` in the chat) and `~/.cache/ccbot/host.log`; `CCBOT_LOG_LEVEL=DEBUG`
  adds the `claude` stderr the host sees.
- Python is `.venv/bin/python` only (3.14, made by `uv`, **no pip** — use
  `uv pip install`). Runtime deps: aiogram 3, python-dotenv, `claude-agent-sdk`
  (pinned: an upgrade means a host restart). Dev: `requirements-dev.txt` (ruff,
  basedpyright, Babel — Babel only for translations).
- 🔴 **`bin/check.sh` before committing** (ruff → basedpyright → import check →
  locales); also the pre-commit hook (`git config core.hooksPath bin/githooks`
  after a clone). Keep it green; fix causes instead of `# noqa`.
- No test directory: checks are scripts outside the repo. Live data:
  `/service`, `~/.local/share/ccbot/host.db` (sessions, event journal),
  `~/.local/share/ccbot/state.db`, `~/.claude/projects/*/<uuid>.jsonl`.
- 🔴 **Several agents work in this repo at once.** Before committing, look at
  `git log --oneline -3` and `git status`.

## Architecture

| Part | Mechanism | Module |
|---|---|---|
| Session host | own process; one actor task per session owns its `ClaudeSDKClient`; SQLite event journal with a growing `seq` | `host_main.py`, `host/server.py`, `host/actor.py`, `host/store.py` |
| Bot ↔ host | NDJSON over `$XDG_RUNTIME_DIR/ccbot/host.sock`; hello with `last_seq` → replay; commands with ids and deadlines | `host/protocol.py`, `hostlink.py` |
| Host events → chat | buffered text, cards from `can_use_tool` (questions, permissions, plans), pulse, usage alerts, acks | `bridge.py` |
| Chat → sessions | handlers, inbox batching, routing, held text, pickers | `bot.py`, `keyboards.py` |
| Metrics | host `metrics` events, kept by the bridge, read via `status_feed.read()` (`sdk_usage` hook) | `status_feed.py` |
| From transcripts | recap («📜 Where it stopped», `/screen`), closed-session captions, version, account | `transcript.py`, `sessions.py`, `account.py` |
| Claude Code version | `claude --version` vs the session's; a 10-min check | `updates.py`, `watcher.py` |
| `/login` | `claude auth login` under a pty | `authlogin.py` |
| Language | gettext; a middleware per update + a context in background loops | `i18n.py` |

A separate host because a `claude` child dies with its parent: the bot restarts
freely. `watcher.py` holds the chat-output helpers the bridge shares (`_emit`,
`_say*`), usage alerts, `_locale` and the version loop. `state.py` — SQLite
(managed sessions, active per chat, `msg_routes`, aliases, names, history
cache, `request_cards`, `meta`); `sessions.py` — managed and closed sessions;
`paths.py` (XDG), `settings.py` (`~/.config/ccbot/config.json`), `service.py`,
`rich.py`, `media.py`, `transport.py`, `util.py`, `logsetup.py`.

## The bot's state

`state.db` is the bot's own SQLite (no ORM); the host keeps `host.db` (one
writer per file). Schema: `CREATE TABLE IF NOT EXISTS` at startup, structural
changes as `ALTER TABLE` lines inside `Store` (`_add_custom_name`,
`_add_backend`). `managed.window_id` / `offset` / `backend` remain only for
older builds (written `''` / `0` / `'sdk'`, never read). **Every running build
shares the database**: rows are read tolerantly (`_row_to_managed`), and the bot
is restarted after a schema change.

## Invariants that are easy to break

- 🔴 **Every `ClaudeAgentOptions` the host builds carries the `claude_code`
  system-prompt preset** — the SDK passes `--system-prompt ""` otherwise.
  Sessions ran without Claude Code's prompt until 3c1accb (142 vs 3352 tokens);
  the `/btw` fork (`cmd_side`) too.
- 🔴 **An event is acked only once it reached the chat**: buffered text keeps
  its `seq`, the ack is the lowest buffered seq − 1, already-delivered seqs are
  skipped on replay, and a card already sent is found in `request_cards`. A
  card is sent by one path at a time (`_Req.sending`). The host takes one
  client — never point a test client at the live socket.
- **`Managed.name` is the launch name and untouchable**: the host knows a
  session by it (`Store.by_name`); the user's name is `custom_name`.
- **session_id is not stable**: `/clear` gives a new id → `session_reset` →
  `Store.rebind`. Never cache an id outside `Store`.
- 🔴 **An old id must keep resolving**: buttons carry the id they were printed
  with. `Store.rebind` records `session_aliases`; `find_prefix` (behind
  `CCBot._resolve`) and `Store.follow` fall back to them; re-pointed on every
  clear, expire after 30 days.
- 🔴 **Register every outgoing message that belongs to a session**
  (`store.remember_message`; `Watcher._say*`, `bridge._say`, `CCBot._ack`). An
  unregistered one is blind: a reply to it goes to the active session (2026-11-24:
  the «▶️ Open» card and «🟢 Text now goes to» were blind). A list redrawn over
  a card drops its route (`forget_message`). Routes live 30 days.
- **The target of an action is `CCBot._target(m)`, not `_active()`**: a reply
  names the session and makes it active — commands included.
- 🔴 **Incoming messages are batched**: `_hold` queues them per chat,
  `_inbox_loop` waits for `_INBOX_QUIET` and every download, `_flush_inbox`
  sends one prompt sorted by `message_id`; `_batch_target` lets the first reply
  win; forwarded items keep their author (`media.build_batch_prompt`).
- 🔴 **A forwarded batch waits for a destination** (`_Parked`, `_ask_route`:
  ▶️ Current / 📋 Another / 🗑 Discard); text sent meanwhile joins it and the
  question is re-asked at the bottom. New/Recent hand over to the pickers and
  `_create` delivers it (`_deliver_after_launch`). A batch replying to a
  session is never asked about. With no active session own typing is parked
  too (`_homeless`); only a lone `/command` gets "no active session".
- 🔴 **Text never goes into a session with an open question**: `_dispatch` →
  `_try_send` checks `bridge.question_open`, then a guarded send the host
  refuses with `request_open` (`bot.Refused`). Blocked text becomes a `_Held`
  with «✏️ This answers the question» (one batch, same question part —
  `bridge.answer_text`), «💬 Close the question and send» (`bridge.dismiss`),
  «⏳ Send after the answer» (`_held_loop`, `_HELD_CLEAR`), «🗑 Don't send».
  Typed text after a card's ✏️ is never dropped: if it no longer applies it
  goes to the session as an ordinary message.
- 🔴 **Sent text can be taken back until a turn takes it in** (2026-11-25).
  The CLI takes a prompt sent mid-turn into the running turn once the current
  tool call ends, so text sent by default (not a reply) to a busy session
  waits `_DELAY` in the bot (`_Outgoing`, kept in `meta.delayed_batches` for a
  restart) with «↩️ Cancel», then goes with a uuid; until the host's
  `--replay-user-messages` echo (`prompt_taken`) the button takes it back from
  the CLI (`cancel_async_message`, a private SDK call — re-check on upgrade).
  A result closes the prompts it names (`user_message_uuids`), never "one per
  result": merged prompts used to stay "in flight" for good.
- 🔴 **A session the account limit stopped continues by itself** (2026-11-26,
  no cancel button — he is asleep): the host hands `limit_resets_at` with
  `turn_end`, the bridge keeps `meta.limit_resume` and sends «Continue.»
  `_RESUME_SLACK` (60 s) after it; any send or `turn_start` drops the entry,
  `fired` stops a loop on the same limit. `credits_required` is not a wait
  but «🧠 Change model» (`_model_refused`).
- 🔴 **«💬 Discuss» answers nothing**: it denies with "ask what to clarify",
  so the next message is the reply to that.
- 🔴 **Answering a card makes its session active**, announced (`_focus`,
  `_say_focused`) — every card route.
- **A settled choice leaves a record**: a picker becomes one line saying what
  was picked (`_settle`, only on success); a card trades its buttons for an
  inert `done_kb` label; a question closed otherwise gets «⏹ Question closed».
- 🔴 **«🔔 Show the question» is posted by the bridge tick, only on a tap**
  (`bridge.request_repost`; the old card gets «⬇️ Shown again below»). Picking
  a session from the list never posts it by itself (2026-11-10).
- 🔴 **A row of /sessions makes its session active at once** (`sp:` →
  `_activate`, the line saying so posted at the bottom): the old «▶️ Open» step
  drew its button on the list far up the chat and the next text went to the
  previous session (2026-11-25). `s:` is the pickers' «⬅️ Back»: card only.
- 🔴 **A launch ends on the session's own card** (`_create` → `_launch_card`:
  the picked name, «🟢 Text now goes to …», the previous active session named),
  card and settled picker both registered (2026-10-03).
- **A new `/command` means four places**: `_register()`, `OWN_COMMANDS`,
  `CCBot.MENU`, `help_text()` (missing from `OWN_COMMANDS` it travels to Claude
  as text). 🔴 A command acting on a session also gets a session-card button
  (2026-10-29: "do you think I have to type it by hand?").
- **`callback_data` ≤ 64 bytes**: ids cut to 8 characters (`sid8`, `rq:<rid8>`)
  and resolved by prefix.
- 🔴 **Closed sessions**: `sessions.closed_all()` drops a transcript live in
  `claude agents --json`, present in `Store.all_managed()` or hidden (a `/btw`
  fork). Per exact `cwd`, paged (`HISTORY_PAGE`); a card resolves by
  `closed_view()` (glob on the file name); the directory in `callback_data` is
  a hash (`dir_key`), never a list position.
- 🔴 **A user-given name outlives the session**: `/rename` writes
  `custom_name` and `session_names` (untouched by `Store.remove()`), `rebind`
  moves it, `Store.add()` reads it back.
- 🔴 **Caption sources, in order** (`sessions._closed_view`): saved name,
  `claude -n` if a human wrote it (`hand_written`), `ai-title`, first prompt.
  A new session is titled from its first task (`bridge._title_from`).
- 🔴 **A prompt is cleaned before it becomes a caption** (`clean_prompt` on
  text with its line breaks — `transcript.prompt_text`); the bot's own wrapping
  is matched structurally, never by wording (it is translated). Skill text
  (`isMeta`) and task notifications are never a prompt.
- 🔴 **A non-empty `thinking` block is a message to the user** —
  `transcript.parse_line` reads it as text (2026-10-09).
- 🔴 **A session's account comes from its own transcript**, never
  `~/.claude.json` (`account.py`: newest of `session_context` and a lone
  `credential_org` resolved via `~/.cache/ccbot/account-orgs.json`; ambiguous →
  nothing; records older than the process start are dropped).
- 🔴 **Attachments are never filtered by format** (`_attachment()`; only
  Telegram's 20 MB limit); the handler filter lists every kind; files keep
  their name (`media.safe_name`); `media.is_image()` picks "image"/"file".
- 🔴 **No `text` may still be words**: Premium's `rich_message` →
  `rich.read()`. The final unfiltered `@dp.message()` answers unhandled kinds
  (`_SERVICE_KINDS` only logged) — keep it last.
- 🔴 **One unparseable update must not stop polling**:
  `transport.TolerantSession` validates update by update, cuts out what the
  models reject, logs it, tells the chat (`_say_damaged`) (2026-10-27).
- 🔴 **Only a connect is retried** (`make_request`: a `ClientConnectorError`
  before a byte left, 0.5/1.5/3 s; `getUpdates`/`getFile`/`getMe` also after a
  reset) — never a timeout (a repeated `sendMessage` duplicates). "➡️ Sent to"
  never raises (`_ack_later`); `_ack` does (it carries refusals).
- 🔴 **Rich messages are the normal path** (`rich.py`; Claude's text as
  `markdown=`, cards as blocks, `rich.ascii_frame()` for terminal-ish text);
  every rich send keeps its plain-text fallback (`rich.send` → None).
- **`c.message` is not guaranteed** (older than 48 h): use `_editable()`.
- 🔴 **No error goes silent**: handlers → `@dp.errors`; bridge/watcher loops →
  `_report_failure` (once an hour per breakage); a host down past
  `_DOWN_GRACE` is announced, and its return. New loops report the same way.
- 🔴 **`bin/setup.py` runs on the system Python before the venv exists**:
  stdlib only, no `ccbot/` imports, its own `.env` loop. Install steps live in
  `bin/setup.py` and README "Manual install" — change both.
- **The bot's files go through `paths.py`** (XDG); `.env` is the only file a
  human edits.
- 🔴 **A chat text reads at a glance**: its point (which session, what
  happened) bold on a line of its own, an empty line, then the details — never
  one run-on sentence. Check it as the real message on a phone before shipping
  (2026-11-25, «🟢 Text now goes to …» ran together: `_switch_note`).
- 🔴 **UI strings only through gettext** (`_()`, `ngettext()` with a number;
  then `pybabel extract` → `update` → translate → `compile`, README "Adding a
  language"). Comments, docstrings and logs: English, no `_()`. No `_()` at
  module level (use a function or `N_()` in lookup tables). `_` is never a
  throwaway variable (tuple unpacking included). Placeholders named, emoji
  inside, format specs outside. Background loops open
  `use_locale(watcher._locale())` on every tick.
