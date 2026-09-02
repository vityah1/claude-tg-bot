"""The laptop's Windows power mode, switched from inside WSL.

The bot lives in WSL, and WSL2 is a virtual machine: the embedded controller
that drives the fans and the power policy that caps the CPU both belong to
Windows. The one bridge across is interop — the binfmt_misc handler that lets a
Linux process exec a Windows binary — so everything here is `powercfg.exe` and
`reg.exe` run on the host and read back from it.

What is switched is the Windows 11 **power mode overlay** (the slider in
Settings: best power efficiency / balanced / best performance), and only that.
It is the one lever that needs no elevation, and that was measured rather than
assumed: `/overlaysetactive` returns 0 under the plain, unelevated token
interop hands out. A custom scheme with its own PROCTHROTTLEMAX, or the
vendor's own fan curve, both need an administrator — a decision to take
deliberately, not a privilege to acquire quietly behind a chat button.

So this is **not** the vendor's Quiet/Performance mode. It moves the energy
preference and the boost policy; that moves temperature; the fan curve in the
EC follows temperature. It gets quieter by a longer route, and the fans are
never commanded directly.

Two traps, both already sprung once here:

* the systemd user unit's PATH has no Windows entries at all, so
  `shutil.which("powercfg.exe")` finds nothing under the service even though it
  works in a login shell. The path is resolved from /proc/mounts instead.
* the overlay is stored **per power source**, and `/overlaysetactive` writes
  only the one in effect. Which source that is comes from
  /sys/class/power_supply, which WSL does pass through — so it is answered on
  the Linux side, without another trip to the host.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .i18n import N_

log = logging.getLogger("ccbot.winpower")

# The overlay GUIDs `powercfg /overlaysetactive` accepts. The all-zero one is
# Windows' own "Balanced (recommended)" — a real value, not a placeholder.
MODES: tuple[tuple[str, str, str], ...] = (
    ("quiet", "961cc777-2547-4f9d-8174-7d86181b8a7a", N_("🍃 Quiet")),
    ("balanced", "00000000-0000-0000-0000-000000000000", N_("⚖️ Balanced")),
    ("turbo", "ded574b5-45a0-4f42-8737-46345c09c238", N_("🚀 Performance")),
)
LABEL = {key: label for key, _guid, label in MODES}
_GUID = {key: guid for key, guid, _label in MODES}
_KEY_OF = {guid: key for key, guid, _label in MODES}

_SCHEMES = r"HKLM\SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes"
_AC_VALUE = "ActiveOverlayAcPowerScheme"
_DC_VALUE = "ActiveOverlayDcPowerScheme"
# Interop spawns a Windows process; measured at ~40-70 ms, but a host under
# load can take much longer, and a hung call must not wedge a chat command.
_TIMEOUT = 15.0
_SUPPLIES = Path("/sys/class/power_supply")
# Present only when interop is on. Without it, exec of a .exe fails with ENOEXEC
# rather than anything a caller could act on, so it is checked up front.
_BINFMT = Path("/proc/sys/fs/binfmt_misc/WSLInterop")

_OCTAL = re.compile(r"\\([0-7]{3})")
# Resolved once: mounts do not move under a running process.
_sys32: Path | None = None
_looked = False


@dataclass(frozen=True, slots=True)
class Power:
    """What Windows reports about its power mode right now.

    Both overlays are carried, not just the live one: on a laptop they differ
    (Windows ships "best performance" on battery here), and a card that showed
    only one would make the other look like it had been changed too.
    """

    ac: str | None          # mode key of the mains overlay, None if unrecognised
    dc: str | None          # mode key of the battery overlay
    on_mains: bool | None   # None when the power source cannot be read
    battery_pct: int | None

    @property
    def active(self) -> str | None:
        """The overlay actually in force — the one a button press would move."""
        if self.on_mains is None:
            return None
        return self.ac if self.on_mains else self.dc


def _unescape(field: str) -> str:
    """/proc/mounts writes spaces and backslashes as octal escapes."""
    return _OCTAL.sub(lambda m: chr(int(m.group(1), 8)), field)


def _find_system32() -> Path | None:
    """Windows' System32, as this distro sees it.

    Found by mount rather than by name: /mnt/c is only the default, a distro
    configured with `root = /` puts the drive at /c, and Windows need not be
    on C: at all.
    """
    try:
        mounts = Path("/proc/mounts").read_text(encoding="utf-8", errors="replace")
    except OSError:
        log.debug("no /proc/mounts — not a Linux this code understands")
        return None
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[2] not in {"9p", "drvfs", "virtiofs"}:
            continue
        cand = Path(_unescape(parts[1])) / "Windows" / "System32"
        if (cand / "powercfg.exe").is_file():
            return cand
    return None


def _exe(name: str) -> str | None:
    global _sys32, _looked
    if not _looked:
        _sys32, _looked = _find_system32(), True
        log.info("Windows System32: %s", _sys32 or "not found")
    if _sys32 is None:
        return None
    path = _sys32 / name
    return str(path) if path.is_file() else None


def available() -> bool:
    """Whether Windows can be reached from here at all.

    Both halves matter: the mount says Windows is visible, the binfmt handler
    says its binaries can actually be run.
    """
    return _exe("powercfg.exe") is not None and _BINFMT.exists()


def _run(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, errors="replace",
                              timeout=_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("%s failed: %s", Path(args[0]).name, exc)
        return None


def _supplies() -> tuple[bool | None, int | None]:
    """Power source and battery charge, read on the Linux side.

    WSL passes the ACPI adapter and battery through to /sys, so neither of
    these costs a trip to the host.
    """
    on_mains: bool | None = None
    pct: int | None = None
    try:
        entries = sorted(_SUPPLIES.iterdir())
    except OSError:
        return None, None
    for dev in entries:
        try:
            kind = (dev / "type").read_text(encoding="utf-8").strip()
            if kind == "Mains" and on_mains is None:
                on_mains = (dev / "online").read_text(encoding="utf-8").strip() == "1"
            elif kind == "Battery" and pct is None:
                pct = int((dev / "capacity").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
    return on_mains, pct


def read() -> Power | None:
    """Both overlays and the live power source, or None if Windows is out of reach.

    Blocking: a subprocess across the interop boundary. Call it off the event
    loop (`asyncio.to_thread`).
    """
    exe = _exe("reg.exe")
    if exe is None:
        return None
    done = _run([exe, "query", _SCHEMES])
    if done is None or done.returncode != 0:
        log.warning("reg query failed: rc=%s", done.returncode if done else "—")
        return None
    # `reg query` prints "    <name>    REG_SZ    <value>" — three fields once
    # split, which is also what tells the value lines from the sub-key lines.
    guids: dict[str, str] = {}
    for line in done.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] == "REG_SZ":
            guids[parts[0]] = parts[2].lower()
    on_mains, pct = _supplies()
    return Power(
        ac=_KEY_OF.get(guids.get(_AC_VALUE, "")),
        dc=_KEY_OF.get(guids.get(_DC_VALUE, "")),
        on_mains=on_mains,
        battery_pct=pct,
    )


def apply(key: str) -> bool:
    """Switch the overlay for the power source in effect.

    Blocking, like `read()`.
    """
    guid = _GUID.get(key)
    exe = _exe("powercfg.exe")
    if guid is None or exe is None:
        log.warning("cannot set power mode %r (guid=%s exe=%s)", key, guid, exe)
        return False
    done = _run([exe, "/overlaysetactive", guid])
    if done is None or done.returncode != 0:
        log.warning("powercfg /overlaysetactive %s: rc=%s %s", key,
                    done.returncode if done else "—",
                    (done.stdout + done.stderr).strip() if done else "")
        return False
    log.info("power overlay set to %s", key)
    return True
