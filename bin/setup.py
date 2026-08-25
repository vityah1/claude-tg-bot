#!/usr/bin/env python3
"""Interactive installer and doctor for claude-tg-bot.

It runs on the *system* Python, before the virtualenv exists, so it uses
nothing but the standard library. Every step it performs is also spelled out
in the README under "Встановлення вручну" — this script is the automation of
those steps, never the only place they are written down.

    ./install.sh            install, or update an existing install
    ./install.sh --doctor   diagnose an existing install, change nothing
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
VENV_PY = VENV / "bin" / "python"
MIN_PY = (3, 12)
UNIT = "claude-tg-bot"
UNIT_FILE = Path.home() / ".config" / "systemd" / "user" / f"{UNIT}.service"
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
TEE = ROOT / "bin" / "statusline-tee.sh"


def _xdg(env: str, default: str) -> Path:
    """Mirror of ccbot/paths.py — which this script must not import.

    It runs before the virtualenv exists, so it cannot depend on the package
    it installs.
    """
    raw = os.getenv(env)
    return (Path(raw).expanduser() if raw else Path.home() / default) / "ccbot"


LOG_FILE = _xdg("XDG_CACHE_HOME", ".cache") / "bot.log"
# Generated only when a status line is already configured: see wire_statusline.
WRAPPER = _xdg("XDG_CONFIG_HOME", ".config") / "statusline.sh"


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

_TTY = sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def head(text: str) -> None:
    print("\n" + _c("1", text))


def ok(text: str) -> None:
    print("  {} {}".format(_c("32", "OK"), text))


def warn(text: str) -> None:
    print("  {} {}".format(_c("33", "!!"), text))


def bad(text: str) -> None:
    print("  {} {}".format(_c("31", "XX"), text))


def info(text: str) -> None:
    print("     " + text)


def die(text: str, *hints: str) -> NoReturn:
    bad(text)
    for hint in hints:
        info(hint)
    raise SystemExit(1)


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        raw = input(f"  {prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit("cancelled") from None
    return raw or default


def confirm(prompt: str, default: bool = True) -> bool:
    raw = ask("{} ({})".format(prompt, "Y/n" if default else "y/N")).lower()
    return default if not raw else raw.startswith("y")


def run(cmd, timeout: int = 120, **kw):
    """Run a command and return (returncode, combined output)."""
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, **kw
        )
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    return p.returncode, (p.stdout + p.stderr).strip()


# --------------------------------------------------------------------------
# Platform
# --------------------------------------------------------------------------

def is_wsl() -> bool:
    if os.getenv("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower()
    except OSError:
        return False


_PKG = [
    ("debian ubuntu linuxmint pop raspbian", "sudo apt update && sudo apt install -y {}"),
    ("fedora rhel centos rocky almalinux", "sudo dnf install -y {}"),
    ("arch manjaro endeavouros", "sudo pacman -S --needed {}"),
    ("opensuse suse", "sudo zypper install -y {}"),
    ("alpine", "sudo apk add {}"),
]


def install_hint(pkg: str) -> str:
    """The exact command this distribution uses to install a package."""
    ids = ""
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith(("ID=", "ID_LIKE=")):
                ids += " " + line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    for names, template in _PKG:
        if any(name in ids.split() for name in names.split()):
            return template.format(pkg)
    return f"install '{pkg}' with your package manager"


def check_platform() -> None:
    head("Platform")
    if sys.platform == "darwin":
        die(
            "macOS is not supported by this installer yet.",
            "The bot itself runs on macOS, but its autostart unit is systemd-only;",
            "a launchd equivalent has not been written. Install by hand instead —",
            "see the manual steps in README.md.",
        )
    if sys.platform.startswith("win") or os.name == "nt":
        die(
            "Native Windows is not supported, and cannot be.",
            "The bot drives the Claude Code TUI through tmux, which does not exist",
            "on Windows. Install WSL and run this from inside it:",
            "    wsl --install        (in PowerShell, then reopen a terminal)",
        )
    if not sys.platform.startswith("linux"):
        die(f"Unsupported platform: {sys.platform}")
    ok("WSL ({})".format(os.getenv("WSL_DISTRO_NAME", "Linux under Windows"))
       if is_wsl() else "Linux")


# --------------------------------------------------------------------------
# Prerequisites
# --------------------------------------------------------------------------

def check_tmux(fatal: bool = True) -> bool:
    path = shutil.which("tmux")
    if not path:
        msg = "tmux is missing — it is how the bot types into Claude Code."
        if fatal:
            die(msg, install_hint("tmux"), "then run this installer again")
        bad(msg)
        return False
    ok(f"tmux: {path}")
    return True


def check_claude(fatal: bool = True) -> bool:
    path = os.getenv("CCBOT_CLAUDE_BIN") or shutil.which("claude")
    if not path:
        msg = "the claude CLI is not on PATH."
        if fatal:
            die(
                msg,
                "Install Claude Code first: https://claude.com/product/claude-code",
                "Already installed? Point CCBOT_CLAUDE_BIN in .env at the binary.",
            )
        bad(msg)
        return False
    ok(f"claude: {path}")
    # Being on PATH is not the same as being usable: an unauthenticated CLI
    # fails here, and the bot would then show empty session lists forever.
    rc, out = run([path, "agents", "--json"], timeout=45)
    if rc != 0:
        warn("`claude agents --json` failed — is Claude Code signed in?")
        info(out.splitlines()[0] if out else "(no output)")
        return False
    ok("claude is signed in and answering")
    return True


def check_jq() -> bool:
    if shutil.which("jq"):
        ok("jq: {}".format(shutil.which("jq")))
        return True
    warn("jq is missing — only the exact-metrics status-line hook needs it.")
    info(install_hint("jq"))
    return False


def check_git() -> None:
    if shutil.which("git"):
        ok("git: {}".format(shutil.which("git")))
    else:
        warn("git is missing — /service will not be able to report the version.")


# --------------------------------------------------------------------------
# Virtualenv
# --------------------------------------------------------------------------

def venv_version() -> tuple | None:
    if not VENV_PY.exists():
        return None
    rc, out = run([str(VENV_PY), "-c",
                   "import sys;print('%d.%d' % sys.version_info[:2])"], timeout=30)
    if rc != 0:
        return None
    try:
        return tuple(int(x) for x in out.strip().split("."))
    except ValueError:
        return None


def create_venv() -> None:
    uv = shutil.which("uv")
    if uv:
        info("creating .venv with uv (it brings its own Python if needed)")
        rc, out = run([uv, "venv", "--python", "3.12", str(VENV)],
                      cwd=str(ROOT), timeout=600)
        if rc != 0:
            die("uv venv failed", out)
        return
    if sys.version_info < MIN_PY:
        die(
            f"this Python is {sys.version_info[0]}.{sys.version_info[1]}, "
            f"the bot needs {MIN_PY[0]}.{MIN_PY[1]}+.",
            "Easiest fix — install uv, which downloads a matching Python itself:",
            "    curl -LsSf https://astral.sh/uv/install.sh | sh",
            "then reopen the terminal and run this installer again.",
        )
    info("creating .venv with python -m venv")
    rc, out = run([sys.executable, "-m", "venv", str(VENV)], timeout=300)
    if rc != 0:
        die("python -m venv failed", out)


def install_deps(dev: bool) -> None:
    files = ["requirements.txt"] + (["requirements-dev.txt"] if dev else [])
    args: list = []
    for name in files:
        args += ["-r", name]
    uv = shutil.which("uv")
    if uv:
        cmd = [uv, "pip", "install", "--python", str(VENV_PY), *args]
    elif (VENV / "bin" / "pip").exists():
        cmd = [str(VENV / "bin" / "pip"), "install", "-q", *args]
    else:
        die(
            "the existing .venv has no pip and uv is not installed.",
            "That venv was made by uv. Either install uv:",
            "    curl -LsSf https://astral.sh/uv/install.sh | sh",
            "or delete .venv and run this installer again.",
        )
    info("installing dependencies ({})".format(", ".join(files)))
    rc, out = run(cmd, cwd=str(ROOT), timeout=900)
    if rc != 0:
        die("dependency install failed", *out.splitlines()[-5:])


def ensure_venv(dev: bool) -> None:
    head("Python environment")
    version = venv_version()
    if version and version >= MIN_PY:
        ok(f".venv exists (Python {version[0]}.{version[1]})")
    elif version:
        warn(f".venv has Python {version[0]}.{version[1]}, below the required "
             f"{MIN_PY[0]}.{MIN_PY[1]} — recreating")
        shutil.rmtree(VENV)
        create_venv()
    else:
        create_venv()
    install_deps(dev)
    rc, out = run([str(VENV_PY), "-c", "import aiogram, dotenv, PIL"],
                  cwd=str(ROOT), timeout=60)
    if rc != 0:
        die("the environment does not import cleanly", out)
    ok("dependencies installed and importable")


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def tg(token: str, method: str, http_timeout: int = 30, **params):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode() if params else None
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=http_timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "description": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        return {"ok": False, "description": f"network error: {e.reason}"}
    except Exception as e:  # timeouts, malformed JSON, anything else
        return {"ok": False, "description": str(e)}


def ask_token() -> tuple[str, str]:
    """Ask for a token and prove it works before writing it anywhere."""
    info("Create a bot with @BotFather in Telegram (/newbot) and copy its token.")
    while True:
        token = ask("Bot token").strip()
        if not token:
            continue
        me = tg(token, "getMe", http_timeout=20)
        if me.get("ok"):
            username = me["result"].get("username", "?")
            ok(f"token works — this is @{username}")
            return token, username
        bad("Telegram refused this token: {}".format(me.get("description", "unknown error")))
        if not confirm("Try another token?", True):
            raise SystemExit("cancelled")


def capture_user_id(token: str, username: str) -> tuple[int, str] | None:
    """Wait for the user to message the bot, and read their id off that message.

    Far kinder than sending someone to @userinfobot for a number they will
    mistype: it also proves they can actually reach this bot.
    """
    info(f"Open https://t.me/{username} and send the bot any message.")
    info("(Ctrl+C to type the numeric id by hand instead.)")
    # Drop whatever is already queued, so an old message cannot answer for them.
    seen = tg(token, "getUpdates", http_timeout=20, offset=-1, timeout=0)
    offset = 0
    if seen.get("ok") and seen.get("result"):
        offset = seen["result"][-1]["update_id"] + 1
    deadline = time.time() + 300
    try:
        while time.time() < deadline:
            r = tg(token, "getUpdates", http_timeout=40,
                   offset=offset, timeout=25, allowed_updates='["message"]')
            if not r.get("ok"):
                desc = r.get("description", "")
                if "terminated by other getUpdates" in desc or "Conflict" in desc:
                    bad("another copy of this bot is already polling Telegram.")
                    info(f"Stop it first: systemctl --user stop {UNIT}")
                    return None
                warn("Telegram: %s" % (desc or "no answer"))
                time.sleep(3)
                continue
            for upd in r.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message") or {}
                frm = msg.get("from") or {}
                if frm.get("id"):
                    name = frm.get("first_name") or frm.get("username") or "you"
                    ok("hello, {} — your Telegram id is {}".format(name, frm["id"]))
                    return int(frm["id"]), name
    except KeyboardInterrupt:
        print()
    return None


def ask_user_ids(token: str, username: str) -> str:
    got = capture_user_id(token, username)
    if got:
        first = str(got[0])
    else:
        info("Get your numeric id from @userinfobot in Telegram.")
        while True:
            first = ask("Your Telegram user id").strip()
            if first.isdigit():
                break
            bad("that is not a number")
    extra = ask("Other user ids allowed to use the bot, comma-separated", "")
    ids = [first] + [x.strip() for x in extra.replace(";", ",").split(",")]
    return ",".join(dict.fromkeys(x for x in ids if x.isdigit()))


# --------------------------------------------------------------------------
# .env
# --------------------------------------------------------------------------

def set_key(text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.M)
    if pattern.search(text):
        return pattern.sub(f"{key}={value}", text, count=1)
    return text.rstrip("\n") + f"\n{key}={value}\n"


def read_env() -> dict:
    """Parse .env loosely — enough to tell configured from not."""
    out: dict = {}
    try:
        for line in (ROOT / ".env").read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def configure_env() -> dict:
    head("Telegram")
    current = read_env()
    if current.get("TG_BOT_TOKEN") and current.get("TG_ALLOWED_USER_IDS"):
        me = tg(current["TG_BOT_TOKEN"], "getMe", http_timeout=20)
        who = "@{}".format(me["result"]["username"]) if me.get("ok") else "token REJECTED"
        ok(".env already configured ({}, ids: {})".format(who, current["TG_ALLOWED_USER_IDS"]))
        if me.get("ok") and not confirm("Replace it?", False):
            return current
    token, username = ask_token()
    ids = ask_user_ids(token, username)
    base = (ROOT / ".env").read_text() if (ROOT / ".env").exists() \
        else (ROOT / ".env.example").read_text()
    base = set_key(base, "TG_BOT_TOKEN", token)
    base = set_key(base, "TG_ALLOWED_USER_IDS", ids)
    (ROOT / ".env").write_text(base)
    os.chmod(ROOT / ".env", 0o600)
    ok("wrote .env (mode 600)")
    return read_env()


def host_zone() -> str:
    """The zone name the host clock is set to, as best we can tell."""
    try:
        link = os.readlink("/etc/localtime")
        if "zoneinfo/" in link:
            return link.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    try:
        return Path("/etc/timezone").read_text().strip()
    except OSError:
        return ""


def known_zone(name: str) -> bool:
    return (Path("/usr/share/zoneinfo") / name).exists()


def configure_timezone(env: dict) -> None:
    """Ask for a timezone when the host clock plainly is not the user's.

    Reset times ("resets tomorrow 09:00") are quoted on the host clock, and a
    WSL image is almost always on UTC while the person reading the message is
    not. Left alone, that is a wrong answer delivered with total confidence.
    """
    head("Timezone")
    if env.get("CCBOT_TZ"):
        ok(f"CCBOT_TZ={env['CCBOT_TZ']}")
        return
    zone = host_zone()
    if zone and zone not in ("Etc/UTC", "UTC", "Etc/Universal", "Universal"):
        ok(f"host clock is {zone} — rate-limit reset times will use it")
        return
    info("This host's clock is UTC, which is the default in WSL images and on")
    info("rented servers. Rate-limit reset times are quoted on it, so unless")
    info("you live in UTC they will be wrong by whole hours.")
    while True:
        name = ask("Your timezone (e.g. Europe/Kyiv), or Enter to keep UTC").strip()
        if not name:
            warn("keeping UTC — set CCBOT_TZ in .env later to change it")
            return
        if known_zone(name):
            break
        bad(f"{name} is not in /usr/share/zoneinfo")
    text = (ROOT / ".env").read_text()
    (ROOT / ".env").write_text(set_key(text, "CCBOT_TZ", name))
    env["CCBOT_TZ"] = name
    ok(f"CCBOT_TZ={name}")


# --------------------------------------------------------------------------
# Status-line hook
# --------------------------------------------------------------------------

def statusline_state() -> str:
    """'ours', 'other', 'none' or 'unreadable'."""
    if not CLAUDE_SETTINGS.exists():
        return "none"
    try:
        data = json.loads(CLAUDE_SETTINGS.read_text())
    except (OSError, ValueError):
        return "unreadable"
    if not isinstance(data, dict):
        return "unreadable"
    line = data.get("statusLine")
    command = line.get("command", "") if isinstance(line, dict) else ""
    if not isinstance(command, str):
        return "unreadable"
    if "statusline-tee.sh" in command or command == str(WRAPPER):
        return "ours"
    return "other" if command else "none"


def wire_statusline() -> None:
    head("Exact metrics (status-line hook)")
    info("Claude Code hands its status line a JSON payload with real context and")
    info("rate-limit numbers. Mirroring it lets the bot report exact figures")
    info("instead of scraping rounded bars off the screen. Optional.")
    state = statusline_state()
    if state == "ours":
        ok("already wired to bin/statusline-tee.sh")
        return
    if state == "unreadable":
        warn("~/.claude/settings.json is not valid JSON — not touching it.")
        return
    if not confirm("Wire it up?", True):
        info("skipped — the bot falls back to reading the screen")
        return
    TEE.chmod(0o755)
    CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if CLAUDE_SETTINGS.exists():
        raw = CLAUDE_SETTINGS.read_text()
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            warn("~/.claude/settings.json is not a JSON object — not touching it.")
            return
        data = parsed
        backup = CLAUDE_SETTINGS.with_suffix(".json.ccbot-backup")
        backup.write_text(raw)
        ok(f"backed up settings.json to {backup.name}")
    current = data.get("statusLine")
    line: dict = current if isinstance(current, dict) else {}
    existing = line.get("command", "")
    if not isinstance(existing, str):
        existing = ""
    if existing:
        # Keep whatever status line was already there by chaining it behind the
        # tee. The variable goes into a generated wrapper rather than straight
        # into statusLine.command, because nothing promises that setting is run
        # through a shell — and "VAR=x /path" means nothing without one.
        WRAPPER.parent.mkdir(parents=True, exist_ok=True)
        WRAPPER.write_text(
            "#!/bin/sh\n"
            "# Generated by the claude-tg-bot installer; redo with ./install.sh\n"
            "# Mirrors the status-line payload for the bot, then runs the status\n"
            "# line you already had, unchanged.\n"
            f"CCBOT_REAL_STATUSLINE={shlex.quote(existing)}"
            f" exec {shlex.quote(str(TEE))}\n"
        )
        WRAPPER.chmod(0o755)
        command = str(WRAPPER)
        ok(f"kept your existing status line, chained behind the tee ({WRAPPER})")
    else:
        command = str(TEE)
    line.update({"type": "command", "command": command})
    data["statusLine"] = line
    CLAUDE_SETTINGS.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    ok("statusLine.command now points at bin/statusline-tee.sh")
    info("It takes effect in sessions started from now on.")


# --------------------------------------------------------------------------
# Autostart
# --------------------------------------------------------------------------

def systemd_available() -> bool:
    if not shutil.which("systemctl"):
        return False
    rc, out = run(["systemctl", "--user", "is-system-running"], timeout=20)
    return not (rc == 127 or "Failed to connect" in out or out.strip() == "offline")


def unit_active() -> bool:
    _rc, out = run(["systemctl", "--user", "is-active", UNIT], timeout=20)
    return out.strip() == "active"


def stray_instance() -> tuple[int, str]:
    """Find a bot already polling: two of them tear each other's updates away.

    Matched by what the process *is*, not by where this checkout happens to
    live — a second copy cloned elsewhere conflicts over the same token just
    as thoroughly, and is exactly the case a path match would miss.
    """
    rc, out = run(["pgrep", "-af", "main.py"], timeout=20)
    if rc != 0:
        return 0, ""
    for line in out.splitlines():
        pid = line.split(" ", 1)[0]
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            cwd = Path(os.readlink(f"/proc/{pid}/cwd"))
        except OSError:
            continue
        if (cwd / "ccbot" / "bot.py").exists():
            return int(pid), line
    return 0, ""


def wsl_systemd_help() -> None:
    bad("systemd is not running in this WSL distribution.")
    info("Enable it once, from inside WSL:")
    info("    printf '[boot]\\nsystemd=true\\n' | sudo tee -a /etc/wsl.conf")
    info("then, in Windows PowerShell:  wsl --shutdown")
    info("Reopen the terminal and run this installer again.")


def tmux_supervisor(start: bool) -> None:
    """Fallback autostart: a restart loop in its own tmux session.

    Plain `python main.py &` would leave the bot dead after any crash and would
    turn /restart — which works by exiting and letting the supervisor return —
    into a way to kill it for good.
    """
    if not start:
        return
    run(["tmux", "kill-session", "-t", "ccbot-service"], timeout=20)
    loop = f"while true; do {shlex.quote(str(VENV_PY))} main.py; sleep 5; done"
    rc, out = run(["tmux", "new-session", "-d", "-s", "ccbot-service",
                   "-c", str(ROOT), "bash", "-lc", loop], timeout=30)
    if rc != 0:
        die("could not start the tmux supervisor", out)
    ok("started in tmux session 'ccbot-service' (attach: tmux attach -t ccbot-service)")
    warn("This does not survive a reboot, and /restart in chat is unavailable.")


def install_autostart() -> str:
    """Returns 'unit', 'tmux' or 'none'."""
    head("Autostart")
    pid, stray = stray_instance()
    if stray and not unit_active():
        warn("another instance is already polling Telegram:")
        info(stray)
        if confirm("Stop it? (two pollers cannot share one bot token)", True):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError as e:
                warn(f"could not stop it: {e}")
            time.sleep(3)
            if stray_instance()[0]:
                warn("it came back — something is supervising it.")
                info("If it is a systemd unit: systemctl --user stop <name>")
    if not systemd_available():
        if is_wsl():
            wsl_systemd_help()
        else:
            bad("systemd user session is unavailable.")
        if confirm("Run under a tmux supervisor instead?", True):
            tmux_supervisor(True)
            return "tmux"
        return "none"
    if not confirm("Install the systemd user unit and start the bot?", True):
        return "none"
    template = (ROOT / "contrib" / f"{UNIT}.service").read_text()
    UNIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    UNIT_FILE.write_text(template.replace("%INSTALL_DIR%", str(ROOT)))
    ok(f"wrote {UNIT_FILE}")
    run(["systemctl", "--user", "daemon-reload"], timeout=30)
    rc, out = run(["systemctl", "--user", "enable", "--now", UNIT], timeout=60)
    if rc != 0:
        die("systemctl enable --now failed", out)
    run(["systemctl", "--user", "restart", UNIT], timeout=60)
    time.sleep(3)
    if not unit_active():
        rc, out = run(["systemctl", "--user", "status", UNIT, "--no-pager"], timeout=30)
        die("the unit did not stay up", *out.splitlines()[-12:])
    ok(f"{UNIT} is active")
    check_linger()
    return "unit"


def check_linger() -> None:
    _rc, out = run(["loginctl", "show-user", os.getenv("USER") or "", "-p", "Linger"],
                  timeout=20)
    if "Linger=yes" in out:
        ok("linger is on — the bot runs with no login session open")
    else:
        warn("linger is off: the bot stops when your last session closes.")
        info("Turn it on once:  sudo loginctl enable-linger $USER")


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------

def log_tail(n: int = 40) -> list[str]:
    try:
        return LOG_FILE.read_text(errors="replace").splitlines()[-n:]
    except OSError:
        return []


def log_since(started: float, limit: int = 400) -> list[str]:
    """Log lines written after `started`.

    The log lives under XDG and is therefore shared by every checkout on the
    machine. An unfiltered tail would happily report a months-old crash from
    somebody else's copy as this install's problem.
    """
    out: list[str] = []
    keeping = False
    for line in log_tail(limit):
        try:
            when = time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            if keeping:  # traceback continuation lines carry no timestamp
                out.append(line)
            continue
        keeping = when >= started - 1
        if keeping:
            out.append(line)
    return out


def restart_count() -> int:
    out = run(["systemctl", "--user", "show", UNIT, "-p", "NRestarts"], timeout=20)[1]
    try:
        return int(out.split("=", 1)[1])
    except (IndexError, ValueError):
        return 0


def verify(env: dict, how: str, started: float) -> None:
    head("Verification")
    if how == "none":
        warn("the bot was not started, so there is nothing to verify yet")
        return
    # "active" a second after starting proves very little: a bot that dies on
    # a bad token comes straight back up and looks exactly the same. What it
    # cannot fake is a restart counter that keeps climbing.
    time.sleep(6)
    if how == "unit":
        if restart_count() > 0:
            bad("the unit is crash-looping — it has already restarted itself.")
            for line in log_since(started, 60):
                if " ERROR " in line or "Traceback" in line or "Error" in line:
                    info(line[:160])
            info(f"Full picture: journalctl --user -u {UNIT} -n 50 --no-pager")
        else:
            ok("still up after six seconds, no restarts")
    lines = log_since(started)
    conflicts = [x for x in lines if "TelegramConflictError" in x]
    errors = [x for x in lines if " ERROR " in x or "Traceback" in x]
    if conflicts:
        bad("TelegramConflictError in the log — two pollers share this token.")
        info("Find the other one:  pgrep -af main.py")
    elif errors:
        warn("errors since the bot started:")
        for line in errors[-3:]:
            info(line[:160])
        info(f"Full log: {LOG_FILE}")
    elif lines:
        ok(f"started cleanly, nothing in the log ({LOG_FILE})")
    token = env.get("TG_BOT_TOKEN", "")
    owner = (env.get("TG_ALLOWED_USER_IDS", "").split(",") or [""])[0]
    if not (token and owner):
        return
    # The last link in the chain is the one the terminal cannot show: a real
    # message, delivered to the real phone.
    r = tg(token, "sendMessage", http_timeout=25, chat_id=owner,
           text="ccbot is installed and running. Send /help to see what it can do.")
    if r.get("ok"):
        ok("sent a confirmation message to your Telegram — check your phone")
    else:
        warn("could not send the confirmation message: {}".format(r.get("description", "unknown")))
        info("Have you pressed Start in the chat with the bot?")


def final_card(how: str, username: str) -> None:
    running = {
        "unit": f"running as the systemd user unit '{UNIT}'",
        "tmux": "running under a tmux supervisor (session 'ccbot-service')",
        "none": "installed, but NOT started",
    }[how]
    local = {
        "unit": f"systemctl --user restart {UNIT}    restart it",
        "tmux": "tmux attach -t ccbot-service              attach to it",
        "none": ".venv/bin/python main.py                  start it",
    }[how]
    head("Done")
    print(f"""
  The bot is {running}.

  Next, on your phone:
    1. open https://t.me/{username} and press Start
    2. /new       start a Claude session in any directory
    3. /sessions  see what is running
    4. /help      everything else

  On this machine:
    {local}
    tail -f {LOG_FILE}
    ./install.sh --doctor                     re-check the install any time
