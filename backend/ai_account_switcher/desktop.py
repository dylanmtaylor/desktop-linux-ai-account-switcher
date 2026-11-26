"""Desktop integration: open a terminal for interactive logins, send notifications."""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from ai_account_switcher import APP_NAME
from ai_account_switcher.util import (
    SwitcherError,
    ensure_private_dir,
    find_program,
    program_env,
    read_text,
    run,
    runtime_dir,
)

# (program, args placed before the command)
_TERMINALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("xdg-terminal-exec", ()),
    ("ptyxis", ("--new-window", "--")),
    ("kgx", ("--",)),
    ("gnome-terminal", ("--wait", "--")),
    ("konsole", ("-e",)),
    ("xfce4-terminal", ("-x",)),
    ("tilix", ("-e",)),
    ("alacritty", ("-e",)),
    ("kitty", ()),
    ("foot", ()),
    ("wezterm", ("start", "--")),
    ("x-terminal-emulator", ("-e",)),
    ("xterm", ("-e",)),
)


def _terminal_argv(configured: str) -> list[str]:
    if configured:
        argv = shlex.split(configured)
        if argv and find_program(argv[0]):
            return [find_program(argv[0]) or argv[0], *argv[1:]]
        raise SwitcherError(f"Configured terminal {configured!r} was not found.")
    for program, prefix in _TERMINALS:
        path = find_program(program)
        if path:
            return [path, *prefix]
    raise SwitcherError("No terminal emulator found. Set one with: ai-account-switcher set terminal konsole -e")


def run_login_in_terminal(
    title: str,
    command: Sequence[str],
    terminal: str,
    done: Callable[[], bool],
    timeout: float = 300,
) -> bool:
    """Run an interactive login command in a visible terminal window.

    Returns True once ``done()`` reports success. Returns False if the command
    exits without success or the timeout passes.
    """
    workdir = ensure_private_dir(runtime_dir())
    stamp = f"{os.getpid()}-{int(time.time())}"
    status_file = workdir / f"login-{stamp}.status"
    script = workdir / f"login-{stamp}.sh"
    quoted = " ".join(shlex.quote(part) for part in command)
    script.write_text(
        "#!/bin/sh\n"
        f"printf '\\033]0;%s\\007' {shlex.quote(title)}\n"
        f"echo {shlex.quote(APP_NAME + ' - ' + title)}\n"
        "echo\n"
        "echo 'Finish signing in (a browser window may open).'\n"
        "echo\n"
        f"{quoted}\n"
        "status=$?\n"
        f"echo \"$status\" > {shlex.quote(str(status_file))}\n"
        "echo\n"
        "if [ \"$status\" -eq 0 ]; then echo 'Signed in. This window closes in a few seconds.'; sleep 3;\n"
        "else echo 'Login failed or was cancelled. Press Enter to close.'; read _; fi\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    try:
        subprocess.Popen(
            [*_terminal_argv(terminal), "/bin/sh", str(script)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=program_env(command[0]),
        )
    except OSError as exc:
        raise SwitcherError(f"Could not open a terminal: {exc}") from exc

    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            exit_text = read_text(status_file)
            if exit_text is not None:
                # The command finished. Give the CLI a moment to flush its files.
                for _ in range(10):
                    if done():
                        return True
                    time.sleep(0.5)
                return False
            if done():
                time.sleep(1.5)  # let the CLI finish writing companion files
                return True
            time.sleep(1)
        return False
    finally:
        for path in (status_file, script):
            try:
                Path(path).unlink()
            except OSError:
                pass


GNOME_BUS_PATH = "/io/github/dylanmtaylor/AiAccountSwitcher"
GNOME_BUS_IFACE = "io.github.dylanmtaylor.AiAccountSwitcher"
PLASMOID_ID = "io.github.dylanmtaylor.aiaccountswitcher"


def open_ui(summary_text: str) -> str:
    """Bring up the switcher UI from an app launcher. Returns what was opened."""
    gdbus = find_program("gdbus")
    if gdbus:
        result = run([gdbus, "call", "--session", "--dest", "org.gnome.Shell", "--object-path",
                      GNOME_BUS_PATH, "--method", f"{GNOME_BUS_IFACE}.Open"], timeout=5)
        if result is not None and result.returncode == 0:
            return "gnome"
    windowed = find_program("plasmawindowed")
    if windowed and _plasmoid_installed():
        subprocess.Popen([windowed, PLASMOID_ID], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return "plasma"
    notify(APP_NAME, summary_text)
    return "notification"


def _plasmoid_installed() -> bool:
    data_dirs = [os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")]
    data_dirs += (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    return any((Path(d) / "plasma/plasmoids" / PLASMOID_ID / "metadata.json").exists() for d in data_dirs if d)


DESKTOP_ENTRY = "io.github.dylanmtaylor.AiAccountSwitcher"


def _icon() -> str:
    """Absolute path of the app icon when installed (theme lookups by name are
    unreliable right after install), else the theme name."""
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    candidates = [
        Path(data_home) / "icons/hicolor/scalable/apps/ai-account-switcher.svg",
        Path(__file__).resolve().parents[2] / "data/ai-account-switcher.svg",  # source checkout
    ]
    candidates += [Path(d) / "icons/hicolor/scalable/apps/ai-account-switcher.svg"
                   for d in (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if d]
    return next((str(p) for p in candidates if p.is_file()), "ai-account-switcher")


def notify(summary: str, body: str = "", urgent: bool = False) -> None:
    """Best-effort desktop notification (GNOME and Plasma both implement the spec)."""
    tool = find_program("notify-send")
    icon = _icon()
    if tool:
        argv = [tool, "--app-name", APP_NAME, "--icon", icon,
                f"--hint=string:desktop-entry:{DESKTOP_ENTRY}", f"--hint=string:image-path:{icon}"]
        if urgent:
            argv += ["--urgency", "critical"]
        run([*argv, summary, body], timeout=5)
        return
    gdbus = find_program("gdbus")
    if gdbus:
        run(
            [gdbus, "call", "--session", "--dest", "org.freedesktop.Notifications",
             "--object-path", "/org/freedesktop/Notifications",
             "--method", "org.freedesktop.Notifications.Notify",
             APP_NAME, "0", icon, summary, body, "[]",
             f"{{'desktop-entry': <'{DESKTOP_ENTRY}'>, 'image-path': <'{icon}'>}}", "-1"],
            timeout=5,
        )
