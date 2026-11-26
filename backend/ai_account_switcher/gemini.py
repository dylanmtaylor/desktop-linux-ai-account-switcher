"""Read-only Gemini status via the Antigravity CLI (``agy``).

* Account: agy keeps its Google OAuth login in the Secret Service under
  ``service=gemini username=antigravity``; we read only ``id_token_email`` and
  ``auth_method`` from it and never store, change or send the tokens.
* Usage: ``agy -p /usage --output-format json`` answers locally (no model turn,
  zero tokens) with per-group 5-hour and weekly buckets.

Safety: if an agy build ever answers ``/usage`` with a model turn instead of the
local command, that would spend the user's quota on every refresh. We detect it
(no ``command`` in the reply, or tokens > 0) and stop calling agy until its
binary changes.
"""

from __future__ import annotations

import json
import os

from ai_account_switcher import usage
from ai_account_switcher.util import (
    ensure_private_dir,
    find_program,
    loads_dict,
    program_env,
    run,
    runtime_dir,
    valid_email,
)

PROVIDER = "gemini"
LABEL = "Gemini (Antigravity)"
_GROUP_SHORT = {"gemini models": "Gemini", "claude and gpt models": "Claude/GPT"}
_WINDOW_SHORT = {"5h": "5h", "weekly": "7d", "daily": "1d"}


def agy_path() -> str | None:
    return find_program("agy")


def _binary_stamp(path: str) -> str:
    try:
        st = os.stat(os.path.realpath(path))
    except OSError:
        return path
    return f"{os.path.realpath(path)}:{st.st_size}:{int(st.st_mtime)}"


def read_login() -> dict | None:
    """``{"email", "auth_method"}`` of the agy login, or None when signed out/unknown."""
    tool = find_program("secret-tool")
    if not tool:
        return None
    result = run([tool, "lookup", "service", "gemini", "username", "antigravity"], timeout=30)
    data = loads_dict(result.stdout) if result and result.returncode == 0 else None
    if not data:
        return None
    email = data.get("id_token_email")
    method = data.get("auth_method") if isinstance(data.get("auth_method"), str) else ""
    return {"email": email if valid_email(email) else None, "auth_method": method}


def plan_label(auth_method: str) -> str:
    return {"consumer": "Google account", "enterprise": "Workspace / enterprise"}.get(
        auth_method, auth_method.capitalize()
    )


def parse_usage(reply: dict) -> dict:
    command = reply.get("command") if isinstance(reply.get("command"), dict) else {}
    data = command.get("data") if isinstance(command.get("data"), dict) else {}
    windows = []
    for group in data.get("groups", []) if isinstance(data.get("groups"), list) else []:
        if not isinstance(group, dict):
            continue
        name = str(group.get("name") or "")
        short = _GROUP_SHORT.get(name.lower(), name.replace(" models", "").replace(" Models", ""))
        for bucket in group.get("buckets", []) if isinstance(group.get("buckets"), list) else []:
            if not isinstance(bucket, dict):
                continue
            remaining = bucket.get("remaining_fraction")
            if not isinstance(remaining, (int, float)):
                continue
            resets_at = usage.parse_iso(bucket.get("reset_time"))
            if resets_at is not None and resets_at < 86400:  # 1970 = window not started
                resets_at = None
            label = f"{short} {_WINDOW_SHORT.get(str(bucket.get('window')), str(bucket.get('window') or ''))}".strip()
            w = usage.window(label, (1 - float(remaining)) * 100, resets_at)
            if w:
                windows.append(w)
    # Keep agy's group order, but within a group show 5h before 7d; Gemini first.
    order = {"5h": 0, "1d": 1, "7d": 2}
    groups = list(dict.fromkeys(w["label"].rsplit(" ", 1)[0] for w in windows))
    windows.sort(key=lambda w: (w["label"].rsplit(" ", 1)[0] != "Gemini",
                                groups.index(w["label"].rsplit(" ", 1)[0]),
                                order.get(w["label"].rsplit(" ", 1)[-1], 9)))
    return usage.ok(windows)


def fetch_usage(state: dict) -> dict:
    """Run ``agy /usage``. ``state`` is the persisted runtime state (for the safety latch)."""
    agy = agy_path()
    if not agy:
        return usage.error("agy is not installed", "missing")
    stamp = _binary_stamp(agy)
    if state.get("agy_usage_unsupported") == stamp:
        return usage.error("This agy version has no local /usage command", "unsupported")
    workdir = ensure_private_dir(runtime_dir() / "agy")
    result = run([agy, "-p", "/usage", "--output-format", "json", "--print-timeout", "30s"],
                 timeout=45, env=program_env(agy), cwd=workdir)
    if result is None:
        return usage.error("agy did not answer in time", "timeout")
    reply = None
    for line in reversed(result.stdout.strip().splitlines()):
        reply = loads_dict(line)
        if reply:
            break
    if not reply:
        message = (result.stdout + result.stderr).strip().splitlines()
        text = message[-1][:160] if message else "no output"
        if "log" in text.lower() and "in" in text.lower():
            return usage.error("Not signed in to agy; run agy once to sign in", "login_required")
        return usage.error(f"agy: {text}", "error")
    command = reply.get("command") if isinstance(reply.get("command"), dict) else None
    tokens = (reply.get("usage") or {}).get("total_tokens") if isinstance(reply.get("usage"), dict) else 0
    if not command or command.get("name") != "usage" or (isinstance(tokens, (int, float)) and tokens > 0):
        state["agy_usage_unsupported"] = stamp
        return usage.error("This agy version has no local /usage command", "unsupported")
    return parse_usage(reply)


def status_entry(state: dict) -> dict | None:
    """The read-only provider block for ``status``; None when agy isn't installed or used."""
    agy = agy_path()
    info = state.get("gemini") if isinstance(state.get("gemini"), dict) else {}
    if not agy and not info:
        return None
    account = info.get("login") if isinstance(info.get("login"), dict) else None
    accounts = []
    if account and account.get("email"):
        accounts.append({
            "id": account["email"], "email": account["email"],
            "plan": plan_label(account.get("auth_method", "")), "org": "",
            "active": True, "has_credentials": True,
            "usage": usage.render(info.get("usage")),
        })
    return {
        "id": PROVIDER, "label": LABEL, "cli_found": agy is not None, "manageable": False,
        "active": accounts[0]["id"] if accounts else None, "unsaved_login": None,
        "auto_switch": False,
        "notice": None if accounts or not agy else "Not signed in to agy. Run agy in a terminal to sign in.",
        "pending": None, "accounts": accounts,
    }


def refresh_into(state: dict) -> None:
    """Update ``state["gemini"]`` with the current login and usage."""
    if not agy_path():
        state.pop("gemini", None)
        return
    login = read_login()
    entry = {"login": login, "usage": fetch_usage(state) if login else None}
    state["gemini"] = json.loads(json.dumps(entry))
