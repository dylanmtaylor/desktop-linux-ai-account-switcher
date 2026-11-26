"""Operations shared by the CLI, the GNOME extension and the Plasma widget."""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict

from ai_account_switcher import __version__, claude_desktop, gemini, usage
from ai_account_switcher.config import PROVIDERS, SETTABLE, Account, Config
from ai_account_switcher.desktop import notify
from ai_account_switcher.providers import Provider, all_providers
from ai_account_switcher.secrets import SecretStore, open_store
from ai_account_switcher.util import (
    SwitcherError,
    atomic_write_text,
    cache_dir,
    ensure_private_dir,
    file_lock,
    pid_alive,
    read_json,
)

AUTO_SWITCH_COOLDOWN = 120  # seconds between auto-switch attempts per provider


# --------------------------------------------------------------------------- runtime state


def _state_path():
    return cache_dir() / "state.json"


def load_state() -> dict:
    data = read_json(_state_path()) or {}
    for key in ("usage", "last_auto_switch", "pending"):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    return data


@contextmanager
def edit_state():
    with file_lock("state", timeout=10):
        state = load_state()
        yield state
        ensure_private_dir(cache_dir())
        atomic_write_text(_state_path(), json.dumps(state, indent=1), 0o600)


def ops_lock():
    return file_lock("ops", timeout=60)


def _provider(config: Config, provider_id: str) -> Provider:
    for provider in all_providers(config):
        if provider.id == provider_id:
            return provider
    raise SwitcherError(f"Unknown provider {provider_id!r}; use one of: {', '.join(PROVIDERS)}.")


# --------------------------------------------------------------------------- status


def _panel_entry(name: str, rendered: dict | None, show_reset: bool = True) -> str | None:
    if not rendered or rendered.get("max_percent") is None:
        return None
    windows = [w for w in rendered.get("windows", []) if isinstance(w, dict)]
    peak = rendered["max_percent"]
    reset_available = any(w.get("resets_in") == "now" for w in windows)
    star = "*" if reset_available else ""
    reset = ""
    if show_reset:
        peak_with_reset = [
            w for w in windows
            if isinstance(w.get("percent"), (int, float))
            and w["percent"] >= round(peak, 1)
            and w.get("resets_in")
        ]
        candidates = peak_with_reset or [w for w in windows if w.get("resets_in")]
        if candidates:
            chosen = min(
                candidates,
                key=lambda w: w["resets_at"] if isinstance(w.get("resets_at"), (int, float)) else float("inf"),
            )
            reset = f" [{chosen['resets_in']}]"
    return f"{name} {peak:.0f}%{star}{reset}"


def status(config: Config | None = None) -> dict:
    """Everything the front ends draw. Cheap: reads files only (no keyring, no network)."""
    config = config or Config()
    state = load_state()
    now = time.time()
    providers_out, panel_parts, tooltip = [], [], []
    for provider in all_providers(config):
        active, live = provider.live_account()
        pending = state["pending"].get(provider.id)
        if not (isinstance(pending, dict) and isinstance(pending.get("pid"), int) and pid_alive(pending["pid"])):
            pending = None
        accounts = []
        for account in config.for_provider(provider.id):
            rendered = usage.render(state["usage"].get(account.key), now)
            is_active = active is not None and active.id == account.id
            accounts.append({
                "id": account.id, "email": account.id, "plan": account.plan, "org": account.org,
                "active": is_active, "has_credentials": account.has_credentials, "usage": rendered,
            })
            if is_active:
                entry = _panel_entry(provider.label.split()[0], rendered, config.settings.show_reset_in_panel)
                if entry:
                    panel_parts.append(entry)
                tooltip.append(f"{provider.label}: {account.id}"
                               + (f" ({rendered['text']})" if rendered else ""))
        providers_out.append({
            "id": provider.id,
            "label": provider.label,
            "cli_found": provider.cli() is not None,
            "active": active.id if active else None,
            "unsaved_login": (live.email or "unknown account") if live and not active else None,
            "manageable": True,
            "auto_switch": bool(config.settings.auto_switch.get(provider.id)),
            "notice": provider.notice(),
            "pending": pending,
            "accounts": accounts,
        })
    desk = claude_desktop.status_entry(config.settings)
    if desk:
        providers_out.append(desk)
    gem = gemini.status_entry(state)
    if gem:
        providers_out.append(gem)
        for account in gem["accounts"]:
            rendered = account.get("usage")
            entry = _panel_entry("Gemini", rendered, config.settings.show_reset_in_panel)
            if entry:
                panel_parts.append(entry)
            tooltip.append(f"{gem['label']}: {account['email']}" + (f" ({rendered['text']})" if rendered else ""))
    return {
        "ok": True,
        "version": __version__,
        "settings": asdict(config.settings),
        "providers": providers_out,
        "panel": {
            "text": " · ".join(panel_parts) if config.settings.show_usage_in_panel else "",
            "tooltip": "\n".join(tooltip) or "No active accounts",
        },
        "last_refresh": state.get("last_refresh"),
    }


