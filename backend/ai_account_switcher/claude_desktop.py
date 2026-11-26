"""Claude Desktop profiles: one Electron ``--user-data-dir`` per account.

Claude Desktop (e.g. the ``claude-desktop`` Linux build) encrypts its session
with a key kept in the keyring and tied to its data directory, so sessions can't
be copied between directories. Instead every profile keeps a *permanent*
directory, and switching means quitting Claude Desktop and relaunching it on the
other directory. The layout matches Adam-Zaghloul/claude-desktop-switcher, so
existing profiles there show up here:

    ~/.config/Claude/                     "default" (Claude Desktop's own directory)
    ~/.config/Claude-accounts/<name>/     one directory per extra profile
    ~/.config/Claude-accounts/.current    name of the last selected profile

There is no email or usage to show (the session is encrypted); profiles are
named by the user.
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import time
from pathlib import Path

from ai_account_switcher.config import Settings
from ai_account_switcher.util import (
    SwitcherError,
    atomic_write_text,
    find_program,
    program_env,
    read_text,
    run,
)

PROVIDER = "desktop"
LABEL = "Claude Desktop"
DEFAULT = "default"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}$")
STOP_TIMEOUT = 10.0


def _config_home() -> Path:
    value = os.environ.get("XDG_CONFIG_HOME")
    return Path(value) if value and os.path.isabs(value) else Path.home() / ".config"


def profiles_root() -> Path:
    return _config_home() / "Claude-accounts"


def default_dir() -> Path:
    return _config_home() / "Claude"


def profile_dir(name: str) -> Path:
    return default_dir() if name == DEFAULT else profiles_root() / name


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name)) and name not in {".", ".."}


def command(settings: Settings) -> list[str] | None:
    """The Claude Desktop launch command (``desktop_command`` may hold arguments,
    e.g. ``flatpak run <app-id>``)."""
    if settings.desktop_command:
        argv = shlex.split(settings.desktop_command)
        program = find_program(argv[0]) if argv else None
        return [program, *argv[1:]] if program else None
    program = find_program("claude-desktop")
    return [program] if program else None


def list_profiles() -> list[str]:
    names = []
    if default_dir().is_dir():
        names.append(DEFAULT)
    try:
        entries = sorted(profiles_root().iterdir(), key=lambda p: p.name.lower())
    except OSError:
        entries = []
    names += [p.name for p in entries if p.is_dir() and valid_name(p.name) and p.name != DEFAULT]
    return names


def selected_profile() -> str | None:
    name = (read_text(profiles_root() / ".current") or "").strip()
    return name if name and valid_name(name) and profile_dir(name).is_dir() else None


def _set_selected(name: str | None) -> None:
    profiles_root().mkdir(parents=True, exist_ok=True)
    atomic_write_text(profiles_root() / ".current", (name or "(none)") + "\n", 0o600)


# --------------------------------------------------------------------------- processes


def _is_claude_desktop_arg(arg: str) -> bool:
    return any(
        part == "claude-desktop" or part.startswith("claude-desktop-") or part.startswith(".mount_claude")
        for part in arg.split("/")
    )


def _candidate_pids() -> list[int]:
    me, parent = os.getpid(), os.getppid()
    return [int(e) for e in os.listdir("/proc") if e.isdigit() and int(e) not in (me, parent)]


def _cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]


def _profile_of(argv: list[str]) -> str:
    data_dir = None
    for index, arg in enumerate(argv):
        if arg.startswith("--user-data-dir="):
            data_dir = arg.split("=", 1)[1]
        elif arg == "--user-data-dir" and index + 1 < len(argv):
            data_dir = argv[index + 1]
    if not data_dir:
        return DEFAULT
    path = Path(os.path.expanduser(data_dir))
    if path == default_dir():
        return DEFAULT
    if path.parent == profiles_root():
        return path.name
    return "(other)"


def running() -> dict[str, list[int]]:
    """Running Claude Desktop processes, grouped by profile name."""
    found: dict[str, list[int]] = {}
    for pid in _candidate_pids():
        argv = _cmdline(pid)
        if not argv or any("ai_account_switcher" in a or "ai-account-switcher" in a for a in argv):
            continue
        if not any(_is_claude_desktop_arg(a) for a in argv[:3]):
            continue
        found.setdefault(_profile_of(argv), []).append(pid)
    return found


def _stop_all() -> None:
    pids = [pid for group in running().values() for pid in group]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        if not running():
            return
        time.sleep(0.25)
    for group in running().values():
        for pid in group:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    time.sleep(0.5)


def _launch(settings: Settings, name: str) -> None:
    argv = command(settings)
    if not argv:
        raise SwitcherError("Claude Desktop (claude-desktop) was not found. "
                            "Set its command with: ai-account-switcher set desktop_command '<command>'")
    if name != DEFAULT:
        argv = [*argv, f"--user-data-dir={profile_dir(name)}"]
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, env=program_env(argv[0]))
    except OSError as exc:
        raise SwitcherError(f"Could not start Claude Desktop: {exc}") from exc


# --------------------------------------------------------------------------- actions


def switch(settings: Settings, name: str) -> dict:
    """Open Claude Desktop on ``name``, quitting it first if another profile is open."""
    if not valid_name(name) or not profile_dir(name).is_dir():
        raise SwitcherError(f"No Claude Desktop profile named {name!r}.")
    procs = running()
    if name in procs and len(procs) == 1:
        _set_selected(name)
        return {"changed": False, "account": name}
    if procs:
        _stop_all()
    _set_selected(name)
    _launch(settings, name)
    return {"changed": True, "account": name, "restarted": bool(procs)}


def add(settings: Settings, name: str) -> dict:
    """Create a profile and open Claude Desktop on it so the user can sign in."""
    name = name.strip()
    if not valid_name(name) or name == DEFAULT:
        raise SwitcherError("Profile names use letters, numbers and . _ @ + - (up to 64), "
                            "e.g. work or you@example.com.")
    if not command(settings):
        raise SwitcherError("Claude Desktop (claude-desktop) was not found.")
    path = profile_dir(name)
    existed = path.is_dir()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = switch(settings, name)
    result["created"] = not existed
    return result


def remove(name: str) -> str:
    """Move a profile's directory to the trash. Returns where it went."""
    if name == DEFAULT:
        raise SwitcherError("The default profile is Claude Desktop's own folder and can't be removed here.")
    path = profile_dir(name)
    if not valid_name(name) or not path.is_dir():
        raise SwitcherError(f"No Claude Desktop profile named {name!r}.")
    if name in running():
        raise SwitcherError(f"Claude Desktop is open on {name}. Switch to another profile first.")
    for argv in (["gio", "trash", str(path)], ["kioclient6", "move", str(path), "trash:/"],
                 ["kioclient", "move", str(path), "trash:/"]):
        program = find_program(argv[0])
        if not program:
            continue
        result = run([program, *argv[1:]], timeout=60)
        if result is not None and result.returncode == 0 and not path.exists():
            if selected_profile() is None or (read_text(profiles_root() / ".current") or "").strip() == name:
                _set_selected(None)
            return "trash"
    raise SwitcherError(f"Could not move {path} to the trash (needs gio or kioclient). Delete it yourself if you want it gone.")


