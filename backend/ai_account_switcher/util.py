"""Small filesystem, process and formatting helpers shared by every module."""

from __future__ import annotations

import base64
import contextlib
import fcntl
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

from ai_account_switcher import APP_ID

EMAIL_RE = re.compile(r"^[^@\s'\"\\]+@[^@\s'\"\\]+\.[^@\s'\"\\]+$")


class SwitcherError(RuntimeError):
    """An error that is safe and meaningful to show to the user."""


# --------------------------------------------------------------------------- paths


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / fallback


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / APP_ID


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / APP_ID


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / APP_ID


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isabs(base) and os.path.isdir(base):
        return Path(base) / APP_ID
    return cache_dir() / "run"


# --------------------------------------------------------------------------- files


def ensure_private_dir(path: Path) -> Path:
    """Create one of *our* directories with 0700 permissions."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
    return path


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def read_json(path: Path) -> dict | None:
    text = read_text(path)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def loads_dict(text: str | None) -> dict | None:
    if not text:
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Write a file atomically (temp file + rename) with the given permissions.

    A symlinked target is followed, so dotfile managers that symlink
    ``~/.claude.json`` keep their link.
    """
    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def file_lock(name: str, timeout: float = 30.0) -> Iterator[None]:
    """Process-wide exclusive lock so concurrent front-end calls never interleave."""
    lock_path = ensure_private_dir(cache_dir()) / f"{name}.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise SwitcherError("Another account operation is still running; try again.")
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- processes

_EXTRA_BIN_DIRS = (
    "~/.local/bin",
    "~/bin",
    "~/.npm-global/bin",
    "~/.bun/bin",
    "~/.volta/bin",
    "~/.cargo/bin",
    "~/.nix-profile/bin",
    "/home/linuxbrew/.linuxbrew/bin",
    "~/.linuxbrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/snap/bin",
)


def find_program(name: str, override: str = "") -> str | None:
    """Find a CLI even when launched from a desktop session with a minimal PATH."""
    if override:
        candidate = os.path.expanduser(override)
        if os.path.isabs(candidate):
            return candidate if os.access(candidate, os.X_OK) else None
        return shutil.which(candidate)
    found = shutil.which(name)
    if found:
        return found
    dirs = [os.path.expanduser(d) for d in _EXTRA_BIN_DIRS]
    dirs += sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin")), reverse=True)
    for directory in dirs:
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def program_env(program: str) -> dict[str, str]:
    """Environment for running ``program``: its own directory first on PATH
    (so ``#!/usr/bin/env node`` style launchers find their interpreter)."""
    env = dict(os.environ)
    directory = os.path.dirname(program)
    path = env.get("PATH", "/usr/bin:/bin")
    if directory and directory not in path.split(os.pathsep):
        env["PATH"] = directory + os.pathsep + path
    return env


def run(
    argv: Sequence[str],
    timeout: float,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    cwd: str | os.PathLike | None = None,
) -> subprocess.CompletedProcess | None:
    """Run a command without a shell. Returns None on timeout or exec failure."""
    try:
        return subprocess.run(
            list(argv),
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=cwd,
            stdin=None if input_text is not None else subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


def processes_named(name: str) -> list[int]:
    """PIDs whose executable (or launched script) is called ``name``."""
    pids = []
    me = os.getpid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == me:
            continue
        try:
            raw = Path(f"/proc/{entry}/cmdline").read_bytes()
        except OSError:
            continue
        argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        if not argv:
            continue
        if os.path.basename(argv[0]) == name:
            pids.append(int(entry))
        elif len(argv) > 1 and os.path.basename(argv[0]) in {"node", "bun"} and (
            os.path.basename(argv[1]) == name or f"/{name}/" in argv[1]
        ):
            pids.append(int(entry))
    return pids


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --------------------------------------------------------------------------- tokens & time


def decode_jwt_payload(token: str) -> dict | None:
    """Decode a JWT payload *without* verifying it (only used to read claims we already own)."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def valid_email(email: object) -> bool:
    return isinstance(email, str) and len(email) <= 254 and bool(EMAIL_RE.match(email))


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds <= 0:
        return "now"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{max(minutes, 1)}m"


def window_label(seconds: object, fallback: str) -> str:
    """Turn a rate-limit window length in seconds into ``5h`` / ``7d``."""
    try:
        secs = int(seconds)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    if secs <= 0:
        return fallback
    if secs % 86400 == 0:
        return f"{secs // 86400}d"
    if secs % 3600 == 0:
        return f"{secs // 3600}h"
    return f"{max(secs // 60, 1)}m"
