"""Account metadata and settings in ``~/.config/ai-account-switcher/accounts.json``.

Only non-secret metadata lives here; tokens live in the secret store
(:mod:`ai_account_switcher.secrets`).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from ai_account_switcher.util import atomic_write_text, config_dir, ensure_private_dir, read_json

CONFIG_VERSION = 1
PROVIDERS = ("claude", "codex")


@dataclass
class Account:
    provider: str
    id: str  # the account email
    plan: str = ""
    org: str = ""
    active: bool = False
    has_credentials: bool = True
    meta: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.id}"


@dataclass
class Settings:
    auto_switch: dict = field(default_factory=lambda: {p: False for p in PROVIDERS})
    auto_switch_threshold: float = 100.0
    secret_backend: str = "auto"  # auto | secret-service | file
    claude_config_dir: str = ""
    claude_command: str = ""
    codex_command: str = ""
    terminal: str = ""
    show_usage_in_panel: bool = True
    refresh_minutes: int = 5


# Settings the CLI ``set`` command may change, with their parsers.
def _parse_bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"expected on/off, got {value!r}")


def _parse_threshold(value: str) -> float:
    number = float(value)
    if not 1 <= number <= 100:
        raise ValueError("threshold must be between 1 and 100")
    return number


def _parse_minutes(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 1440:
        raise ValueError("refresh interval must be between 1 and 1440 minutes")
    return number


def _parse_backend(value: str) -> str:
    if value not in {"auto", "secret-service", "file"}:
        raise ValueError("secret backend must be auto, secret-service or file")
    return value


SETTABLE = {
    "auto_switch_threshold": _parse_threshold,
    "secret_backend": _parse_backend,
    "claude_config_dir": str,
    "claude_command": str,
    "codex_command": str,
    "terminal": str,
    "show_usage_in_panel": _parse_bool,
    "refresh_minutes": _parse_minutes,
}


def _settings_from(raw: object) -> Settings:
    settings = Settings()
    if not isinstance(raw, dict):
        return settings
    auto = raw.get("auto_switch")
    if isinstance(auto, dict):
        for provider in PROVIDERS:
            if provider in auto:
                settings.auto_switch[provider] = bool(auto[provider])
    for f in fields(Settings):
        if f.name == "auto_switch" or f.name not in raw:
            continue
        value = raw[f.name]
        default = getattr(settings, f.name)
        if isinstance(default, bool):
            if isinstance(value, bool):
                setattr(settings, f.name, value)
        elif isinstance(default, (int, float)):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                setattr(settings, f.name, type(default)(value))
        elif isinstance(value, str):
            setattr(settings, f.name, value)
    try:
        settings.auto_switch_threshold = _parse_threshold(str(settings.auto_switch_threshold))
    except ValueError:
        settings.auto_switch_threshold = 100.0
    try:
        settings.secret_backend = _parse_backend(settings.secret_backend)
    except ValueError:
        settings.secret_backend = "auto"
    settings.refresh_minutes = min(max(int(settings.refresh_minutes), 1), 1440)
    return settings


def _account_from(raw: object) -> Account | None:
    if not isinstance(raw, dict):
        return None
    provider, account_id = raw.get("provider"), raw.get("id")
    if provider not in PROVIDERS or not isinstance(account_id, str) or not account_id:
        return None
    meta = raw.get("meta")
    return Account(
        provider=provider,
        id=account_id,
        plan=str(raw.get("plan") or ""),
        org=str(raw.get("org") or ""),
        active=bool(raw.get("active")),
        has_credentials=bool(raw.get("has_credentials", True)),
        meta=meta if isinstance(meta, dict) else {},
    )


class Config:
    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "accounts.json"
        data = read_json(self.path) or {}
        self.exists = self.path.exists()
        self.settings = _settings_from(data.get("settings"))
        accounts = data.get("accounts")
        self.accounts: list[Account] = [
            a for a in (_account_from(r) for r in (accounts if isinstance(accounts, list) else [])) if a
        ]

    # ---------------------------------------------------------------- persistence
    def save(self) -> None:
        ensure_private_dir(self.path.parent)
        payload = {
            "version": CONFIG_VERSION,
            "settings": asdict(self.settings),
            "accounts": [asdict(a) for a in self.accounts],
        }
        atomic_write_text(self.path, json.dumps(payload, indent=2) + "\n", 0o600)
        self.exists = True

    # ---------------------------------------------------------------- queries
    def for_provider(self, provider: str) -> list[Account]:
        return [a for a in self.accounts if a.provider == provider]

    def find(self, provider: str, account_id: str) -> Account | None:
        return next((a for a in self.for_provider(provider) if a.id == account_id), None)

    def active(self, provider: str) -> Account | None:
        return next((a for a in self.for_provider(provider) if a.active), None)

    # ---------------------------------------------------------------- mutations
    def upsert(self, account: Account) -> None:
        for index, existing in enumerate(self.accounts):
            if existing.provider == account.provider and existing.id == account.id:
                self.accounts[index] = account
                return
        self.accounts.append(account)

    def remove(self, provider: str, account_id: str) -> None:
        self.accounts = [
            a for a in self.accounts if not (a.provider == provider and a.id == account_id)
        ]

    def set_active(self, provider: str, account_id: str | None) -> None:
        for account in self.for_provider(provider):
            account.active = account.id == account_id
