# claude-tg-bot

A Telegram dispatcher for Claude Code sessions, running on Linux or WSL.

From your phone it lets you see the list of sessions, start new ones in any
directory, bring closed ones back, send prompts and `/`-commands, and — above
all — **answer Claude's interactive questions** with buttons.

## Quick start

You need Linux or WSL, [Claude Code](https://claude.com/product/claude-code)
installed and signed in, and `tmux`. The installer handles everything else.

```bash
git clone <url> claude-tg-bot && cd claude-tg-bot
./install.sh
```

It checks the prerequisites, builds the virtualenv, and asks for the bot token
from [@BotFather](https://t.me/BotFather) — validating it on the spot, so a
mistyped token is caught in two seconds rather than five minutes later in the
log. Then, instead of sending you off to look up your numeric Telegram id, it
asks you to message the bot and reads the id off that message: one action that
also proves you can reach the bot. Finally it wires the status-line hook,
installs the systemd user unit, starts the bot, and confirms the whole chain
works by sending you a message in Telegram.

Run it again whenever you like — it is also the update path
(`git pull && ./install.sh`), and it never overwrites a working `.env` without
asking. `./install.sh --doctor` inspects an existing install and changes
nothing.

**Windows** is not supported natively and cannot be: the bot drives the Claude
Code TUI through tmux. Install WSL (`wsl --install` in PowerShell) and run
everything inside it. **macOS** runs the bot fine, but has no autostart
template yet — install by hand, as below.

## Why it works this way

The built-in channels (`--remote-control`, `--channels`) are blocked by a
corporate org policy here. So the bot drives sessions from the outside:

| Direction | Mechanism |
|---|---|
| In (prompts, option choices, `/`-commands) | `tmux paste-buffer` / `send-keys` |
| Out (the text of the answers) | `~/.claude/projects/*/<uuid>.jsonl` |
| Blocking dialogs | `tmux capture-pane` + `ccbot/screen.py` |
| The list of live sessions | `claude agents --json` |

Answer text comes from the transcript rather than the screen: it is clean, free
of ANSI, unwrapped and not limited by the scrollback. The screen is needed only
for dialogs.

## Three kinds of session

* **Managed** — created by the bot inside tmux. Full control.
* **Foreign** — started by you in an ordinary terminal. View only: their stdin
  is out of reach from outside, and hijacking them would mean ruining work that
  is already under way.
* **Closed** — historical transcripts; brought back with `--resume`.

## Manual install

`install.sh` is only the automation of the steps below, never the only place
they are written down. If you would rather not run a script, this is the same
install by hand.

**1. Prerequisites.** `tmux`, plus Claude Code installed and signed in —
`claude agents --json` has to answer, since being on `PATH` is not the same as
being usable. `jq` matters only for step 4.

**2. Environment.** Python 3.12 or newer:

```bash
uv venv && uv pip install -r requirements.txt
# no uv? python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

**3. Credentials.**

```bash
cp .env.example .env && chmod 600 .env
```

`TG_BOT_TOKEN` comes from [@BotFather](https://t.me/BotFather) (`/newbot`).
`TG_ALLOWED_USER_IDS` is your numeric Telegram id — [@userinfobot](https://t.me/userinfobot)
tells you yours. It has no default on purpose: an empty list would mean a bot
that runs shell sessions for anyone who finds it, so the bot refuses to start
without one.

**4. Exact metrics** (optional). Point `statusLine.command` in
`~/.claude/settings.json` at `bin/statusline-tee.sh`; if you already have a
status line, keep it by naming it in `CCBOT_REAL_STATUSLINE`. Skipping this
costs precision, not function — the bot falls back to reading the screen. See
[Exact metrics](#exact-metrics).

**5. Run it.** `.venv/bin/python main.py`, or install the systemd user unit —
see [Autostart](#autostart).

**6. Check.** `./install.sh --doctor` verifies all of the above and tails the
log, whichever way you installed.

## Model, effort, permission mode

The session card shows the current values (read off the TUI status line) and
offers buttons to change them:

* **🧠 Model** and **◉ Effort** — through `/model <alias>` and
  `/effort <level>`. Claude Code also keeps them as the default for new
  sessions, which the UI warns about.
* **🔐 Mode** — there is no slash command for it, so the bot presses Shift+Tab
  until the status line shows the mode you asked for. The cycle is
  `auto → manual → accept edits → plan`. `bypassPermissions` and `dontAsk` can
  only be set when the process starts.

## Lists

The message text does not restate what the buttons already say — by the time
you have scrolled a long list, the summary is forgotten. So the top carries
counters only, and all the information lives on the buttons themselves:

```
🕘 Recent sessions — newest first.
   [09:33 · finman · Widget rework of the mileage…]
   [08:32 · 7loc-admin-web · Paddle billing agree…]
   [Sun 21:25 · finman · The last deploy]
```

The button format is `time · directory · what the session is about`. The
directory is shown by its base name: a full path does not fit on a button, and
`finman` or `pay4say` is enough to get your bearings. The name comes from the
session's AI title, or from the first prompt when there is none, because a bare
UUID says nothing at all.

## Session names

The bot calls a managed session `directory-1a2b` — unique, but no help in
telling two sessions of the same project apart. So the name in the list and in
message captions is taken from three sources in turn:

1. **your own name** — `/rename Billing audit`, or ✏️ on the session card,
   or just `/rename` picked out of the "/" menu: it asks which name, and takes
   your next message as the answer (with a ⬅️ Cancel button, and it lapses
   after three minutes rather than swallowing a message meant for Claude);
2. **the title Claude came up with** (`ai-title` in the transcript) — managed
   sessions rarely get one: the bot starts `claude -n <name>`, and Claude does
   not invent a title for a session that already has one;
3. **the session's first prompt** — the watcher reads it out of the transcript
   itself.

In messages the name comes **with the directory in front** — `pay4say · pricing` —
because several projects write into one chat at the same time and the name
alone does not say where the work is happening. If the name already starts with
the directory, the prefix is not repeated. In list buttons the directory is a
column of its own, so the name there goes without it.

Renaming changes **only** what you see: alongside it the database keeps `name`,
the name `claude -n` was started with. That one never changes, and it is how the
session is found again after `/clear` (see below).

The first prompt sits behind a file-history preamble that easily grows past
250 KB, so it is looked for in the first megabyte of the file rather than the
first 128 KB — otherwise a session would end up with no name at all (and a
closed one would also drop out of the 🕘 list).

## `/clear` changes the session id — and the bot follows

The most treacherous breakage that had to be caught here: after `/clear`,
Claude does **not** restart — same process, same window, same `-n` name — but
it starts **a new transcript under a new session id**. Nobody announces this:
the old file simply stops growing. A bot reading the old id then goes silent
forever — prompts still arrive (they go through tmux, not through the id),
Claude still answers them, and the answers are written where nobody is reading.

The thread back is the launch name: `claude agents --json` shows it next to the
new id. So on every tick the watcher reconciles managed sessions against live
agents, and when a managed session's id is missing from the live ones while an
agent with the same name and directory is present, it rebinds the session to
the new id: same window, same name, same reply routes, offset back to zero. A
line arrives in the chat: "🧹 *name*: context cleared — the session was given a
new id, still watching it."

That is exactly why renaming touches `custom_name` only: had it changed `name`,
the thread would snap and the session would be lost after the very first
`/clear`.

## While Claude is thinking

Claude writes the transcript in bursts, so a long turn with reasoning leaves
nothing in it for minutes — and in a chat that is indistinguishable from a
hung bot. The terminal, meanwhile, counts out loud:
`✢ Fluttering… (5m 14s · ↓ 13.7k tokens)`.

So after 75 seconds of uninterrupted work the bot sends **one** line,
`⏳ <name>`, carrying that counter, and then **edits it in place** every 45
seconds. When the real answer arrives, the pulse is deleted so it leaves no
litter. The "esc to interrupt" hint is cut out of the line: there is no way to
press it from a phone.

It is specifically the line with a spinner and a timer in brackets that is
picked up — "✻ Worked for 8m 9s" without brackets means the turn is already
over, and no pulse is due.

## Running under systemd: PATH

A user unit does **not** read your shell profile, so `nvm` is not on its PATH —
and `claude` simply does not exist as far as the bot is concerned. The
consequence was insidious: lists of live sessions silently came back empty,
with no error anywhere.

So the bot resolves the CLI itself: `CCBOT_CLAUDE_BIN` → `PATH` →
`~/.nvm/versions/node/*/bin/claude` → `~/.local/bin` → `/usr/local/bin`. The
path it found is written to the log at startup, and if nothing is found the bot
sends a warning to the chat rather than staying quiet. The unit also spells out
PATH explicitly.

## The log

Written to `~/.cache/ccbot/bot.log` (rotating, 2 MB × 3) and to journald in
parallel. `/log` shows the last lines right in the chat.

It is recorded systematically rather than in selected spots: an outer
middleware catches **every** update before any handler — including the ones
that are dropped afterwards (a sender who is not allowed, no handler matched).
Those are precisely the cases that are hardest to reconstruct after the fact.
On top of that: session lifecycle, prompts sent, watcher decisions (a dialog, a
flush, a threshold, a session gone) and rejected users.

`CCBOT_LOG_LEVEL=DEBUG` adds every tmux command as well.

## No error goes silent

The worst state to be in is a bot that is alive while every action quietly
fails: in a chat that is indistinguishable from "Claude is just thinking". So
an exception never simply stays in the log:

* **an exception in a handler** is caught by `@dp.errors`, which answers in the
  same chat — what broke (type and message) and that sessions are unaffected;
* **an exception in a watcher tick** stops delivery of answers from every
  session, so it gets a warning of its own. The tick repeats every second and a
  half, so the same breakage reports **once an hour**, and when the loop
  recovers, "✅ the watcher is working again" arrives.

This is what the failure that prompted it looked like: a running process had an
old version of the code while the database had already gained a new column,
every tick died with a `TypeError`, the unit stayed `active`, and the bot said
nothing until somebody restarted it.

## The active session, and working in parallel

Sessions live independently: starting a task in `finman` and then opening
`7loc` does not stop the first one — it keeps working, and its answers arrive
captioned with its own name. The watcher follows every managed session at once.

The **active** one is whichever you last created, resumed, or opened with the
"▶️ Open" button. Ordinary text goes there, and the list marks it `▶️`.

Buttons are always addressed — the session id is baked into them, so an answer
to Claude's question lands exactly where the question came from, whichever
session happens to be active.

For everything else there is **reply**: replying to any message from a session
goes to that session — both text and commands (`/clear`, `/esc`, `/usage`,
`/screen`, `/exit`, `/rename`). It matters when two tasks are running side by
side: otherwise it is easy to wipe the context of the wrong session while
reading a message from another. A reply also **makes that session active**:
without that, the next message sent without a reply would go back to the
previous one, and "where I just answered" would drift apart from "where the bot
is writing" all over again. The confirmation shows it —
`➡️ Sent to <name> · in reply, now active`.

The bot's own confirmations are routed too: a reply to `➡️ Sent to …` lands in
that same session, so you can answer the last message in the chat without
hunting for the previous one.

## Commands

Registered through `setMyCommands`, so they show up in the "/" menu — nothing
has to be typed by hand.

| Command | What it does |
|---|---|
| `/sessions` | the session list, cards, controls |
| `/new` | start a session in a chosen directory |
| `/usage` | quota and context, without asking Claude |
| `/screen` | a text snapshot of the terminal |
| `/esc` | presses Esc — **interrupts what Claude is doing**. The session stays alive, its context untouched |
| `/clear` | clears the session's context. The session lives on, its history is gone |
| `/dirs` | the project directories offered when starting a session |
| `/rename` | your own name for a session (`/rename -` gives the automatic one back) |
| `/lang` | interface language (`en` and `uk` for now) |
| `/update` | which Claude Code each session runs, which one is on disk, and a restart that keeps the context |
| `/exit` | **ends the session**: Claude shuts down on its own, then the tmux window closes |

All of them act on the **active** session, or — in a reply — on the one whose
message you are quoting.

A distinction worth keeping in mind: `/esc` stops the *action*, `/clear` erases
the *memory*, `/exit` closes the *session*. After `/exit` the transcript stays,
so the session can be brought back from 🕘 in the list.

`/usage` and the card's "📉 Context breakdown" are not the same thing either.
`/usage` is the bot's own summary, read off the status-line payload: how much of
the 5-hour and 7-day quota is spent, how full the context is, plus model, effort
and cost. It is instant, costs no tokens and does not disturb the session.
"📉 Context breakdown" sends Claude's own `/context` into the session and asks
*what* is filling the window — system prompt, tools, MCP, memory files — and
that is a real turn.

Any other `/command` (`/model`, `/compact`, `/cost`, `/context`…) is passed to
Claude unchanged.

### Why /exit and not /kill

The process is not killed. `/exit` goes into the session — Claude shuts itself
down and gets to finish writing the transcript; only then is the tmux window
taken away. Calling it `kill` would promise the opposite. Ctrl+D does not work
here: the TUI ignores it (verified).

## Updating Claude Code

Claude Code updates itself in the background, but a running process keeps the
build it started with: the new one reaches a session only when its `claude` is
started again. Nothing in the TUI says so, so a session can sit three releases
behind for a week and look perfectly healthy.

`/update` puts both numbers on one card — the version on disk, the version each
session is actually running — and offers the restart that closes the gap.

```
⬆️ Claude Code

On disk: 2.1.251 — what a session gets the moment it is started again.

✅ finman · calendar — 2.1.247 ⬆️
✅ pay4say · widget — 2.1.245 ⬆️
▶️⏸ claude-tg-bot · the update card — 2.1.241 ⬆️

3 sessions are behind.
Busy or waiting, so left alone: claude-tg-bot · the update card
```

**Where the numbers come from.** On disk: `claude --version`, asked at most
every five minutes — it only moves when the background updater has been at
work. In a session: the `version` field of the status-line payload, which the
process rewrites on every status-line render, with the transcript as the
fallback where the tee is not installed. Both are written by the process
itself, so they describe the build that is running and not the file on disk.

**What a restart is.** `/exit` into the session, then
`claude --resume <id> -n <name>` in the *same* tmux window. The session id
survives a resume, so the transcript keeps growing in the same file, the
reader's offset stays valid, and nothing is replayed into the chat. The launch
name comes back exactly as it was — it is the only thread back to a session
that is later `/clear`ed. The context comes back with the history; the prompt
cache does not, so the first reply after a restart takes a little longer, and
anything half-typed into the session's input line is gone.

**The self-update is the hazard here.** Claude Code replaces its own binary in
place, and for a second or two `claude` is missing — or half-written, which
bash reports as `Exec format error`. A session shut down inside that window has
nothing to come back to: that is exactly how one session ended up at a shell
prompt on 2026-08-30. So the CLI is checked *before* anything is shut down, and
a launch that does not come up is tried once more. Even then the window is
left standing, and pressing restart again resumes the session.

**What it refuses to do.** A session that is working, or waiting on a question,
is left alone — checked twice over, because neither source is enough on its
own: `claude agents --json` (asked fresh, not from the five-second cache) knows
about dialogs the screen parser does not recognise, and the screen answers
immediately for a session that has only just been relaunched and is missing
from the agent list for a couple of seconds. If `/exit` does not finish in
20 seconds, or the resumed `claude` does not appear within 40, the restart
stops and says so — the window is never killed, unlike `/exit` from the menu.

**"🆕 What's new"** answers the obvious next question — what a restart would
actually bring in. Claude Code keeps the full changelog at
`~/.claude/cache/changelog.md` and refreshes it when it updates itself, so the
notes are read from disk: no network, no tokens. The card shows exactly the
releases between the oldest session still behind and the version on disk
(`2.1.247 → 2.1.251 — 3 releases`), newest first, and names how many older ones
were left out. `/release-notes` inside a session is the same thing from the
other side.

**"🔄 Check for updates"** runs `claude update`, which is the CLI's own
"check and install if there is anything". It changes what is on disk and
nothing else: the restart is still yours to press.

**Being told about it.** When a release lands on disk while sessions are behind,
the watcher says so once — one message per version, remembered in
`config.json`, so a bot restart does not turn one release into a second
notification. Between messages the badge is enough: `⬆️` and a count in
`/sessions`, the two versions on the session card, and the disk version in
`/service`.

## Three kinds of session in the list

* **managed** (`⏸ ⚡ ✅`) — created by the bot in tmux, full control;
* **foreign** (`🔗`) — started by you in an ordinary terminal. They cannot be
  driven from here, but they are visible, and the "🔗 Move to tmux" button sends
  the process a SIGTERM and brings `claude --resume` up in a tmux window. The
  card says so plainly, and separately warns when the session is busy right now;
* **closed** (`🕘`) — history, brought back with `--resume`. Sessions where
  nothing happened (only `/exit`) do not make the list: there is nothing to
  resume. Judging that has to be done by content, because even an empty session
  weighs hundreds of kilobytes thanks to the file-history snapshot.

There is no reaching the stdin of a foreign session from outside, so a clean
`/exit` is impossible for one — hence a signal instead of a command.

## The session card

A button caption is a single line, so the full name and path do not fit there.
Picking a session from the list therefore opens a card with every known
attribute first, and only the "▶️ Open" button makes the session active. The
list headers say as much — "a tap shows the details; whether to open it is
decided there" — so that tapping never feels irreversible:

```
▶️ claude-tg-bot-b7e1

📁 /home/user/dev/claude-tg-bot
📊 Status: idle
🧠 Opus 5 (1M context) · ◉ xhigh · 🔐 auto
📈 Context: 5% (46,864 tokens)
    5 hours: 27% · resets today 15:10
    7 days: 24% · resets Fri 22:00
💵 Session cost: $0.21
🕘 Created: 23.08 12:09
🖥 tmux: @3
🔑 6789ea9b-2bcc-4626-8e9e-97ee33c717dc

[▶️ Open]  [⬅️ Back]
```

A closed session gets a card of its own — full name, path, time of last
activity, the first prompt (when the name came from an AI title), transcript
size — and only then "🕘 Resume".

A fresh session has no metrics yet: `used_percentage` and `rate_limits` come
back `null` in the payload until the first request has gone through. The card
says exactly that instead of showing zeros.

## Exact metrics

The figures come from the same JSON Claude Code hands its status line, not from
the bars that line draws. The bars are rounded to 10 %, reset times to the
minute, and the labels depend on how the script is written; the payload, by
contrast, carries exact `context_window.used_percentage`, the window size,
`rate_limits.*.resets_at` (epoch), `effort.level`, the model and the session
cost.

The interception is a wrapper that **does not change** your status line:

```
"statusLine": { "command": ".../claude-tg-bot/bin/statusline-tee.sh" }
```

It mirrors stdin into `~/.cache/ccbot/status/<session_id>.json` and hands it on
to your own script — `~/.claude/statusline-command.sh`, or whatever is named in
`CCBOT_REAL_STATUSLINE`. If you have no status line of your own, the wrapper
draws a minimal one itself rather than leaving a blank. Every failure path
inside is non-fatal: if the write fails, the status line still renders as
before.

`./install.sh` wires it up for you, backing up `settings.json` first. If a
status line is already configured, the installer does not lose it: it generates
a one-line wrapper, `~/.config/ccbot/statusline.sh`, which sets
`CCBOT_REAL_STATUSLINE` and calls the tee. The variable is not written straight
into `statusLine.command` because nothing promises that field is run through a
shell — and `VAR=x /path` without a shell means nothing.

Remove the wrapper from `settings.json` and the bot falls back to parsing the
screen on its own (warning about it in the log) — the numbers simply get
coarser.

## Context and limits

Three ways, because each one alone has a flaw: showing it constantly is noise,
showing it only on request means remembering to ask, and thresholds alone never
answer "how much right now?".

1. **On request** — `/usage`, or the "📊 Limits" button: context as a percentage
   and as tokens out of the window size, every limit with its reset time, plus
   the model, the effort level and the session cost.
2. **Automatically at thresholds** — context at 60/75/85/95 %, limits at
   80/90/95 %. Each step fires once and rearms when the value drops (after
   `/clear`, or with a new limit window).
3. **A quiet suffix** — `· ctx 62% · 5h 81%` appended to Claude's answer, but
   only once context is ≥ 50 % or a limit is ≥ 75 %. While there is plenty of
   room, there is no noise at all.

Figures older than 3 minutes are marked stale — the session simply has not
redrawn its status line in a while.

Reset times ("resets tomorrow 09:00") are quoted on the host's own clock. WSL
images often sit on `Etc/UTC` even though you do not, and a bot on a rented
server is in the same position, so set `CCBOT_TZ=Europe/Kyiv` in `.env` when the
host clock is not yours.

## Complicated dialogs

Multi-step questions arrive one at a time: answer, and the next one comes.

When an option has an ASCII diagram, Claude draws it to the side and only for
the highlighted row. The bot splits the screen into columns: option labels go
onto buttons, and the diagram is delivered according to its width.

For years this was the hard part: mobile Telegram did not scroll a code block,
it wrapped it, so a wide diagram fell apart on a phone while looking flawless on
the desktop. Anything wider than 36 characters had to leave as a PNG.

**Bot API 10.1 (June 2026) removed the problem.** A rich message's preformatted
block scrolls sideways, so the diagram now travels inline as text at any width —
it lines up, and it can be copied. Two things were measured on a live client
rather than read in the documentation:

* a single block accepted **30 000** characters, where the docs promise 1024;
* box-drawing (`─ │ ┼`) is **not a letter wide** in Telegram's monospace face,
  so a captured frame comes out ragged. Plain ASCII (`- | +`) lines up exactly,
  which is what `rich.ascii_frame()` converts it to.

The PNG path (`ccbot/render.py`, Pillow + DejaVu Sans Mono) stays underneath as
the fallback for a client that has no rich messages; "🖼 Diagram" still redraws
the current option on demand.

A small thing that cost time back then, and still applies to the fallback: the
lines in the PNG overlap by a pixel (`LINE_SPACING = -1`) — otherwise the `│`
glyph renders a hair shorter than its cell and every vertical comes out dotted.

Parsing the columns is needed precisely so the graphics do not end up inside
option labels. The question and the heading are rendered across the full width,
so they are not cut at a column boundary, and the frame characters are stripped
off their edges.

The ⬆️ ⬇️ ✅ buttons page through the list, and the message is **edited** rather
than duplicated. The arrows also reach options with no number ("Chat about
this") that no digit corresponds to.

## Attachments

Anything Telegram can carry — a photo, an album, a document of any type, a
video, an audio file, a voice message, a sticker — is saved into
`~/.cache/ccbot/media/<session>/` and handed to Claude as a path. Its `Read`
displays images natively; a JSON, a log or a CSV it simply reads, and an
archive or an audio file it can unpack or convert on its own. The system
clipboard is not used here: WSL has no display, so there is no way to paste a
picture "with Ctrl+V".

**The format is not filtered.** It used to be — images and PDFs only, and a
forwarded JSON came back as "I only take images and PDFs", which is a bot
deciding what Claude is capable of reading. The only real limit is Telegram's:
a bot may download **20 MB**, and past that the answer says so and suggests
sending a path instead.

A file keeps the name it was sent with (sanitised, and given a `-1` suffix
rather than overwriting a namesake): the path is all Claude sees, and
`dialog-with-maksym.json` says more than `20260828-131032-0.json`. Only what
arrives nameless — a photo, a voice note — is named by timestamp.

* with a caption — it goes straight through together with it;
* without one — it waits for the next text message;
* an album is gathered into a single prompt.

The prompt says "image" only when every attachment really is one; otherwise it
says "file", and inside a forwarded transcript the marker is `[file 2]` rather
than `[image 2]`.

Attachments older than 14 days are cleared out when the bot starts.

## One burst, one prompt

Nothing goes to a session the moment it arrives. Every incoming message lands
in a per-chat inbox and leaves 0.8 s after the last of them
(`_INBOX_QUIET`) — as **one** prompt, ordered by `message_id`. An attachment
still coming down from Telegram holds the batch open, so the picture is never
mentioned in a prompt that arrives before the file exists.

The reason is what a phone actually sends. Forwarding a conversation produces a
message per bubble, Telegram hands the whole lot over in a single poll (13 of
them within 190 ms on 2026-08-27), and aiogram runs an update per task. Sent
straight through, that became 13 prompts in whatever order the event loop
scheduled them, each with its own Enter — and 13 simultaneous
`tmux load-buffer`/`paste-buffer` pairs on one buffer name, of which 11 died
with `no buffer ccbot-7`, because `paste-buffer -d` deletes the buffer another
call was about to paste. Hence both halves of the fix: the inbox, and one lock
per window in `tmux.py` (`tmux.submit_text` — paste and Enter, indivisible).

A forwarded batch also keeps its authorship. `forward_origin` says who wrote
each message — a visible user, a hidden one, a group, a channel — and the
prompt becomes a transcript:

```
Take a look at this image:
1. /home/vik/.cache/ccbot/media/0444d8e4/20260827-131032-0.jpg

Forwarded from Telegram, 13 messages, oldest first:

Andrii: hi!
Andrii: did you not set the plans up??
Vik: Hi
Andrii [image 1]: why like this??
…
```

Without the names, two people answering each other collapse into one voice
contradicting itself. Messages the user typed themselves have no author to
name, so they are simply joined by a blank line, and a single message is passed
through byte for byte — the ordinary case is untouched.

Two things deliberately skip the queue: a `/command` bound for Claude (it
flushes whatever is waiting and then goes alone, because a command has to be
the first thing on its line) and an answer to a question the bot asked
("send me a path", a dialog option), which is a conversation of its own.

## Project directories

The list of directories for "➕ New session", and the filter on the resume list,
are nowhere hard-coded. There are three sources, in descending priority:

1. **`CCBOT_DIRS` in `.env`** — a fixed list for this machine. When it is set,
   the `/dirs` buttons will not change it, and the bot says so.
2. **`/dirs` in the bot** — add or remove a directory straight from the phone;
   stored in `~/.config/ccbot/config.json`.
3. **Auto-discovery** — with neither of the above, directories are taken from
   the history of your own Claude sessions. A fresh clone shows something
   meaningful right away.

The roots are always present, and under them the subdirectories worked in
recently. A directory outside the list stays reachable through "✏️ Other path".

## Interface language

The interface is not nailed to any one language: the strings live in gettext
catalogues, and the base language of the code is English. Ukrainian is a
translation like any other that may follow.

The language is decided from three sources, in descending priority:

1. **`/lang` in the bot** — an explicit choice, stored in
   `~/.config/ccbot/config.json`;
2. **the Telegram profile's `language_code`** — a guess that is almost always
   right, which is why the bot asks nothing on first start;
3. **English** — when there is no translation for the profile's language.
   English, and not empty strings: the `msgid` in the code *is* the English
   text, so the worst a broken `.mo` can do is leave the interface in English.

`/lang` takes effect **without a restart**. Handlers get the language from a
middleware on every update, and the watcher — a background loop with no update
to look at — rereads the setting on every tick and opens the context itself.
The "/" menu is republished along with the language, since Telegram would
otherwise keep showing the old command list.

Why gettext rather than JSON dictionaries: `.po` is understood natively by
Crowdin, Weblate and POEditor — a translator can add a language without
touching Python. And `ngettext` gives the correct **three** Ukrainian plural
forms ("1 файл / 2 файли / 5 файлів"), which are almost impossible to get right
by hand.

```
locales/
  bot.pot                     # template, generated from the code
  en/LC_MESSAGES/bot.po|.mo   # the base: msgstr repeats msgid
  uk/LC_MESSAGES/bot.po|.mo   # a translation
```

The `.mo` files **are committed**: `git clone && python main.py` has to work
without Babel. Babel is needed only by whoever changes the texts.

## Adding a language

There are only two languages to begin with, and that is deliberate: nobody will
ever see or correct a machine translation into a language they do not read. A
new language arrives through a PR.

```bash
uv pip install -r requirements-dev.txt

# 1. create the catalogue (German, for example)
.venv/bin/pybabel init -i locales/bot.pot -d locales -D bot -l de

# 2. translate the msgstr entries in locales/de/LC_MESSAGES/bot.po

# 3. build the .mo — that is what the bot actually reads
.venv/bin/pybabel compile -d locales -D bot
```

One more step that is easy to miss: add a line to `LANGUAGE_NAMES`
(`ccbot/i18n.py`) — the name of the language **in that language**. A "Deutsch"
button helps somebody who does not read English; "German" does not. Without
that line the button shows `DE`.

After changing strings **in the code**, the template and every translation are
updated like this:

```bash
.venv/bin/pybabel extract -F babel.cfg -k __ -k N_ \
    --add-comments=TRANSLATORS: -o locales/bot.pot .
.venv/bin/pybabel update -i locales/bot.pot -d locales -D bot
.venv/bin/pybabel compile -d locales -D bot
```

`update` marks changed strings `fuzzy` — an invitation to reread the
translation, not litter: `bin/check.sh` will not let a commit through while any
such marks remain. It also catches a `.po` newer than its `.mo`, the most common
mistake of all ("translated it, forgot to compile").

A few rules that keep translations usable:

* **emoji belong inside the string** (`"📋 Sessions"`) rather than being glued on
  in code: a sign that reads unambiguously in one culture may not in another;
* **named placeholders** (`{name}`, not `{}`), or the translator cannot tell
  what goes where;
* **HTML travels with the string** — otherwise word order cannot be changed;
* **logs are not translated**: they are for the developer, and their language is
  English.

## Where the bot keeps its files

Under XDG, not next to the code: the same checkout may be shared, read-only or
packaged, and somebody's data has no business living inside it.

| What | Where |
|---|---|
| Settings the bot changes itself | `~/.config/ccbot/config.json` |
| State (managed sessions, reply routes) | `~/.local/share/ccbot/state.db` |
| Log, attachments, status-line payloads | `~/.cache/ccbot/` |

The exception is `locales/`: translations are part of the code, not user data,
so they live in the repository.

`XDG_CONFIG_HOME` / `XDG_DATA_HOME` / `XDG_CACHE_HOME` are honoured. A database
in the old location (next to the code) is moved by the bot itself on first
start.

## Checks before a commit

`bin/check.sh` — four layers, cheapest first: **ruff** (dead imports, undefined
names, ordering), **basedpyright** (types), an **import check** (the package
loads at all) and **locales** (`.mo` not behind `.po`, no fuzzy entries left).
The same script is installed as a pre-commit hook, so a broken tree simply does
not get committed:

```bash
uv pip install -r requirements-dev.txt
git config core.hooksPath bin/githooks   # the hook is local; redo it after a clone
bin/check.sh
```

The type checker is not a formality here: it catches exactly what otherwise
surfaces at runtime and takes a session down — a renamed `Store` method left
behind at a call site, or a reference to `c.message`, which Telegram serves as
`InaccessibleMessage` for a card older than 48 hours. The emergency way around
is `git commit -n`.

The checks run against the **working tree**, not the index: the bot is started
from the tree, and a tree that fails is a bot that will not come back up.

## Autostart

`./install.sh` does exactly this and verifies the result. By hand: the unit
template lives in `contrib/claude-tg-bot.service`, with `%INSTALL_DIR%`
substituted at install time:

```bash
mkdir -p ~/.config/systemd/user
sed "s|%INSTALL_DIR%|$PWD|g" contrib/claude-tg-bot.service \
    > ~/.config/systemd/user/claude-tg-bot.service
systemctl --user daemon-reload
systemctl --user enable --now claude-tg-bot
```

The unit lives as long as you have at least one session on the machine. To keep
it running with none: `sudo loginctl enable-linger $USER`.

### KillMode=process is not cosmetic

The tmux server is started by the bot itself, so it ends up in the unit's
cgroup. With the usual `KillMode=control-group`, every `systemctl restart` would
take down the whole cgroup — that is, the tmux server and with it **every Claude
session**. Verified on a twin unit: with the default mode the server's PID after
a restart is a different one; with `KillMode=process` it is the same.

Hence `KillMode=process` in the unit: only the bot gets the signal, and sessions
carry on. tmux panes, incidentally, already live in their own
`tmux-spawn-*.scope`, but without this option the server dies along with the bot.

### Driving it from the phone

| Command | What it shows / does |
|---|---|
| `/service` | uptime, PID, code version (git hash + whether the tree is clean), tmux server PID, poll interval, number of managed sessions |
| `/restart` | restart the bot, with a confirmation; the same thing sits on a button in `/service` |

A restart is arranged as an ordinary exit: the bot stops polling and finishes,
and `Restart=always` brings it back in five seconds. That is why the button
appears **only** when the process really is a unit — checked through its own
cgroup rather than `INVOCATION_ID`, which the tmux server inherits and therefore
so does every session. A bot started by hand answers `/restart` with the
instructions for installing the unit and goes nowhere.

Who asked for the restart is recorded in `~/.cache/ccbot/restart.json`, so once
it is back up the bot reports in that same chat: "✅ Back — the restart took N s."
A flag older than 10 minutes is ignored: it stands for a start that never
returned.

## Attaching to the same session locally

```bash
tmux attach -t ccbot \; select-window -t @N
```

## Maintaining the TUI parser

`ccbot/screen.py` is the only place that knows what the Claude Code interface
looks like. If the buttons stop appearing after a CLI update, take a fresh
sample:

```bash
tmux capture-pane -p -t @N > /tmp/dialog.txt
```

and compare it against `find_dialog()`.

A dialog is recognised by its **footer**, and there are two kinds of footer:

* choosing an option — `Enter to select · Esc to cancel`;
* a permission request (from a tool or a `PreToolUse` hook) —
  `Esc to cancel · Tab to amend · ctrl+e to explain`. This one went unrecognised
  for a long time: the session sat quietly on "Do you want to proceed?" while
  the chat showed nothing.

The list of options is searched **bottom-up, from the footer to the item "1."**.
Otherwise the parser latched onto the first numbered line on the screen — and
Claude numbers its prose all the time, so the items of its own plan ended up on
the buttons.

### If the question still is not recognised

`claude agents --json` says `waiting` whatever the dialog looks like, which
makes it a more honest signal than the parser. When a session is waiting and
`find_dialog()` stays silent, the bot sends the screen as it is, with buttons
that work without any parsing: digits, arrows, ✅ and Esc. Staying quiet in that
situation is not an option.

## Later

* **Streaming an answer.** `sendRichMessageDraft` exists for exactly the case
  this bot has — a reply that grows while Claude writes it — and would replace
  the "⏳ working" pulse with the text itself appearing.
* **Native tables.** Claude's Markdown tables are already drawn by the client;
  the bot's own lists (sessions, usage) are still `·`-separated text and could
  become `InputRichBlockTable`.
* **Long plans.** `ExitPlanMode` with a large plan is truncated for now — it
  ought to be split across several messages.

## License

MIT — see [LICENSE](LICENSE). Contributions are welcome; a new interface
language needs no Python at all, only a `.po` file (see "Adding a language").
