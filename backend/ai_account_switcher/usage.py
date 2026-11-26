"""Provider-independent usage state, rendering and a tiny HTTP helper."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime

from ai_account_switcher import __version__
from ai_account_switcher.util import format_duration

USER_AGENT = f"ai-account-switcher/{__version__}"


def window(label: str, percent: object, resets_at: float | None) -> dict | None:
    try:
        value = float(percent)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return {"label": label, "percent": max(0.0, min(value, 100.0)), "resets_at": resets_at}


def ok(windows: list[dict]) -> dict:
    if not windows:
        return error("No usage data returned", "empty")
    return {"available": True, "windows": windows, "fetched_at": time.time()}


def error(message: str, code: str = "unavailable") -> dict:
    return {"available": False, "message": message, "code": code, "fetched_at": time.time()}


def max_percent(state: dict | None) -> float | None:
    if not state or not state.get("available"):
        return None
    values = [w.get("percent") for w in state.get("windows", []) if isinstance(w, dict)]
    numbers = [v for v in values if isinstance(v, (int, float))]
    return max(numbers) if numbers else None


def is_exhausted(state: dict | None, threshold: float) -> bool:
    peak = max_percent(state)
    return peak is not None and peak >= threshold


def parse_iso(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def render(state: dict | None, now: float | None = None) -> dict | None:
    """Usage as the front ends display it, with reset countdowns computed *now*."""
    if not state:
        return None
    now = time.time() if now is None else now
    fetched = state.get("fetched_at")
    age = int(now - fetched) if isinstance(fetched, (int, float)) else None
    if not state.get("available"):
        message = str(state.get("message") or "Usage unavailable")
        return {"available": False, "text": message, "message": message,
                "code": state.get("code"), "windows": [], "max_percent": None, "age_seconds": age}
    windows, parts = [], []
    for w in state.get("windows", []):
        if not isinstance(w, dict) or not isinstance(w.get("percent"), (int, float)):
            continue
        resets_at = w.get("resets_at")
        resets_in = format_duration(resets_at - now) if isinstance(resets_at, (int, float)) else None
        reset_clock = (
            datetime.fromtimestamp(resets_at).strftime("%a %H:%M")
            if isinstance(resets_at, (int, float)) else None
        )
        label = str(w.get("label") or "")
        windows.append({"label": label, "percent": round(w["percent"], 1),
                        "resets_at": resets_at, "resets_in": resets_in, "resets_at_local": reset_clock})
        parts.append(f"{label} {w['percent']:.0f}%" + (f" · resets in {resets_in}" if resets_in else ""))
    return {"available": True, "text": "   ".join(parts), "message": None, "code": None,
            "windows": windows, "max_percent": max_percent(state), "age_seconds": age}


def http_json(
    url: str,
    headers: dict[str, str],
    timeout: float = 10,
    data: bytes | None = None,
) -> tuple[int | None, dict | None, str]:
    """Returns ``(status, json_body_or_None, raw_text)``; status None on network failure."""
    request = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", USER_AGENT)
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read(2_000_000)
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read(2_000_000)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None, None, ""
    text = raw.decode("utf-8", "replace")
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        body = None
    return status, body if isinstance(body, dict) else None, text
