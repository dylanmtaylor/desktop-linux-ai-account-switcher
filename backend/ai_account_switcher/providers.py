"""Claude Code and Codex CLI account handling.

Both CLIs keep their *active* login in a plain 0600 file on Linux:

* Claude Code: ``~/.claude/.credentials.json`` (OAuth tokens) plus the
  ``oauthAccount`` object in ``~/.claude.json`` (email, org, plan).
  ``CLAUDE_CONFIG_DIR`` moves both into that directory.
* Codex CLI: ``~/.codex/auth.json`` (``CODEX_HOME`` moves it) when
  ``cli_auth_credentials_store`` is ``file`` (or ``auto`` with no keyring entry).

A *snapshot* is everything needed to restore one login. Saved snapshots live in
the secret store, keyed by provider and email. Switching first re-saves the
current login (so rotated refresh tokens are never lost), then writes the
target snapshot back into the CLI's files.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

from ai_account_switcher import usage
from ai_account_switcher.config import Account, Config
from ai_account_switcher.desktop import run_login_in_terminal
from ai_account_switcher.secrets import SecretStore
from ai_account_switcher.util import (
    SwitcherError,
    atomic_write_text,
    decode_jwt_payload,
    find_program,
    loads_dict,
    processes_named,
    program_env,
    read_json,
    read_text,
    run,
    valid_email,
    window_label,
)

LOGIN_TIMEOUT = 300


@dataclass
class Live:
    """The login a CLI is currently using."""

    snapshot: str
    email: str | None
    plan: str = ""
    org: str = ""
    meta: dict = field(default_factory=dict)


class Provider:
    id = ""
    label = ""
    program = ""

    def __init__(self, config: Config):
        self.config = config
        self.settings = config.settings

    # ------------------------------------------------------------ provider specifics
    def command_override(self) -> str:
        return ""

    def read_live(self) -> Live | None:
        raise NotImplementedError

    def restore(self, snapshot: str) -> None:
        raise NotImplementedError

    def clear_live(self) -> None:
        raise NotImplementedError

    def login_argv(self, cli: str) -> list[str]:
        raise NotImplementedError

    def fetch_usage(self, snapshot: str, active: bool) -> tuple[dict, str | None]:
        """Returns ``(usage_state, refreshed_snapshot_or_None)``."""
        raise NotImplementedError

    def email_from_cli(self, cli: str) -> str | None:
        return None

    def notice(self) -> str | None:
        return None

    # ------------------------------------------------------------ shared logic
    def cli(self) -> str | None:
        return find_program(self.program, self.command_override())

    def running_sessions(self) -> int:
        return len(processes_named(self.program))

    def match(self, live: Live) -> Account | None:
        accounts = self.config.for_provider(self.id)
        for key in ("account_uuid", "account_id"):
            value = live.meta.get(key)
            if value:
                hit = next((a for a in accounts if a.meta.get(key) == value), None)
                if hit:
                    return hit
        if live.email:
            return next((a for a in accounts if a.id == live.email), None)
        return None

    def capture(self, store: SecretStore) -> Account | None:
        """Save the CLI's current login (adding the account if it is new)."""
        live = self.read_live()
        if not live:
            return None
        account = self.match(live)
        email = account.id if account else live.email
        if not email:
            cli = self.cli()
            email = self.email_from_cli(cli) if cli else None
        if not valid_email(email):
            return None
        account = account or Account(provider=self.id, id=email)  # type: ignore[arg-type]
        account.plan = live.plan or account.plan
        account.org = live.org or account.org
        account.meta.update({k: v for k, v in live.meta.items() if v})
        if store.get(self.id, account.id) != live.snapshot:
            store.set(self.id, account.id, live.snapshot)
        account.has_credentials = True
        self.config.upsert(account)
        self.config.set_active(self.id, account.id)
        self.config.save()
        return account

    def live_account(self) -> tuple[Account | None, Live | None]:
        live = self.read_live()
        return (self.match(live) if live else None), live

    def switch(self, store: SecretStore, target_id: str) -> dict:
        target = self.config.find(self.id, target_id)
        if not target:
            raise SwitcherError(f"No saved {self.label} account {target_id}.")
        current = self.capture(store)
        if current and current.id == target.id:
            return {"changed": False, "account": target.id}
        snapshot = store.get(self.id, target.id)
        if not snapshot:
            target.has_credentials = False
            self.config.save()
            raise SwitcherError(f"Saved credentials for {target.id} are missing. Add the account again.")
        snapshot = self.prepare_restore(store, target, snapshot)
        self.restore(snapshot)
        self.config.set_active(self.id, target.id)
        self.config.save()
        return {"changed": True, "account": target.id, "previous": current.id if current else None,
                "running_sessions": self.running_sessions()}

    def prepare_restore(self, store: SecretStore, target: Account, snapshot: str) -> str:
        return snapshot

    def remove(self, store: SecretStore, target_id: str) -> None:
        active, _ = self.live_account()
        if active and active.id == target_id:
            raise SwitcherError(
                f"{target_id} is the active {self.label} account. Switch to another account first."
            )
        store.delete(self.id, target_id)
        self.config.remove(self.id, target_id)
        self.config.save()

    def add(
        self,
        store: SecretStore,
        timeout: float = LOGIN_TIMEOUT,
        lock: Callable[[], AbstractContextManager] = nullcontext,
    ) -> Account | None:
        """Back up the current login, run the CLI login in a terminal, save the new login.

        ``lock`` guards the file changes but is *not* held while the user signs in.
        """
        cli = self.cli()
        if not cli:
            raise SwitcherError(f"The {self.program} command was not found. Install {self.label} first.")
        with lock():
            previous = self.capture(store)
            previous_live = self.read_live()
            previous_snapshot = previous_live.snapshot if previous_live else None
            previous_key = self._identity_key(previous_live)
            self.clear_live()

        def done() -> bool:
            # A new account, or fresh tokens for the same account (re-login).
            live = self.read_live()
            return bool(live) and (
                self._identity_key(live) != previous_key or live.snapshot != previous_snapshot
            )

        try:
            finished = run_login_in_terminal(
                f"Add {self.label} account", self.login_argv(cli), self.settings.terminal, done, timeout
            )
        except SwitcherError:
            with lock():
                if previous_snapshot and not self.read_live():
                    self.restore(previous_snapshot)
            raise

        with lock():
            live = self.read_live()
            if finished and live:
                account = self.capture(store)
                if account:
                    return account
            # Cancelled, failed or timed out: put the previous login back.
            if previous_snapshot and (not live or live.snapshot == previous_snapshot):
                self.restore(previous_snapshot)
                if previous:
                    self.config.set_active(self.id, previous.id)
                    self.config.save()
        return None

    @staticmethod
    def _identity_key(live: Live | None) -> str | None:
        if not live:
            return None
        return str(live.meta.get("account_uuid") or live.meta.get("account_id") or live.email or live.snapshot)


