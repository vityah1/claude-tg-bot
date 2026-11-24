# claude-tg-bot

A Telegram dispatcher for Claude Code sessions, running on Linux or WSL.

From your phone it lets you see the list of sessions, start new ones in any
directory, bring closed ones back, send prompts and `/`-commands, and — above
all — **answer Claude's interactive questions** with buttons.

An overview — what it is for and how it compares with the built-in remote
control, SSH from the phone and API-wrapper bots — is on the project page:
**[vhworx.com/claude-tg-bot](https://vhworx.com/claude-tg-bot)**.

## Quick start

You need Linux or WSL with a systemd user session, and
[Claude Code](https://claude.com/product/claude-code) installed and signed in.
The installer handles everything else.

```bash
git clone <url> claude-tg-bot && cd claude-tg-bot
./install.sh
```

It checks the prerequisites, builds the virtualenv, and asks for the bot token
from [@BotFather](https://t.me/BotFather) — validating it on the spot, so a
mistyped token is caught in two seconds rather than five minutes later in the
log. Then, instead of sending you off to look up your numeric Telegram id, it
asks you to message the bot and reads the id off that message: one action that
also proves you can reach the bot. Finally it installs the two systemd user
units — the session host first, then the bot — starts them, and confirms the
whole chain works by sending you a message in Telegram.

Run it again whenever you like — it is also the update path
(`git pull && ./install.sh`), and it never overwrites a working `.env` without
asking. `./install.sh --doctor` inspects an existing install and changes
nothing.

**Windows** is not supported natively: the bot and its session host run as
systemd user units, which Windows does not have. Install WSL (`wsl --install`
in PowerShell) and run everything inside it. **macOS** runs the bot fine, but
has no autostart template yet — install by hand, as below.

## How it works

The built-in channels (`--remote-control`, `--channels`) are blocked by a
corporate org policy here. So the bot holds the sessions itself, through the
official [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/python):
questions, permission requests and plans arrive as data (`can_use_tool`), so
the cards are built from data and a new Claude Code release cannot break them.

| Part | Where |
|---|---|
| The session host — a process of its own that holds one `ClaudeSDKClient` per session | `host_main.py`, `ccbot/host/`, unit `claude-tg-host` |
| The socket between them (NDJSON, event journal with replay) | `ccbot/host/protocol.py`, `ccbot/hostlink.py` |
| Host events → chat messages and cards | `ccbot/bridge.py` |
| Chat → sessions: commands, batching, routing, pickers | `ccbot/bot.py` |

Why a separate process: a `claude` child dies with the process that started it,
so sessions held inside the bot would die on every bot restart. The host is
restarted only when its own code changes; the bot restarts freely, and a turn in
progress or an open question survive it — the host journals its events, every
event has a sequence number, and the bot acknowledges one only once its effect
has reached the chat, so after a restart exactly what had not gone out is
replayed.

What a session's card offers:

* **Cards from data.** Questions (one card paged part by part, «✏️ Own answer»,
  «💬 Discuss»), permission requests (Allow / Always / Deny / «✏️ Deny with a
  reason») and plans (Approve / Approve and accept edits / «✏️ Revise» / Keep
  planning) — see [Questions, permissions and plans](#questions-permissions-and-plans).
* **Where it stopped.** `/screen` and «📜 Where it stopped» send a recap read
  from the transcript: the state, your last task, Claude's last steps and last
  answer. No tokens, and true after any restart.
* **Model, effort, mode** come from Claude Code's own `get_server_info()` and
  apply to this session only; the host remembers them across a wake-up.
* **Restart** resumes the same id on the CLI on disk; **/btw** asks a fork of
  the session that keeps its model, mode and tools (so it reads the cached
  context) and is barred from running tools by a hook. The fork is kept out of
  the history list.
* **/login** runs `claude auth login` under a pty: the link comes to the chat,
  the code goes back the same way. The login is the machine's, for every session.

## Manual install

`install.sh` is only the automation of the steps below, never the only place
they are written down. If you would rather not run a script, this is the same
install by hand.

**1. Prerequisites.** Claude Code installed and signed in — `claude agents
--json` has to answer, since being on `PATH` is not the same as being usable.

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

**4. Run it.** Both systemd user units, the host first — see
[Autostart](#autostart). Without systemd, start both by hand:
`.venv/bin/python host_main.py` (the session host) and
`.venv/bin/python main.py` — nothing brings them back after a crash, and
`/restart` in the chat is unavailable. Without the host no session can run.

**5. Check.** `./install.sh --doctor` verifies all of the above — both units,
the host socket — and tails the log, whichever way you installed.

## Model, effort, permission mode

The session card shows the current values and offers buttons to change them.
`/menu` opens that card for the session you are talking to — the buttons below
used to be reachable only by walking through `/sessions` and tapping a row:

* **🧠 Model** and **◉ Effort** list what the session's own
  `get_server_info()` reports — the models with their descriptions, and the
  effort levels the current model supports. Nothing is listed in the bot: a
  table in the source went stale the day Fable 5.1 shipped. A pick applies to
  this session only, and the host keeps it across a wake-up and a restart.
* **🔐 Mode** — auto, plan, accept edits or manual, switched on the running
  session.

## Lists

The message text does not restate what the buttons already say — by the time
you have scrolled a long list, the summary is forgotten. So the top carries
counters only, and all the information lives on the buttons themselves:

```
🕘 Closed sessions — resuming brings the context back.
📁 /home/vik/7loc/backend/pay4say

88 sessions · tap one for details.
   [09:33 · Widget rework of the mileage report]
   [08:32 · Paddle billing agreement, second try]
   [Sun 21:25 · The last deploy]
   [◀️]  [page 2 of 11]  [▶️]
   [📁 Another directory]  [⬅️ Sessions]
```

History is **per directory**, and paged. It has to be: a project accumulates
dozens of transcripts (88 in that example), and a single global list of the
twelve newest — which is what this used to be — buried every one of them under
whatever other project was touched last. `🕘 Recent` opens the active session's
own directory straight away, and `📁 Another directory` lists the rest with a
count each:

```
🕘 Closed sessions — 185 in all, by directory.
   [📁 ~/dev/finman · 60 · 06:30]
   [📁 ~/7loc/backend/pay4say · 88 · yst 20:39]
   [📁 …/7loc-admin-web · 10 · yst 20:34]
```

The button format is `time · what the session is about`; in the by-directory
list it is `path · how many · last activity`. Inside one directory the path is
left off the rows — it is the same on all of them, and the caption is better
spent on what tells them apart.

The name itself comes from the first of these that exists:

1. **what you called it** — `/rename` keeps the name in a table of its own, so
   it survives the session ending and comes back with a resume. Such a row is
   marked `✏️`.
2. **`claude -n`, when a person wrote it** — the bot's own `pay4say-7fda` is
   recognised and skipped.
3. **the AI title** Claude Code invents.
4. **the first prompt** — with the wrapping the bot itself added stripped off,
   so a photo sent from the phone reads as its caption rather than as
   `Take a look at these 3 images: /home/…/media/…`.

Fourth is the common case: Claude Code has all but stopped writing `ai-title`
(one of the sixteen sessions on 31.08.2026 had one), which is exactly why the
other three exist.

`🔍 Find` searches every directory at once — names, first prompt, last prompt.
Not the body of the conversation: that would mean reading two hundred files of
a megabyte each per query, while the four fields are already in the scan cache
and answer in milliseconds.

A card adds `⏹ It stopped on:` next to `💬 It started with:`. Of the two, the
last prompt is usually the one that says which session this was.

The page a card was opened from travels in its `callback_data`
(`res:<sid8>:<dir key>:<page>`), so "⬅️ Back" returns to page 7 rather than to
the top. The directory key is a hash of the path, not its position in the list:
positions move as sessions open and close, and an older card would then page
through a different project.

## Session names

The bot calls a session `directory-1a2b` — unique, but no help in telling two
sessions of the same project apart. So the name in the list and in message
captions is taken from these sources in turn:

1. **your own name** — `/rename Billing audit`, or ✏️ on the session card,
   or just `/rename` picked out of the "/" menu: it asks which name, and takes
   your next message as the answer (with a ⬅️ Cancel button, and it lapses
   after three minutes rather than swallowing a message meant for Claude);
2. **the name it was picked by** — a session resumed from 🕘 history or a
   search keeps the caption its row had there;
3. **the launch name** otherwise.

In messages the name comes **with the directory in front** — `pay4say · pricing` —
because several projects write into one chat at the same time and the name
alone does not say where the work is happening. If the name already starts with
the directory, the prefix is not repeated. In list buttons the directory is a
column of its own, so the name there goes without it.

Renaming changes **only** what you see: alongside it the database keeps `name`,
the name the session was launched with (`claude -n`). That one never changes:
it is the host's key for the session, and it is how the session is followed
after `/clear` (see below).

The first prompt sits behind a file-history preamble that easily grows past
250 KB, so it is looked for in the first megabyte of the file rather than the
first 128 KB — otherwise a closed session would end up with no name at all, and
drop out of the 🕘 list.

## `/clear` changes the session id — and the bot follows

After `/clear`, Claude does **not** restart — same process, same launch name —
but it starts **a new transcript under a new session id**. The host notices
(the SDK reports the conversation reset, and the next message carries the new
id), records the new id against the launch name and emits `session_reset`; the
bridge rebinds the row to it — same name, same reply routes. A line arrives in
the chat: "🧹 *name*: context cleared — the session was given a new id, and I
am following it there", with «🎛 Session controls» opening the fresh card.

That is exactly why renaming touches `custom_name` only: the launch name is the
thread that ties the new id to the old session.

The rebind moves the row, the active-session pointer and the reply routes to
the new id — but not the buttons already sitting in the chat: a card carries
the id it was printed with, cut to eight characters, inside its
`callback_data`. So every control on a card older than the last `/clear` used
to answer "that session is gone", which is what happens to the card you
cleared *from*: press 🧹, then ✏️ Rename on the same card, and the rename was
refused. Hence `session_aliases`, a table of "the id this one became", written
by `Store.rebind` and read by `Store.find_prefix` (and by `Store.follow`, for
the places that write an id down outside the row — a half-finished rename and
text held for an open question). Aliases already pointing at the old id are
moved along with it, so a session cleared three times is still reachable from
its very first card; they expire after 30 days.

## While Claude is thinking

A long turn with reasoning can say nothing for minutes — and in a chat that is
indistinguishable from a hung bot. So after 75 seconds of uninterrupted work
the bot sends **one** line, `⏳ <name> · working for N min`, and then **edits
it in place** every 45 seconds. When the real answer (or a question) arrives,
the pulse is deleted so it leaves no litter.

## Running under systemd: PATH

A user unit does **not** read your shell profile, so `nvm` is not on its PATH —
and `claude` simply does not exist as far as the bot is concerned. The
consequence was insidious: lists of live sessions silently came back empty,
with no error anywhere.

So the bot and the host resolve the CLI themselves: `CCBOT_CLAUDE_BIN` → `PATH`
→ `~/.nvm/versions/node/*/bin/claude` (the bot) → `~/.local/bin` →
`/usr/local/bin`. The host checks on every session start rather than caching
the answer, so a stale `CCBOT_CLAUDE_BIN` does not break a launch while a
working binary is on `PATH`. If nothing is found, the bot sends a warning to the
chat rather than staying quiet. Both units also spell out PATH explicitly.

## The log

Written to `~/.cache/ccbot/bot.log` (rotating, 2 MB × 3) and to journald in
parallel; the host writes `~/.cache/ccbot/host.log`. `/log` shows the last
lines right in the chat.

It is recorded systematically rather than in selected spots: an outer
middleware catches **every** update before any handler — including the ones
that are dropped afterwards (a sender who is not allowed, no handler matched).
Those are precisely the cases that are hardest to reconstruct after the fact.
On top of that: session lifecycle, prompts sent, cards, held and delivered
batches, rejected users. A sign-in code is masked in every line.

`CCBOT_LOG_LEVEL=DEBUG` adds the `claude` stderr the host sees.

## No error goes silent

The worst state to be in is a bot that is alive while every action quietly
fails: in a chat that is indistinguishable from "Claude is just thinking". So
an exception never simply stays in the log:

* **an exception in a handler** is caught by `@dp.errors`, which answers in the
  same chat — what broke (type and message) and that sessions are unaffected;
* **an exception in the bridge tick** stops delivery of answers from every
  session, so it gets a warning of its own — **once an hour** for the same
  breakage;
* **a session host that stays down** for more than 30 seconds is announced
  («The session host is not answering»), and so is its return;
* **a session whose `claude` process ends** says so, with the prompts it cut
  off and «🔁 Send again» for each; the session rests and wakes with your next
  message.

This is what the failure that prompted it looked like: a running process had an
old version of the code while the database had already gained a new column,
every tick died with a `TypeError`, the unit stayed `active`, and the bot said
nothing until somebody restarted it.

## The active session, and working in parallel

Sessions live independently: starting a task in `finman` and then opening
`7loc` does not stop the first one — it keeps working, and its answers arrive
captioned with its own name.

The **active** one is whichever you last created, resumed, or opened with the
"▶️ Open" button. Ordinary text goes there, and the list marks it `🟢`.

A launch — ➕ new, ▶️ resume from 🕘 history or a search — answers with the
same card a row of `/sessions` opens: «▶️ Active session», the controls,
«🟢 Text now goes to …», and the session that was active before it named as
still working, reachable by reply. Both that card and the picker line it
settles into («▶️ Resuming …») belong to the new session, so a reply to either
lands there. It used to be a bare «✅ Resumed: 7loc-7487»: the launch name
instead of the one the session was found by, no buttons, no word about the
text — and on 2026-10-03 two messages meant for a session found by search went
to the one whose question cards were being answered meanwhile.

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
| `/menu` | one session's card, without the list in front of it: model, effort, permission mode, restart, rename, end |
| `/new` | start a session in a chosen directory |
| `/usage` | quota and context, without asking Claude |
| `/screen` | where the session stopped: its state, last task, last steps and last answer |
| `/esc` | **interrupts what Claude is doing**. The session stays alive, its context untouched |
| `/clear` | clears the session's context. The session lives on, its history is gone |
| `/dirs` | the project directories offered when starting a session |
| `/rename` | your own name for a session (`/rename -` gives the automatic one back) |
| `/lang` | interface language (`en` and `uk` for now) |
| `/update` | which Claude Code each session runs, which one is on disk, and a restart that keeps the context |
| `/login` | logs Claude in to another account (also «🔑 Other account» on the session card): the sign-in link comes to the chat, the code goes back as a message |
| `/exit` | **ends the session**: the host shuts its Claude down |
| `/service`, `/restart`, `/log` | the bot itself — see [Driving it from the phone](#driving-it-from-the-phone) |

All of them act on the **active** session, or — in a reply — on the one whose
message you are quoting.

A distinction worth keeping in mind: `/esc` stops the *action*, `/clear` erases
the *memory*, `/exit` closes the *session*. After `/exit` the transcript stays,
so the session can be brought back from 🕘 in the list.

`/usage` and `/context` are not the same thing either. `/usage` is the bot's own
summary of what the session last reported to the host: how much of the 5-hour
and 7-day quota is spent, how full the context is, plus model, effort and cost.
It is instant, costs no tokens and does not disturb the session. `/context` is
Claude Code's own breakdown of what is filling the window (system prompt,
tools, MCP, memory files, messages), sent into the session like any other
command; its answer comes back as the turn's result. The card's "🗜 /compact"
runs `/compact`: the conversation is summed up and the session carries on with
the summary instead of the history, keeping its id and its name, and the chat
is told «🗜 Context compacted» when it is done. "🔁 Restart" next to it is the
restart `/update` offers (below), for one session and whatever version it is
on.

Any other `/command` (`/model`, `/cost`, `/context`…) is passed to Claude
unchanged.

## Updating Claude Code

Claude Code updates itself in the background, but a running process keeps the
build it started with: the new one reaches a session only when its `claude` is
started again. Nothing says so, so a session can sit three releases behind for
a week and look perfectly healthy.

`/update` puts both numbers on one card — the version on disk, the version each
session is actually running — and offers the restart that closes the gap.

```
⬆️ Claude Code

On disk: 2.1.251 — what a session gets the moment it is started again.

☑️ finman · calendar — 2.1.247 ⬆️
☑️ pay4say · widget — 2.1.245 ⬆️
🟢⏸ claude-tg-bot · the update card — 2.1.241 ⬆️

3 sessions are behind.
Left alone by «Restart all»:
• claude-tg-bot · the update card — it is waiting for an answer
```

**Where the numbers come from.** On disk: `claude --version`, asked at most
every five minutes — it only moves when the background updater has been at
work. In a session: the version the session's own process reports to the host,
with the transcript as the fallback. Both describe the build that is running
and not the file on disk.

**What a restart is.** The host leaves the session's client and resumes the
same id on the CLI on disk. The session id survives a resume, so the
transcript keeps growing in the same file and the launch name stays as it was;
the model, effort and mode picked from the chat come back with it. The context
comes back with the history; the prompt cache does not, so the first reply
after a restart takes a little longer.

**What it refuses to do.** A session that is working, or waiting on a question,
is left alone — the host checks, and the refusal says why. A resting session
needs no restart: it wakes on the build on disk with your next message.

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

**Being told about it.** When a release lands on disk while running sessions
are behind, the bot says so once — one message per version, remembered in
`config.json`, so a bot restart does not turn one release into a second
notification. It looks every ten minutes, and resting sessions do not count.
Between messages the badge is enough: `⬆️` and a count in `/sessions`, the two
versions on the session card, and the disk version in `/service`.

## Logging in to another account

When one account's limits run out, `/login` — or «🔑 Other account» on the
session card — switches the machine to another without going to it. The bot
runs `claude auth login` under a pseudo-terminal and sends the sign-in link it
prints as a button. The page it opens shows a code (`<code>#<state>`), which
goes back as an ordinary message: the bot deletes the message, enters the code,
and says which account it now is (`claude auth status`). A rejected code gets
«🔁 Try again with a new link»; «✖️ Cancel» stops the login.

**The code takes its own road.** Text shaped like a code (`…#…`, base64url on
both sides, not forwarded) is caught before the inbox and never becomes a
prompt. A message that is nothing but a code, with no login waiting, is
answered and goes nowhere. The log records it masked, in the handler and in the
update middleware.

**The sessions follow on their own.** The login is the machine's: every running
session sends its next request with the new credentials — no restart
(verified on 2.1.318: a session started at 11:44 sent its first request after
a login made elsewhere at 13:39 as the new organisation).

### Which account each session runs

`👤 Account: …` stands on the session card, its details, the launch card,
`/usage` (right under the name — the limits belong to the account), and the
`/sessions` caption (one line when every session shares an account, one line
per account otherwise). A limit alert ends with the address too: that is the
moment to switch. Closed sessions show none — a resume runs under the current
login.

`~/.claude.json` is **not** the source: it names the last login only, and a
session moves to it with its next request — one that has been quiet since is
still on the account it last used. The session says it itself, in two
attachment records of its transcript: `session_context`, whose `userEmail`
names the address, written with the first request after a start, a resume or a
login; and `credential_org` (an organisation id), written beside it and
**alone** when the credentials changed under a running session — the address
is not repeated then. `ccbot/account.py` takes the newest of both (read from
the end once, then only the appended bytes). A `credential_org` newer than the
address is resolved through the organisations whose address is known (the pair
written together, and `oauthAccount` in `~/.claude.json`, remembered in
`~/.cache/ccbot/account-orgs.json` across restarts); one that cannot be
resolved — or that several addresses share, as a team does — shows nothing
rather than the stale address. A record is trusted only if it is not older
than the running process: a resume appends to the same file and writes its own
record only with the first prompt, so until then nothing is shown rather than
the previous run's account.

## Two kinds of session in the list

* **managed** (`⏸ ⚡ ☑️`) — started or resumed by the bot and held by the
  session host, full control;
* **closed** (`🕘`) — history, brought back with a resume through the host.
  Sessions where nothing happened do not make the list: there is nothing to
  resume. Judging that has to be done by content, because even an empty session
  weighs hundreds of kilobytes thanks to the file-history snapshot. A session
  still running somewhere (`claude agents --json` names it) is not offered as
  closed either.

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
🔑 6789ea9b-2bcc-4626-8e9e-97ee33c717dc

[▶️ Open]  [⬅️ Back]
```

The controls under an opened session: «🔔 Show the question» (only while a
question card stands — it posts that card again at the bottom of the chat, for
when it has scrolled far up), «📜 Where it stopped», ⏸ Esc, 📊 Limits,
🧹 /clear, 🗜 /compact, 🔁 Restart, 🧠 Model, ◉ Effort, 🔐 Mode, ✏️ Rename,
🔑 Other account and 🚪 End.

A closed session gets a card of its own — full name, path, time of last
activity, the first prompt (when the name came from an AI title), transcript
size — and only then "🕘 Resume".

A fresh session has no metrics yet: the context and the limits arrive with the
first request. The card says exactly that instead of showing zeros.

## Context and limits

The figures are what the session reports to the host — exact context usage
and window size, every rate limit with its reset time, the model, the effort
level and the session cost. Three ways to see them, because each one alone has
a flaw: showing it constantly is noise, showing it only on request means
remembering to ask, and thresholds alone never answer "how much right now?".

1. **On request** — `/usage`, or the "📊 Limits" button: context as a percentage
   and as tokens out of the window size, every limit with its reset time, plus
   the model, the effort level and the session cost.
2. **Automatically at thresholds** — context at 60/75/85/95 %, limits at
   80/90/95 %. Each step fires once and rearms when the value drops (after
   `/clear`, or with a new limit window).
3. **A quiet suffix** — `· ctx 62% · 5h 81%` appended to Claude's answer, but
   only once context is ≥ 50 % or a limit is ≥ 75 %. While there is plenty of
   room, there is no noise at all.

Reset times ("resets tomorrow 09:00") are quoted on the host's own clock. WSL
images often sit on `Etc/UTC` even though you do not, and a bot on a rented
server is in the same position, so set `CCBOT_TZ=Europe/Kyiv` in `.env` when the
host clock is not yours.

## Questions, permissions and plans

Every card is built from the request's data, never from a screen:

* **A question** (`AskUserQuestion`) is one card paged part by part — answer a
  part and the card shows the next, with «⬅️ Back» to the previous one. Options
  are buttons; a multi-select part ticks ☑ / ☐ and sends the set with «✅ Next»
  or «✅ Send». «✏️ Own answer» takes your next message as the answer to the
  part on the card; «🚫 Don't answer» declines.
* **«💬 Discuss» answers nothing.** It declines the question with an
  instruction to ask what you would like to clarify first, so Claude asks, and
  your next message is read as the reply to that — one message, one turn.
* **A permission request** shows the command or the file and the change, with
  Allow / Deny, «✏️ Deny with a reason» (your next message goes to Claude with
  the refusal) and up to two «✅ Always: …» rules the request suggests.
* **A plan** (`ExitPlanMode`) shows the plan with Approve, Approve and accept
  edits, «✏️ Revise» (your next message is what to change) and Keep planning.

A card whose data the renderer cannot read still arrives, with buttons that
answer it. Once answered, a card keeps its text and trades its buttons for one
inert button naming the answer; a question closed any other way is marked
«⏹ Question closed». Nothing from a session lands under its own open card: its
output waits until the card is answered.

Answering from a card also **makes that session active**. It has to: what
follows an answer belongs to the same thread, and plain text goes to the active
session. Before that, a clarification typed after pressing a card button could
land in another session entirely — which happened on 2026-09-01, and the other
session started working on a question nobody had asked it.

### Text sent while a question is open

Text is never sent into a session that is waiting on a question: it would land
beside the question nobody has answered. It is held, and a card asks what to do
with it — «✏️ This answers the question» (for a question that takes a typed
answer), «💬 Close the question and send» (declines the open request, then
sends), «⏳ Send after the answer» or «🗑 Don't send». Anything sent while that
card stands joins the same held text, and the card is asked again at the
bottom. The host checks too: a guarded send is refused while a request is
open, so nothing slips in between.

## Attachments

Anything Telegram can carry — a photo, an album, a document of any type, a
video, an audio file, a voice message, a sticker — is saved into
`~/.cache/ccbot/media/<session>/` and handed to Claude as a path. Its `Read`
displays images natively; a JSON, a log or a CSV it simply reads, and an
archive or an audio file it can unpack or convert on its own.

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
scheduled them, each a turn of its own.

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
("send me a path", «✏️ Own answer»), which is a conversation of its own.

### A forwarded batch asks where it goes

Forwarding a conversation is normally how a session is given the context it
should *start* from, so its destination is a question rather than "whichever
session is active": the batch is held (`_Parked`) and the chat is asked, with
«▶️ Current: *name*», «📋 Another session» and «🗑 Discard». The picker behind
the second one is the session list with every row a destination — the running
sessions, ➕ a new one in a directory you pick, 🕘 a closed one to bring back.

Three details make it usable rather than merely correct:

* **Everything sent while the question is open joins the same batch**, and the
  question is deleted and asked again at the bottom. The comment that explains
  a forward ("look at this") is part of the forward, and a question scrolled
  above the new messages is one that gets answered for the wrong batch.
* **Both "new" and "recent" go through `_create`**, which delivers the batch
  itself once the host has the session up (`_deliver_after_launch`), so there
  is one delivery path instead of three.
* **A forward sent as a reply to a session's message is not asked about** —
  the reply has already said where it goes — and the answer makes that session
  active, because what the user types next belongs to the thread they have just
  started. A batch nobody routes expires after an hour and says so; a file that
  arrives before any session owns it is downloaded under `media/inbox`.

Anything the user types themselves still goes straight to the active session:
a question per message would make the ordinary case cost a tap. **With no
session active, a batch is parked the same way, forwarded or not**, and the
question opens straight on the destinations (the running sessions, ➕ a new
one, 🕘 a closed one, 🗑 discard) — there is no «Current» to offer, and
answering "no active session" used to drop the message, so it had to be sent
again once a session existed. A Claude command on its own (`/compact`) is the
exception: it acts on a running session, so it still gets that answer. A
session started from /sessions while a batch waits moves the question under
its card, where «▶️ Current» now names it.

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

The home directory is a fourth root, an implicit one, and only as an exact
match: `~` cannot be put on the list — as a root it would take in every project
below it and leave no shortlist at all — while sessions do get started there.
Without it their transcripts were the one thing the history could not show,
however many of them there were.

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
middleware on every update, and the background loops — which have no update to
look at — reread the setting on every tick and open the context themselves.
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
| The host's sessions and event journal | `~/.local/share/ccbot/host.db` |
| The host socket | `$XDG_RUNTIME_DIR/ccbot/host.sock` |
| Logs, attachments | `~/.cache/ccbot/` |

The exception is `locales/`: translations are part of the code, not user data,
so they live in the repository.

`XDG_CONFIG_HOME` / `XDG_DATA_HOME` / `XDG_CACHE_HOME` / `XDG_RUNTIME_DIR` are
honoured (`CCBOT_HOST_SOCKET` overrides the socket path). A database in the old
location (next to the code) is moved by the bot itself on first start.

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
templates live in `contrib/`, with `%INSTALL_DIR%` substituted at install
time — the host first, since the bot's unit wants it:

```bash
mkdir -p ~/.config/systemd/user
for u in claude-tg-host claude-tg-bot; do
  sed "s|%INSTALL_DIR%|$PWD|g" contrib/$u.service > ~/.config/systemd/user/$u.service
done
systemctl --user daemon-reload
systemctl --user enable --now claude-tg-host claude-tg-bot
```

`claude-tg-host` is the session host (see [How it works](#how-it-works)). Its
unit uses `KillMode=control-group` on purpose: the `claude` processes are its
children and must end with it — one left behind by a host that died would hold
a session nobody can reach. The bot's unit uses the default kill mode too: it
holds no session, so a stop or restart takes only the bot (and a
`claude auth login` it may be running).

The units live as long as you have at least one session on the machine. To keep
them running with none: `sudo loginctl enable-linger $USER`.

### When the host or the machine restarts

Sessions rest in the host. After a host restart, or a reboot, each one wakes by
resume — same id, same context — with the next message sent to it. Work that
was in progress at that moment is not picked up again: the chat gets «🔌 The
session host was restarted», naming the prompts that were cut off with
«🔁 Send again» for each, and how many open questions were closed with it.

### Driving it from the phone

| Command | What it shows / does |
|---|---|
| `/service` | uptime, code version (git hash + whether the tree is clean), Claude Code on disk, PID and whether systemd supervises it, number of managed sessions, the journal, and the session host: PID, uptime, SDK version, live sessions |
| `/restart` | restart the bot, with a confirmation; the same thing sits on a button in `/service`. Sessions are unaffected |

A restart is arranged as an ordinary exit: the bot stops polling and finishes,
and `Restart=always` brings it back in five seconds. That is why the button
appears **only** when the process really is a unit — checked through its own
cgroup rather than `INVOCATION_ID`, which every process started inside a unit
inherits (a Claude session of the host, and a bot launched by hand from one of
them). A bot started by hand answers `/restart` with the instructions for
installing the unit and goes nowhere.

Who asked for the restart is recorded in `~/.cache/ccbot/restart.json`, so once
it is back up the bot reports in that same chat: "✅ Back — the restart took N s."
A flag older than 10 minutes is ignored: it stands for a start that never
returned.

## Later

* **Streaming an answer.** `sendRichMessageDraft` exists for exactly the case
  this bot has — a reply that grows while Claude writes it — and would replace
  the "⏳ working" pulse with the text itself appearing.
* **Native tables.** Claude's Markdown tables are already drawn by the client;
  the bot's own lists (sessions, usage) are still `·`-separated text and could
  become `InputRichBlockTable`.
* **Long plans.** A plan too large for one rich message goes out as plain text
  in pieces; it ought to be split across several rich messages instead.

## License

MIT — see [LICENSE](LICENSE). Contributions are welcome; a new interface
language needs no Python at all, only a `.po` file (see "Adding a language").
