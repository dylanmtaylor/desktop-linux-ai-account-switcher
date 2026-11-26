"""Where saved account tokens live.

* ``secret-service`` (default when available): the freedesktop Secret Service,
  i.e. GNOME Keyring or KWallet, via ``secret-tool``. Secrets go over stdin,
  never argv.
* ``file``: 0600 files under ``~/.local/share/ai-account-switcher/secrets``
  (a 0700 directory). Claude Code and Codex already keep their *active* tokens
  in plain 0600 files on Linux, so this is no weaker than the CLIs themselves.

The backend is chosen once and recorded in the settings, so saved accounts never
silently move between stores.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
from typing import Protocol

from ai_account_switcher import APP_ID, APP_NAME
from ai_account_switcher.config import Config
from ai_account_switcher.util import (
    SwitcherError,
    atomic_write_text,
    data_dir,
    ensure_private_dir,
    find_program,
    read_text,
    run,
)

SECRET_TIMEOUT = 60  # a locked keyring shows an unlock prompt; give the user time


class SecretStore(Protocol):
    name: str

    def get(self, provider: str, account_id: str) -> str | None: ...
    def set(self, provider: str, account_id: str, secret: str) -> None: ...
    def delete(self, provider: str, account_id: str) -> bool: ...


class SecretServiceStore:
    name = "secret-service"

    def __init__(self, tool: str):
        self.tool = tool

    @staticmethod
    def _attrs(provider: str, account_id: str) -> list[str]:
        return ["application", APP_ID, "provider", provider, "account", account_id]

    def get(self, provider: str, account_id: str) -> str | None:
        result = run([self.tool, "lookup", *self._attrs(provider, account_id)], SECRET_TIMEOUT)
        if result is None:
            raise SwitcherError("Timed out waiting for the keyring. Is it locked?")
        if result.returncode != 0:
            if result.stderr.strip():
                raise SwitcherError(f"Keyring error: {result.stderr.strip()}")
            return None
        return result.stdout.strip() or None

    def set(self, provider: str, account_id: str, secret: str) -> None:
        label = f"{APP_NAME}: {provider} {account_id}"
        result = run(
            [self.tool, "store", "--label", label, *self._attrs(provider, account_id)],
            SECRET_TIMEOUT,
            input_text=secret,
        )
        if result is None or result.returncode != 0:
            detail = result.stderr.strip() if result else "timed out"
            raise SwitcherError(f"Could not save credentials to the keyring ({detail}).")

    def delete(self, provider: str, account_id: str) -> bool:
        result = run([self.tool, "clear", *self._attrs(provider, account_id)], SECRET_TIMEOUT)
        return result is not None and result.returncode == 0


class FileStore:
    name = "file"

    def __init__(self) -> None:
        self.root = data_dir() / "secrets"

    def _path(self, provider: str, account_id: str):
        digest = hashlib.sha256(f"{provider}\0{account_id}".encode()).hexdigest()[:32]
        return self.root / f"{provider}-{digest}.json"

    def get(self, provider: str, account_id: str) -> str | None:
        text = read_text(self._path(provider, account_id))
        return text.strip() if text and text.strip() else None

    def set(self, provider: str, account_id: str, secret: str) -> None:
        ensure_private_dir(self.root.parent)
        ensure_private_dir(self.root)
        atomic_write_text(self._path(provider, account_id), secret, 0o600)

    def delete(self, provider: str, account_id: str) -> bool:
        try:
            os.unlink(self._path(provider, account_id))
        except FileNotFoundError:
            return False
        return True


def _secret_service_works(tool: str) -> bool:
    probe = SecretServiceStore(tool)
    try:
        probe.set("probe", "probe", "ok")
        works = probe.get("probe", "probe") == "ok"
    except SwitcherError:
        return False
    finally:
        with contextlib.suppress(SwitcherError):
            probe.delete("probe", "probe")
    return works


def open_store(config: Config) -> SecretStore:
    """Return the configured store, choosing (and persisting) one on first use."""
    backend = config.settings.secret_backend
    tool = find_program("secret-tool")
    if backend == "auto":
        backend = "secret-service" if tool and _secret_service_works(tool) else "file"
        config.settings.secret_backend = backend
        config.save()
    if backend == "secret-service":
        if not tool:
            raise SwitcherError(
                "secret-tool is not installed (package libsecret / libsecret-tools), "
                "but accounts are stored in the keyring."
            )
        return SecretServiceStore(tool)
    return FileStore()