# =============================================================================== Claude


CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


class ClaudeExpired(SwitcherError):
    pass


class ClaudeCode(Provider):
    id = "claude"
    label = "Claude Code"
    program = "claude"

    def command_override(self) -> str:
        return self.settings.claude_command

    def _custom_dir(self) -> str:
        return self.settings.claude_config_dir or os.environ.get("CLAUDE_CONFIG_DIR", "")

    def config_dir(self) -> Path:
        custom = self._custom_dir()
        return Path(os.path.expanduser(custom)) if custom else Path.home() / ".claude"

    def credentials_path(self) -> Path:
        return self.config_dir() / ".credentials.json"

    def state_path(self) -> Path:
        return self.config_dir() / ".claude.json" if self._custom_dir() else Path.home() / ".claude.json"

    def read_live(self) -> Live | None:
        text = read_text(self.credentials_path())
        creds = loads_dict(text)
        oauth = creds.get("claudeAiOauth") if creds else None
        if not isinstance(oauth, dict) or not oauth.get("accessToken"):
            return None
        uuid = oauth.get("accountUuid") if isinstance(oauth.get("accountUuid"), str) else None
        state = read_json(self.state_path()) or {}
        account = state.get("oauthAccount") if isinstance(state.get("oauthAccount"), dict) else None
        if account and uuid and account.get("accountUuid") and account.get("accountUuid") != uuid:
            account = None  # ~/.claude.json describes a different login
        email = account.get("emailAddress") if account else None
        plan = ""
        if account and isinstance(account.get("planDisplayName"), str):
            plan = account["planDisplayName"]
        elif isinstance(oauth.get("subscriptionType"), str):
            plan = oauth["subscriptionType"].capitalize()
        org = account.get("organizationName", "") if account else ""
        snapshot = json.dumps({"credentials": (text or "").strip(), "oauthAccount": account},
                              separators=(",", ":"), sort_keys=True)
        return Live(snapshot=snapshot, email=email if valid_email(email) else None,
                    plan=plan, org=org if isinstance(org, str) else "", meta={"account_uuid": uuid})

    def email_from_cli(self, cli: str) -> str | None:
        result = run([cli, "auth", "status", "--json"], timeout=20, env=program_env(cli))
        data = loads_dict(result.stdout) if result and result.returncode == 0 else None
        email = data.get("email") if data else None
        return email if valid_email(email) else None

    def restore(self, snapshot: str) -> None:
        data = loads_dict(snapshot) or {}
        creds_text = data.get("credentials")
        creds = loads_dict(creds_text) if isinstance(creds_text, str) else None
        if not creds or not isinstance(creds.get("claudeAiOauth"), dict):
            raise SwitcherError("Saved Claude Code credentials are corrupt; add the account again.")
        atomic_write_text(self.credentials_path(), creds_text, 0o600)  # type: ignore[arg-type]
        account = data.get("oauthAccount")
        if isinstance(account, dict):
            path = self.state_path()
            state = read_json(path)
            if state is None and path.exists():
                return  # unreadable but present: never clobber it
            state = state or {}
            state["oauthAccount"] = account
            atomic_write_text(path, json.dumps(state, indent=2), 0o600)

    def clear_live(self) -> None:
        try:
            self.credentials_path().unlink()
        except FileNotFoundError:
            pass

    def login_argv(self, cli: str) -> list[str]:
        return [cli, "auth", "login"]

    @staticmethod
    def _token_expired(oauth: dict) -> bool:
        expires = oauth.get("expiresAt")
        return isinstance(expires, (int, float)) and expires / 1000 < time.time() + 60

    def refresh(self, snapshot: str) -> str | None:
        """Exchange the refresh token. Raises ClaudeExpired if it was revoked or already used."""
        data = loads_dict(snapshot) or {}
        creds = loads_dict(data.get("credentials")) if isinstance(data.get("credentials"), str) else None
        oauth = creds.get("claudeAiOauth") if creds else None
        if not isinstance(oauth, dict) or not oauth.get("refreshToken"):
            return None
        body = json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": oauth["refreshToken"],
            "client_id": CLAUDE_CLIENT_ID,
        }).encode()
        status, reply, text = usage.http_json(
            CLAUDE_TOKEN_URL, {"Content-Type": "application/json"}, 15, body
        )
        if status in (400, 401) and any(s in text.lower() for s in ("invalid_grant", "invalid_token",
                                                                     "revoked", "expired")):
            raise ClaudeExpired("This saved Claude Code login has expired. Add the account again.")
        if status != 200 or not reply or not isinstance(reply.get("access_token"), str) or not reply["access_token"]:
            return None
        oauth["accessToken"] = reply["access_token"]
        if isinstance(reply.get("refresh_token"), str) and reply["refresh_token"]:
            oauth["refreshToken"] = reply["refresh_token"]
        if isinstance(reply.get("expires_in"), (int, float)):
            oauth["expiresAt"] = int((time.time() + reply["expires_in"]) * 1000)
        creds["claudeAiOauth"] = oauth  # type: ignore[index]
        data["credentials"] = json.dumps(creds, separators=(",", ":"))
        return json.dumps(data, separators=(",", ":"), sort_keys=True)

    def fetch_usage(self, snapshot: str, active: bool) -> tuple[dict, str | None]:
        refreshed = None
        data = loads_dict(snapshot) or {}
        creds = loads_dict(data.get("credentials")) if isinstance(data.get("credentials"), str) else None
        oauth = creds.get("claudeAiOauth") if creds else None
        token = oauth.get("accessToken") if isinstance(oauth, dict) else None
        if not token:
            return usage.error("No access token saved", "login_required"), None
        if self._token_expired(oauth):  # type: ignore[arg-type]
            if active:
                # Never refresh the live login: a running claude process holds the same
                # refresh token, and rotating it underneath would sign that process out.
                return usage.error("Token expired; Claude Code refreshes it on next use", "expired"), None
            try:
                refreshed = self.refresh(snapshot)
            except ClaudeExpired:
                return usage.error("Login expired; add the account again", "login_required"), None
            if not refreshed:
                return usage.error("Could not refresh the saved login", "network"), None
            data = loads_dict(refreshed) or {}
            creds = loads_dict(data.get("credentials")) if isinstance(data.get("credentials"), str) else None
            oauth = creds.get("claudeAiOauth") if creds else None
            token = oauth.get("accessToken") if isinstance(oauth, dict) else None
            if not token:
                return usage.error("No access token saved", "login_required"), refreshed
        status, body, _ = usage.http_json(
            CLAUDE_USAGE_URL,
            {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"},
        )
        if status is None:
            return usage.error("Offline or the usage service is unreachable", "network"), refreshed
        if status in (401, 403):
            return usage.error("Session expired; switch to it or add it again", "login_required"), refreshed
        if status != 200 or body is None:
            return usage.error(f"Usage service returned HTTP {status}", "http"), refreshed
        return parse_claude_usage(body), refreshed


def parse_claude_usage(body: dict) -> dict:
    windows = []
    for label, key in (("5h", "five_hour"), ("7d", "seven_day")):
        raw = body.get(key)
        if isinstance(raw, dict) and "utilization" in raw:
            w = usage.window(label, raw.get("utilization"), usage.parse_iso(raw.get("resets_at")))
            if w:
                windows.append(w)
    return usage.ok(windows)


# =============================================================================== Codex

CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_USAGE_URLS = (
    "https://chatgpt.com/backend-api/wham/usage",
    "https://chatgpt.com/backend-api/api/codex/usage",
)
_STORE_RE = re.compile(r'^\s*cli_auth_credentials_store\s*=\s*["\']([a-z]+)["\']', re.MULTILINE)


class CodexExpired(SwitcherError):
    pass


class Codex(Provider):
    id = "codex"
    label = "Codex"
    program = "codex"

    def command_override(self) -> str:
        return self.settings.codex_command

    def home(self) -> Path:
        custom = os.environ.get("CODEX_HOME")
        return Path(os.path.expanduser(custom)) if custom else Path.home() / ".codex"

    def auth_path(self) -> Path:
        return self.home() / "auth.json"

    def store_mode(self) -> str:
        match = _STORE_RE.search(read_text(self.home() / "config.toml") or "")
        return match.group(1) if match else "auto"

    def notice(self) -> str | None:
        mode = self.store_mode()
        if mode == "keyring" or (mode == "auto" and not self.auth_path().exists() and self.cli()):
            return ('Codex keeps its login in the keyring, which can\'t be switched. Set '
                    'cli_auth_credentials_store = "file" in ~/.codex/config.toml, or use "Add Codex account".')
        return None

    @staticmethod
    def _claims(data: dict) -> dict:
        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
        payload = decode_jwt_payload(tokens.get("id_token", "")) if isinstance(tokens.get("id_token"), str) else None
        return payload or {}

    def read_live(self) -> Live | None:
        text = read_text(self.auth_path())
        data = loads_dict(text)
        if not data or not isinstance(data.get("tokens"), dict):
            return None
        claims = self._claims(data)
        auth = claims.get("https://api.openai.com/auth")
        auth = auth if isinstance(auth, dict) else {}
        email = claims.get("email")
        plan = str(auth.get("chatgpt_plan_type") or "").capitalize()
        account_id = data["tokens"].get("account_id") or auth.get("chatgpt_account_id")
        return Live(snapshot=(text or "").strip(), email=email if valid_email(email) else None,
                    plan=plan, meta={"account_id": account_id if isinstance(account_id, str) else None})

    def restore(self, snapshot: str) -> None:
        if not loads_dict(snapshot):
            raise SwitcherError("Saved Codex credentials are corrupt; add the account again.")
        atomic_write_text(self.auth_path(), snapshot, 0o600)

    def clear_live(self) -> None:
        try:
            self.auth_path().unlink()
        except FileNotFoundError:
            pass

    def login_argv(self, cli: str) -> list[str]:
        return [cli, "login", "-c", 'cli_auth_credentials_store="file"']

    # -------------------------------------------------------------- tokens
    @staticmethod
    def _token_expired(token: str) -> bool:
        payload = decode_jwt_payload(token) or {}
        exp = payload.get("exp")
        return isinstance(exp, (int, float)) and exp < time.time() + 60

    def refresh(self, snapshot: str) -> str | None:
        """Exchange the refresh token. Raises CodexExpired if it was revoked or already used."""
        data = loads_dict(snapshot)
        tokens = data.get("tokens") if data else None
        if not isinstance(tokens, dict) or not tokens.get("refresh_token"):
            return None
        body = urlencode({"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                          "client_id": CODEX_CLIENT_ID}).encode()
        status, reply, text = usage.http_json(
            CODEX_TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"}, 15, body
        )
        if status in (400, 401) and any(s in text for s in ("invalid_grant", "already been used",
                                                             "token_invalidated", "refresh_token_reused")):
            raise CodexExpired("This saved Codex login has expired. Add the account again.")
        if status != 200 or not reply:
            return None
        for key in ("access_token", "refresh_token", "id_token"):
            if isinstance(reply.get(key), str) and reply[key]:
                tokens[key] = reply[key]
        data["tokens"] = tokens  # type: ignore[index]
        data["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())  # type: ignore[index]
        return json.dumps(data, indent=2)

    def prepare_restore(self, store: SecretStore, target: Account, snapshot: str) -> str:
        data = loads_dict(snapshot) or {}
        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
        if isinstance(tokens.get("access_token"), str) and self._token_expired(tokens["access_token"]):
            refreshed = self.refresh(snapshot)  # CodexExpired propagates with a clear message
            if refreshed:
                store.set(self.id, target.id, refreshed)
                return refreshed
        return snapshot

    def fetch_usage(self, snapshot: str, active: bool) -> tuple[dict, str | None]:
        refreshed = None
        data = loads_dict(snapshot) or {}
        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
        token = tokens.get("access_token")
        if not isinstance(token, str) or not token:
            return usage.error("No access token saved", "login_required"), None
        if self._token_expired(token):
            if active:
                # Never refresh the live login: a running codex process holds the same
                # refresh token, and rotating it underneath would sign that process out.
                return usage.error("Token expired; Codex refreshes it on next use", "expired"), None
            try:
                refreshed = self.refresh(snapshot)
            except CodexExpired:
                return usage.error("Login expired; add the account again", "login_required"), None
            if not refreshed:
                return usage.error("Could not refresh the saved login", "network"), None
            data = loads_dict(refreshed) or {}
            tokens = data.get("tokens", {})
            token = tokens.get("access_token", "")
        account_id = tokens.get("account_id") or (
            (self._claims(data).get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")
        )
        headers = {"Authorization": f"Bearer {token}"}
        if isinstance(account_id, str) and account_id:
            headers["ChatGPT-Account-Id"] = account_id
        last_status = None
        for url in CODEX_USAGE_URLS:
            status, body, _ = usage.http_json(url, headers)
            last_status = status
            if status == 200 and body is not None:
                return parse_codex_usage(body), refreshed
            if status in (401, 403):
                return usage.error("Session expired; switch to it or add it again", "login_required"), refreshed
        if last_status is None:
            return usage.error("Offline or the usage service is unreachable", "network"), refreshed
        return usage.error(f"Usage service returned HTTP {last_status}", "http"), refreshed


def parse_codex_usage(body: dict) -> dict:
    rate = body.get("rate_limit")
    if not isinstance(rate, dict):
        return usage.error("No usage data returned", "empty")
    windows = []
    for key, fallback in (("primary_window", "5h"), ("secondary_window", "7d")):
        raw = rate.get(key)
        if not isinstance(raw, dict) or "used_percent" not in raw:
            continue
        resets_at = raw.get("reset_at")
        if not isinstance(resets_at, (int, float)):
            after = raw.get("reset_after_seconds")
            resets_at = time.time() + after if isinstance(after, (int, float)) else None
        w = usage.window(window_label(raw.get("limit_window_seconds"), fallback), raw["used_percent"], resets_at)
        if w:
            windows.append(w)
    return usage.ok(windows)


def all_providers(config: Config) -> list[Provider]:
    return [ClaudeCode(config), Codex(config)]