# --------------------------------------------------------------------------- refresh & auto-switch


def _fetch_all(config: Config, store: SecretStore, providers: list[Provider]) -> dict[str, dict]:
    jobs, results = [], {}
    for provider in providers:
        active, _ = provider.live_account()
        for account in config.for_provider(provider.id):
            try:
                snapshot = store.get(provider.id, account.id)
            except SwitcherError as exc:
                results[account.key] = usage.error(str(exc), "keyring")
                continue
            if not snapshot:
                account.has_credentials = False
                results[account.key] = usage.error("Saved login missing; add the account again", "missing")
                continue
            account.has_credentials = True
            jobs.append((provider, account, snapshot, active is not None and active.id == account.id))
    config.save()

    def fetch(job):
        provider, account, snapshot, is_active = job
        try:
            return job, *provider.fetch_usage(snapshot, is_active)
        except Exception as exc:  # one bad account must never break the menu
            return job, usage.error(f"Usage check failed: {exc}", "error"), None

    with ThreadPoolExecutor(max_workers=6) as pool:
        for (provider, account, _snapshot, is_active), state, refreshed in pool.map(fetch, jobs):
            results[account.key] = state
            if refreshed and not is_active:
                with ops_lock():
                    current, _ = provider.live_account()
                    if not current or current.id != account.id:
                        store.set(provider.id, account.id, refreshed)
    return results


def choose_target(accounts: list[Account], active_id: str, usage_by_key: dict, threshold: float) -> Account | None:
    """Least-used saved account that is not exhausted; accounts with unknown usage come last."""
    known, unknown = [], []
    for account in accounts:
        if account.id == active_id or not account.has_credentials:
            continue
        state = usage_by_key.get(account.key)
        if state and state.get("available"):
            if not usage.is_exhausted(state, threshold):
                known.append((usage.max_percent(state) or 0.0, account))
        elif not (state and state.get("code") in {"login_required", "missing"}):
            unknown.append(account)
    if known:
        return min(known, key=lambda item: item[0])[1]
    return unknown[0] if unknown else None


def _auto_switch(config: Config, store: SecretStore, providers: list[Provider], usage_by_key: dict) -> list[dict]:
    events = []
    threshold = config.settings.auto_switch_threshold
    for provider in providers:
        if not config.settings.auto_switch.get(provider.id):
            continue
        active, _ = provider.live_account()
        if not active or not usage.is_exhausted(usage_by_key.get(active.key), threshold):
            continue
        with edit_state() as state:
            last = state["last_auto_switch"].get(provider.id, 0)
            if time.time() - (last if isinstance(last, (int, float)) else 0) < AUTO_SWITCH_COOLDOWN:
                continue
            state["last_auto_switch"][provider.id] = time.time()
        target = choose_target(config.for_provider(provider.id), active.id, usage_by_key, threshold)
        if not target:
            events.append({"type": "limit_reached", "provider": provider.id, "account": active.id,
                           "message": f"{provider.label}: {active.id} hit its limit and no other account has room."})
            continue
        try:
            with ops_lock():
                provider.switch(store, target.id)
        except SwitcherError as exc:
            events.append({"type": "error", "provider": provider.id, "message": str(exc)})
            continue
        events.append({"type": "auto_switched", "provider": provider.id, "account": target.id,
                       "message": f"{provider.label}: {active.id} hit its limit, switched to {target.id}."})
    return events


def _refresh_gemini() -> None:
    """agy can take seconds, so run it on a copy of the state and merge afterwards."""
    scratch = load_state()
    gemini.refresh_into(scratch)
    with edit_state() as state:
        for key in ("gemini", "agy_usage_unsupported"):
            if key in scratch:
                state[key] = scratch[key]
            else:
                state.pop(key, None)


def refresh(auto_switch: bool = True) -> dict:
    """Save the CLIs' current logins, fetch usage for every account, maybe auto-switch."""
    config = Config()
    first_run = not config.exists
    providers = all_providers(config)
    events: list[dict] = []
    needs_store = any(p.read_live() for p in providers) or bool(config.accounts)
    if needs_store:
        store = open_store(config)
        with ops_lock():
            for provider in providers:
                known = {a.id for a in config.for_provider(provider.id)}
                try:
                    account = provider.capture(store)
                except SwitcherError as exc:
                    events.append({"type": "error", "provider": provider.id, "message": str(exc)})
                    continue
                if account and account.id not in known:
                    events.append({"type": "imported", "provider": provider.id, "account": account.id,
                                   "message": f"Saved {provider.label} account {account.id}."
                                   + (" Add more accounts from the menu." if first_run else "")})
        results = _fetch_all(config, store, providers)
        keys = {a.key for a in config.accounts}
        with edit_state() as state:
            state["usage"] = {k: v for k, v in {**state["usage"], **results}.items() if k in keys}
            state["last_refresh"] = time.time()
        if auto_switch:
            events += _auto_switch(config, store, providers, results)
    elif first_run:
        config.save()
    _refresh_gemini()
    out = status(Config())
    out["events"] = events
    return out