""")


# --------------------------------------------------------------------------
# Doctor
# --------------------------------------------------------------------------

def doctor() -> int:
    print(_c("1", "claude-tg-bot — install check"))
    problems = 0

    head("Platform")
    ok("WSL" if is_wsl() else "Linux")

    head("Prerequisites")
    problems += 0 if check_tmux(fatal=False) else 1
    problems += 0 if check_claude(fatal=False) else 1
    check_jq()
    check_git()

    head("Python environment")
    version = venv_version()
    if not version:
        bad("no working .venv — run ./install.sh")
        problems += 1
    elif version < MIN_PY:
        bad(f".venv has Python {version[0]}.{version[1]}, "
            f"below {MIN_PY[0]}.{MIN_PY[1]}")
        problems += 1
    else:
        ok(f".venv: Python {version[0]}.{version[1]}")
        rc, out = run([str(VENV_PY), "-c", "import main, ccbot.bot"],
                      cwd=str(ROOT), timeout=60)
        if rc != 0:
            bad("the package does not import: {}".format((out.splitlines()[-1:] or [""])[0]))
            problems += 1
        else:
            ok("package imports cleanly")

    head("Telegram")
    env = read_env()
    if not env.get("TG_BOT_TOKEN"):
        bad("TG_BOT_TOKEN is not set in .env")
        problems += 1
    else:
        me = tg(env["TG_BOT_TOKEN"], "getMe", http_timeout=20)
        if me.get("ok"):
            ok("token valid: @{}".format(me["result"].get("username")))
        else:
            bad("token rejected: {}".format(me.get("description")))
            problems += 1
    if env.get("TG_ALLOWED_USER_IDS"):
        ok("allowed ids: {}".format(env["TG_ALLOWED_USER_IDS"]))
    else:
        bad("TG_ALLOWED_USER_IDS is empty — the bot refuses to start")
        problems += 1
    mode = oct((ROOT / ".env").stat().st_mode & 0o777) if (ROOT / ".env").exists() else ""
    if mode and mode != "0o600":
        warn(f".env is {mode} — it holds a bot token; chmod 600 .env")

    head("Timezone")
    if env.get("CCBOT_TZ"):
        if known_zone(env["CCBOT_TZ"]):
            ok(f"CCBOT_TZ={env['CCBOT_TZ']}")
        else:
            bad(f"CCBOT_TZ={env['CCBOT_TZ']} is not a known zone — times fall back to UTC")
            problems += 1
    else:
        zone = host_zone() or "unknown"
        if zone in ("Etc/UTC", "UTC", "Etc/Universal", "Universal", "unknown"):
            warn(f"no CCBOT_TZ and the host clock is {zone}")
            info("Reset times will be quoted in UTC. Set CCBOT_TZ in .env.")
        else:
            ok(f"host clock is {zone}")

    head("Exact metrics")
    state = statusline_state()
    if state == "ours":
        ok("status-line hook is wired")
    elif state == "other":
        warn("another status line is configured — metrics come from the screen")
    else:
        warn("status-line hook is not wired — metrics come from the screen")

    head("Service")
    if not systemd_available():
        warn("no systemd user session")
        rc, out = run(["tmux", "has-session", "-t", "ccbot-service"], timeout=20)
        if rc == 0:
            ok("running under the tmux supervisor")
        else:
            bad("the bot does not appear to be running")
            problems += 1
    elif unit_active():
        ok(f"{UNIT} is active")
        check_linger()
    else:
        bad(f"{UNIT} is not active")
        info(f"systemctl --user status {UNIT} --no-pager")
        problems += 1
    stray = stray_instance()[1]
    if stray and not unit_active():
        warn(f"an unmanaged instance is running: {stray}")

    head("Log")
    lines = log_tail()
    if not lines:
        warn(f"no log at {LOG_FILE} yet")
    else:
        errors = [x for x in lines if " ERROR " in x or "Traceback" in x]
        if errors:
            bad("recent errors:")
            for line in errors[-5:]:
                info(line[:160])
            problems += 1
        else:
            ok(f"no errors in the last {len(lines)} lines")

    print()
    if problems:
        print(_c("31", f"  {problems} problem(s) found."))
    else:
        print(_c("32", "  Everything checks out."))
    return 1 if problems else 0


# --------------------------------------------------------------------------

def install() -> int:
    print(_c("1", "claude-tg-bot — installer"))
    print(f"  Repository: {ROOT}")
    check_platform()

    head("Prerequisites")
    check_tmux()
    check_claude()
    check_jq()
    check_git()

    dev = confirm("Install developer tools too (ruff, basedpyright, Babel)?", False)
    ensure_venv(dev)
    if dev and shutil.which("git"):
        run(["git", "config", "core.hooksPath", "bin/githooks"], cwd=str(ROOT))
        ok("pre-commit hook enabled (bin/check.sh)")

    env = configure_env()
    configure_timezone(env)
    wire_statusline()
    started = time.time()
    how = install_autostart()
    verify(env, how, started)

    me = tg(env.get("TG_BOT_TOKEN", ""), "getMe", http_timeout=20)
    username = me["result"].get("username", "your_bot") if me.get("ok") else "your_bot"
    final_card(how, username)
    return 0


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if args and args[0] == "--doctor":
        return doctor()
    if args:
        sys.stderr.write(f"unknown argument: {args[0]} (try --help)\n")
        return 2
    return install()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        raise SystemExit(130) from None
