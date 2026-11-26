"""``ai-account-switcher`` command line. Front ends call it with ``--json``."""

from __future__ import annotations

import argparse
import json
import sys

from ai_account_switcher import APP_NAME, __version__, app
from ai_account_switcher.config import PROVIDERS, SETTABLE
from ai_account_switcher.util import SwitcherError

ACCOUNT_PROVIDERS = (*PROVIDERS, "desktop")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-account-switcher",
        description=f"{APP_NAME}: switch Claude Code and Codex CLI accounts.",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output (used by the panel apps)")
    parser.add_argument("--notify", action="store_true", help="show desktop notifications for what happened")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show accounts and the last known usage (no network)")
    p = sub.add_parser("refresh", help="save current logins, fetch usage, auto-switch if enabled")
    p.add_argument("--no-auto-switch", action="store_true")

    p = sub.add_parser("switch", help="make a saved account active (desktop: open that profile)")
    p.add_argument("provider", choices=ACCOUNT_PROVIDERS)
    p.add_argument("account", help="account email")

    p = sub.add_parser("add", help="sign in to another account (desktop: create a profile)")
    p.add_argument("provider", choices=ACCOUNT_PROVIDERS)
    p.add_argument("--name", help="profile name (required for desktop)")

    p = sub.add_parser("remove", help="forget a saved (inactive) account (desktop: trash the profile)")
    p.add_argument("provider", choices=ACCOUNT_PROVIDERS)
    p.add_argument("account", help="account email")

    p = sub.add_parser("auto-switch", help="switch automatically when the active account hits its limit")
    p.add_argument("provider", choices=PROVIDERS)
    p.add_argument("state", choices=("on", "off"))

    sub.add_parser("open", help="show the panel menu (GNOME) or the widget window (KDE); used by the app launcher")

    p = sub.add_parser("set", help="change a setting")
    p.add_argument("key", choices=sorted(SETTABLE))
    p.add_argument("value")
    return parser


def _print_human(result: dict) -> None:
    for event in result.get("events", []):
        print(event.get("message", ""))
    for provider in result.get("providers", []):
        flag = " [auto-switch]" if provider["auto_switch"] else ""
        print(f"\n{provider['label']}{flag}")
        if not provider["cli_found"]:
            print(f"  ({provider['id']} command not found)")
        if provider.get("notice"):
            print(f"  ! {provider['notice']}")
        if provider.get("unsaved_login"):
            print(f"  ! signed in as {provider['unsaved_login']} (not saved yet; run refresh)")
        if not provider["accounts"] and provider.get("manageable", True):
            print(f"  no accounts. Add one: ai-account-switcher add {provider['id']}")
        for account in provider["accounts"]:
            marker = "●" if account["active"] else "○"
            plan = f" ({account['plan']})" if account["plan"] else ""
            print(f"  {marker} {account['email']}{plan}")
            if account.get("usage"):
                print(f"      {account['usage']['text']}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "open":
            from ai_account_switcher.desktop import open_ui
            status = app.status()
            lines = [line for line in status["panel"]["tooltip"].splitlines()]
            result = {**status, "opened": open_ui("\n".join(lines))}
        elif args.command == "status":
            result = app.status()
        elif args.command == "refresh":
            result = app.refresh(auto_switch=not args.no_auto_switch)
        elif args.command == "switch":
            result = app.switch(args.provider, args.account)
        elif args.command == "add":
            result = app.add(args.provider, args.name)
        elif args.command == "remove":
            result = app.remove(args.provider, args.account)
        elif args.command == "auto-switch":
            result = app.set_auto_switch(args.provider, args.state == "on")
        else:
            result = app.set_setting(args.key, args.value)
    except SwitcherError as exc:
        return _fail(args, str(exc))
    except Exception as exc:  # never dump a traceback into a panel menu
        return _fail(args, f"Unexpected error: {exc.__class__.__name__}: {exc}")
    if args.notify:
        app.send_notifications(result.get("events", []))
    if args.json:
        print(json.dumps(result))
    else:
        _print_human(result)
    return 0


def _fail(args: argparse.Namespace, message: str) -> int:
    if args.notify:
        app.send_notifications([{"type": "error", "message": message}])
    if args.json:
        print(json.dumps({"ok": False, "error": message}))
    else:
        print(f"error: {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