def status_entry(settings: Settings) -> dict | None:
    argv = command(settings)
    profiles = list_profiles()
    if not argv and not profiles:
        return None
    procs = running()
    open_profiles = [n for n in profiles if n in procs]
    active = open_profiles[0] if len(open_profiles) == 1 else selected_profile()
    accounts = []
    for name in profiles:
        is_running = name in procs
        text = "Open" if is_running else "Not open"
        accounts.append({
            "id": name, "email": name, "plan": "Claude Desktop's own folder" if name == DEFAULT else "",
            "org": "", "active": name == active, "has_credentials": True,
            "running": is_running, "launchable": not is_running,
            "removable": name != DEFAULT and not is_running,
            "usage": {"available": False, "text": text, "message": text, "code": "desktop",
                      "windows": [], "max_percent": None, "age_seconds": None},
        })
    return {
        "id": PROVIDER, "label": LABEL, "cli_found": argv is not None, "manageable": True,
        "add_needs_name": True,
        "remove_hint": "Its folder is moved to the trash, so you can restore it. Your Claude account is not affected.",
        "active": active, "unsaved_login": None, "auto_switch": False,
        "notice": None if argv else ("Claude Desktop (claude-desktop) not found. If it is installed elsewhere: "
                                     "ai-account-switcher set desktop_command '<command>'"),
        "pending": None, "accounts": accounts,
    }