# --------------------------------------------------------------------------- account actions


def switch(provider_id: str, account_id: str) -> dict:
    if provider_id == claude_desktop.PROVIDER:
        config = Config()
        result = claude_desktop.switch(config.settings, account_id)
        out = status(config)
        out["events"] = [{"type": "switched", "provider": provider_id, "account": account_id,
                          "message": f"Opening Claude Desktop with the {account_id} profile."}] if result["changed"] else []
        return out
    config = Config()
    provider = _provider(config, provider_id)
    store = open_store(config)
    with ops_lock():
        result = provider.switch(store, account_id)
    events = []
    if result["changed"]:
        message = f"{provider.label} now uses {account_id}."
        running = result.get("running_sessions", 0)
        if running:
            message += f" Restart the {running} running {provider.program} session(s) to pick it up."
        events.append({"type": "switched", "provider": provider_id, "account": account_id, "message": message})
    out = status(config)
    out["events"] = events
    return out


def add(provider_id: str, name: str | None = None) -> dict:
    if provider_id == claude_desktop.PROVIDER:
        if not name:
            raise SwitcherError("Give the new Claude Desktop profile a name (--name).")
        config = Config()
        result = claude_desktop.add(config.settings, name)
        out = status(config)
        verb = "Created" if result.get("created") else "Opened"
        out["events"] = [{"type": "added", "provider": provider_id, "account": name,
                          "message": f"{verb} Claude Desktop profile {name}. Sign in in the window that opens."}]
        return out
    config = Config()
    provider = _provider(config, provider_id)
    store = open_store(config)
    with edit_state() as state:
        pending = state["pending"].get(provider_id)
        if isinstance(pending, dict) and isinstance(pending.get("pid"), int) and pid_alive(pending["pid"]):
            raise SwitcherError(f"A {provider.label} login is already in progress.")
        state["pending"][provider_id] = {"pid": os.getpid(), "started": time.time(), "op": "login"}
    try:
        account = provider.add(store, lock=ops_lock)
    finally:
        with edit_state() as state:
            state["pending"].pop(provider_id, None)
    if account:
        event = {"type": "added", "provider": provider_id, "account": account.id,
                 "message": f"Added {provider.label} account {account.id}."}
    else:
        event = {"type": "cancelled", "provider": provider_id,
                 "message": f"{provider.label} login was cancelled; the previous account is still active."}
    out = refresh(auto_switch=False)
    out["events"] = [event, *out["events"]]
    return out


def remove(provider_id: str, account_id: str) -> dict:
    if provider_id == claude_desktop.PROVIDER:
        claude_desktop.remove(account_id)
        out = status()
        out["events"] = [{"type": "removed", "provider": provider_id, "account": account_id,
                          "message": f"Moved Claude Desktop profile {account_id} to the trash."}]
        return out
    config = Config()
    provider = _provider(config, provider_id)
    if not config.find(provider_id, account_id):
        raise SwitcherError(f"No saved {provider.label} account {account_id}.")
    store = open_store(config)
    with ops_lock():
        provider.remove(store, account_id)
    with edit_state() as state:
        state["usage"].pop(f"{provider_id}:{account_id}", None)
    out = status(config)
    out["events"] = [{"type": "removed", "provider": provider_id, "account": account_id,
                      "message": f"Removed {provider.label} account {account_id} from this computer."}]
    return out


def set_auto_switch(provider_id: str, enabled: bool) -> dict:
    config = Config()
    _provider(config, provider_id)
    config.settings.auto_switch[provider_id] = enabled
    config.save()
    return status(config)


def set_setting(key: str, value: str) -> dict:
    parser = SETTABLE.get(key)
    if parser is None:
        raise SwitcherError(f"Unknown setting {key!r}; settable: {', '.join(sorted(SETTABLE))}.")
    try:
        parsed = parser(value)
    except ValueError as exc:
        raise SwitcherError(f"Invalid value for {key}: {exc}") from exc
    config = Config()
    setattr(config.settings, key, parsed)
    config.save()
    return status(config)


def send_notifications(events: list[dict]) -> None:
    titles = {"switched": "Account switched", "auto_switched": "Auto-switched account",
              "added": "Account added", "removed": "Account removed", "imported": "Account saved",
              "cancelled": "Login cancelled", "limit_reached": "Usage limit reached", "error": "Problem"}
    for event in events:
        notify(titles.get(event.get("type", ""), "AI Account Switcher"), event.get("message", ""),
               urgent=event.get("type") in {"limit_reached", "error"})
