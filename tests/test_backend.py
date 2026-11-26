"""Backend tests. Each test runs in a throwaway HOME with the file secret store,
so nothing touches the real keyring, CLIs or network."""

import base64
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from ai_account_switcher import app, claude_desktop, usage  # noqa: E402
from ai_account_switcher.config import Account, Config  # noqa: E402
from ai_account_switcher.providers import ClaudeCode, Codex, parse_claude_usage, parse_codex_usage  # noqa: E402
from ai_account_switcher.secrets import FileStore, open_store  # noqa: E402
from ai_account_switcher.util import SwitcherError, format_duration, window_label  # noqa: E402


def jwt(claims: dict) -> str:
    def enc(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        env = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "XDG_DATA_HOME": str(self.home / ".local/share"),
            "XDG_RUNTIME_DIR": "",
            "PATH": "/nonexistent",
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
            os.environ.pop(var, None)
        self.addCleanup(self.tmp.cleanup)
        cfg = Config()
        cfg.settings.secret_backend = "file"
        cfg.save()
        # No network in tests.
        net = mock.patch("ai_account_switcher.usage.http_json", return_value=(None, None, ""))
        self.http = net.start()
        self.addCleanup(net.stop)

    # ---- helpers that play the part of the CLIs
    def claude_login(self, email, uuid, token="tok", expires_in=3600):
        creds = {"claudeAiOauth": {"accessToken": f"{token}-{email}", "refreshToken": f"r-{email}",
                                   "expiresAt": int((time.time() + expires_in) * 1000),
                                   "subscriptionType": "max", "accountUuid": uuid}}
        (self.home / ".claude").mkdir(exist_ok=True)
        (self.home / ".claude/.credentials.json").write_text(json.dumps(creds))
        state_path = self.home / ".claude.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {"numStartups": 3}
        state["oauthAccount"] = {"accountUuid": uuid, "emailAddress": email,
                                 "organizationName": f"{email} org", "planDisplayName": "Max"}
        state_path.write_text(json.dumps(state))

    def codex_login(self, email, account_id, exp_in=3600):
        auth = {"auth_mode": "chatgpt", "tokens": {
            "id_token": jwt({"email": email, "https://api.openai.com/auth": {"chatgpt_plan_type": "plus",
                                                                              "chatgpt_account_id": account_id}}),
            "access_token": jwt({"exp": time.time() + exp_in}),
            "refresh_token": f"r-{email}", "account_id": account_id}}
        (self.home / ".codex").mkdir(exist_ok=True)
        (self.home / ".codex/auth.json").write_text(json.dumps(auth))

    def live_claude_email(self):
        return json.loads((self.home / ".claude.json").read_text())["oauthAccount"]["emailAddress"]


class ClaudeTests(Sandbox):
    def test_refresh_imports_current_login(self):
        self.claude_login("a@example.com", "uuid-a")
        out = app.refresh()
        claude = next(p for p in out["providers"] if p["id"] == "claude")
        self.assertEqual(claude["active"], "a@example.com")
        self.assertEqual(claude["accounts"][0]["plan"], "Max")
        self.assertEqual(out["events"][0]["type"], "imported")

    def test_switch_round_trip_preserves_rotated_tokens_and_other_state(self):
        self.claude_login("a@example.com", "uuid-a")
        app.refresh()
        self.claude_login("b@example.com", "uuid-b")  # user signs in to B via the CLI
        app.refresh()
        self.assertEqual(len(Config().for_provider("claude")), 2)

        # Claude Code rotates B's token while B is active.
        self.claude_login("b@example.com", "uuid-b", token="rotated")
        app.switch("claude", "a@example.com")
        self.assertEqual(self.live_claude_email(), "a@example.com")
        state = json.loads((self.home / ".claude.json").read_text())
        self.assertEqual(state["numStartups"], 3, "unrelated ~/.claude.json keys must survive")
        self.assertEqual(oct((self.home / ".claude/.credentials.json").stat().st_mode & 0o777), "0o600")

        app.switch("claude", "b@example.com")
        creds = json.loads((self.home / ".claude/.credentials.json").read_text())
        self.assertEqual(creds["claudeAiOauth"]["accessToken"], "rotated-b@example.com")

    def test_manual_cli_login_is_not_saved_under_the_wrong_email(self):
        self.claude_login("a@example.com", "uuid-a")
        app.refresh()
        self.claude_login("c@example.com", "uuid-c")
        out = app.status()
        claude = next(p for p in out["providers"] if p["id"] == "claude")
        self.assertEqual(claude["unsaved_login"], "c@example.com")
        self.assertIsNone(claude["active"])
        app.refresh()
        store = FileStore()
        saved_a = json.loads(store.get("claude", "a@example.com"))
        self.assertIn("a@example.com", saved_a["credentials"])

    def test_cannot_remove_active_account(self):
        self.claude_login("a@example.com", "uuid-a")
        app.refresh()
        with self.assertRaises(SwitcherError):
            app.remove("claude", "a@example.com")

    def test_remove_inactive_account_deletes_secret(self):
        self.claude_login("a@example.com", "uuid-a")
        app.refresh()
        self.claude_login("b@example.com", "uuid-b")
        app.refresh()
        app.remove("claude", "a@example.com")
        self.assertIsNone(FileStore().get("claude", "a@example.com"))
        self.assertEqual([a.id for a in Config().for_provider("claude")], ["b@example.com"])

    def test_add_restores_previous_login_when_cancelled(self):
        self.claude_login("a@example.com", "uuid-a")
        app.refresh()
        provider = ClaudeCode(Config())
        with mock.patch.object(ClaudeCode, "cli", return_value="/bin/true"), \
                mock.patch("ai_account_switcher.providers.run_login_in_terminal", return_value=False):
            self.assertIsNone(provider.add(FileStore()))
        self.assertEqual(self.live_claude_email(), "a@example.com")

    def test_add_saves_new_login(self):
        self.claude_login("a@example.com", "uuid-a")
        app.refresh()

        def fake_login(title, argv, terminal, done, timeout):
            self.assertEqual(argv[1:], ["auth", "login"])
            self.assertFalse((self.home / ".claude/.credentials.json").exists())
            self.claude_login("n@example.com", "uuid-n")
            return done()

        with mock.patch.object(ClaudeCode, "cli", return_value="/bin/true"), \
                mock.patch("ai_account_switcher.providers.run_login_in_terminal", side_effect=fake_login):
            account = ClaudeCode(Config()).add(FileStore())
        self.assertEqual(account.id, "n@example.com")
        self.assertEqual(sorted(a.id for a in Config().for_provider("claude")), ["a@example.com", "n@example.com"])
        self.assertIsNotNone(FileStore().get("claude", "a@example.com"))

    def test_expired_inactive_token_reports_idle_without_network(self):
        self.claude_login("a@example.com", "uuid-a", expires_in=-10)
        snap = ClaudeCode(Config()).read_live().snapshot
        state, _ = ClaudeCode(Config()).fetch_usage(snap, active=False)
        self.assertFalse(state["available"])
        self.assertEqual(state["code"], "expired")
        self.http.assert_not_called()

    def test_corrupt_state_file_is_never_clobbered(self):
        self.claude_login("a@example.com", "uuid-a")
        app.refresh()
        self.claude_login("b@example.com", "uuid-b")
        app.refresh()
        (self.home / ".claude.json").write_text("{not json")
        app.switch("claude", "a@example.com")
        self.assertEqual((self.home / ".claude.json").read_text(), "{not json")


class CodexTests(Sandbox):
    def test_import_and_switch(self):
        self.codex_login("x@example.com", "acct-x")
        app.refresh()
        self.codex_login("y@example.com", "acct-y")
        out = app.refresh()
        codex = next(p for p in out["providers"] if p["id"] == "codex")
        self.assertEqual(codex["active"], "y@example.com")
        self.assertEqual({a["plan"] for a in codex["accounts"]}, {"Plus"})
        app.switch("codex", "x@example.com")
        live = Codex(Config()).read_live()
        self.assertEqual(live.email, "x@example.com")

    def test_active_expired_token_is_never_refreshed(self):
        self.codex_login("x@example.com", "acct-x", exp_in=-100)
        snap = Codex(Config()).read_live().snapshot
        with mock.patch.object(Codex, "refresh") as refresh:
            state, refreshed = Codex(Config()).fetch_usage(snap, active=True)
        refresh.assert_not_called()
        self.assertIsNone(refreshed)
        self.assertEqual(state["code"], "expired")

    def test_keyring_mode_notice(self):
        (self.home / ".codex").mkdir()
        (self.home / ".codex/config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
        self.assertIn("keyring", Codex(Config()).notice())


class UsageTests(unittest.TestCase):
    def test_parse_claude(self):
        state = parse_claude_usage({"five_hour": None, "seven_day": {
            "utilization": 19.0, "resets_at": "2030-12-01T10:59:59.674676+00:00"}})
        self.assertTrue(state["available"])
        self.assertEqual([w["label"] for w in state["windows"]], ["7d"])
        rendered = usage.render(state)
        self.assertIn("7d 19%", rendered["text"])
        self.assertIn("resets in", rendered["text"])

    def test_parse_codex_window_labels(self):
        state = parse_codex_usage({"rate_limit": {
            "primary_window": {"used_percent": 42, "limit_window_seconds": 18000, "reset_after_seconds": 600},
            "secondary_window": {"used_percent": 7, "limit_window_seconds": 604800, "reset_at": time.time() + 86400}}})
        self.assertEqual([w["label"] for w in state["windows"]], ["5h", "7d"])
        self.assertEqual(usage.max_percent(state), 42)

    def test_garbage_is_unavailable(self):
        self.assertFalse(parse_claude_usage({"five_hour": "x"})["available"])
        self.assertFalse(parse_codex_usage({"rate_limit": []})["available"])

    def test_helpers(self):
        self.assertEqual(format_duration(0), "now")
        self.assertEqual(format_duration(3 * 3600 + 120), "3h 2m")
        self.assertEqual(format_duration(2 * 86400 + 3600), "2d 1h")
        self.assertEqual(window_label(18000, "?"), "5h")
        self.assertEqual(window_label(None, "7d"), "7d")


class AutoSwitchTests(Sandbox):
    def test_choose_least_used_non_exhausted(self):
        accounts = [Account("claude", e) for e in ("a@x.io", "b@x.io", "c@x.io", "d@x.io")]
        u = {
            "claude:a@x.io": usage.ok([usage.window("5h", 100, None)]),
            "claude:b@x.io": usage.ok([usage.window("5h", 60, None)]),
            "claude:c@x.io": usage.ok([usage.window("5h", 20, None)]),
            "claude:d@x.io": usage.error("x", "login_required"),
        }
        self.assertEqual(app.choose_target(accounts, "a@x.io", u, 100).id, "c@x.io")
        u["claude:c@x.io"] = usage.ok([usage.window("5h", 100, None)])
        u["claude:b@x.io"] = usage.ok([usage.window("7d", 100, None)])
        self.assertIsNone(app.choose_target(accounts, "a@x.io", u, 100))

    def test_auto_switch_when_active_exhausted(self):
        self.claude_login("a@example.com", "uuid-a")
        app.refresh()
        self.claude_login("b@example.com", "uuid-b")
        app.refresh()
        app.set_auto_switch("claude", True)

        def fake_http(url, headers, timeout=10, data=None):
            pct = 100 if "b@example.com" in headers.get("Authorization", "") else 10
            return 200, {"five_hour": {"utilization": pct, "resets_at": "2030-01-01T00:00:00Z"}}, ""

        self.http.side_effect = fake_http
        out = app.refresh()
        self.assertIn("auto_switched", [e["type"] for e in out["events"]])
        self.assertEqual(self.live_claude_email(), "a@example.com")
        # Cooldown: an immediate second refresh does not flip-flop.
        out = app.refresh()
        self.assertNotIn("auto_switched", [e["type"] for e in out["events"]])


class ClaudeDesktopTests(Sandbox):
    """Uses a fake ``claude-desktop`` (a sleeping Python script). The /proc scan is
    limited to processes this test started, so a real Claude Desktop is never touched."""

    def setUp(self):
        super().setUp()
        import subprocess
        self.subprocess = subprocess
        bindir = self.home / "fakebin" / "claude-desktop"
        bindir.mkdir(parents=True)
        self.fake = bindir / "claude-desktop"
        self.fake.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
        self.fake.chmod(0o755)
        self.children = []
        real_popen = subprocess.Popen

        def tracking_popen(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            self.children.append(proc)
            return proc

        for target, value in (
            ("ai_account_switcher.claude_desktop.subprocess.Popen", tracking_popen),
            ("ai_account_switcher.claude_desktop._candidate_pids", lambda: [c.pid for c in self.children if c.poll() is None]),
            ("ai_account_switcher.claude_desktop.find_program", lambda name, override="": str(self.fake) if name == "claude-desktop" else None),
        ):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._reap)

    def _reap(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait()

    def wait_running(self, name):
        for _ in range(50):
            if name in claude_desktop.running():
                return True
            time.sleep(0.05)
        return False

    def provider(self):
        return next(p for p in app.status()["providers"] if p["id"] == "desktop")

    def test_add_switch_remove_profiles(self):
        (self.home / ".config/Claude").mkdir(parents=True)  # existing default profile
        app.add("desktop", "work")
        self.assertTrue(self.wait_running("work"))
        self.assertEqual((self.home / ".config/Claude-accounts/.current").read_text().strip(), "work")
        desk = self.provider()
        self.assertEqual([a["id"] for a in desk["accounts"]], ["default", "work"])
        self.assertEqual(desk["active"], "work")
        self.assertFalse(next(a for a in desk["accounts"] if a["id"] == "work")["removable"])

        app.switch("desktop", "default")
        self.assertTrue(self.wait_running("default"))
        self.assertNotIn("work", claude_desktop.running(), "the old profile's instance must be stopped")

        with mock.patch("ai_account_switcher.claude_desktop.run") as fake_run:
            def trash(argv, timeout):
                import shutil
                shutil.rmtree(argv[-1] if argv[1] == "trash" else argv[2])
                return self.subprocess.CompletedProcess(argv, 0, "", "")
            fake_run.side_effect = trash
            with mock.patch("ai_account_switcher.claude_desktop.find_program", lambda n, o="": "/usr/bin/" + n):
                app.remove("desktop", "work")
        self.assertEqual([a["id"] for a in self.provider()["accounts"]], ["default"])

    def test_cannot_remove_default_or_open_profile(self):
        (self.home / ".config/Claude").mkdir(parents=True)
        with self.assertRaises(SwitcherError):
            app.remove("desktop", "default")
        app.add("desktop", "work")
        self.assertTrue(self.wait_running("work"))
        with self.assertRaises(SwitcherError):
            app.remove("desktop", "work")

    def test_bad_names_rejected(self):
        for bad in ("", "../x", ".hidden", "a b", "default"):
            with self.assertRaises(SwitcherError):
                app.add("desktop", bad)

    def test_existing_profiles_from_other_tool_are_listed(self):
        root = self.home / ".config/Claude-accounts"
        (root / "personal").mkdir(parents=True)
        (root / ".current").write_text("personal\n")
        desk = self.provider()
        self.assertEqual(desk["active"], "personal")
        self.assertEqual(desk["accounts"][0]["usage"]["text"], "Not open")


class ConfigTests(Sandbox):
    def test_hostile_config_is_tolerated(self):
        path = Config().path
        path.write_text(json.dumps({"settings": {"auto_switch_threshold": "lots", "refresh_minutes": -4,
                                                 "secret_backend": "plaintext", "auto_switch": []},
                                    "accounts": [1, {"provider": "evil", "id": "x"}, {"provider": "claude"}]}))
        cfg = Config()
        self.assertEqual(cfg.accounts, [])
        self.assertEqual(cfg.settings.auto_switch_threshold, 100.0)
        self.assertEqual(cfg.settings.refresh_minutes, 1)
        self.assertEqual(cfg.settings.secret_backend, "auto")

    def test_set_setting_validates(self):
        with self.assertRaises(SwitcherError):
            app.set_setting("auto_switch_threshold", "500")
        app.set_setting("show_usage_in_panel", "off")
        self.assertFalse(Config().settings.show_usage_in_panel)

    def test_auto_backend_falls_back_to_file_without_secret_tool(self):
        cfg = Config()
        cfg.settings.secret_backend = "auto"
        cfg.save()
        with mock.patch("ai_account_switcher.secrets.find_program", return_value=None):
            self.assertEqual(open_store(Config()).name, "file")
        self.assertEqual(Config().settings.secret_backend, "file")

    def test_panel_status_shows_resets_in_brackets_when_available(self):
        now = 1_700_000_000.0
        with_resets = usage.render(usage.ok([
            usage.window("5h", 10, now + 3600 + 600),
            usage.window("7d", 42, now + 2 * 86400 + 3 * 3600),
        ]), now)
        self.assertEqual(app._panel_entry("Claude", with_resets, show_reset=True), "Claude 42% [2d 3h]")
        self.assertEqual(app._panel_entry("Claude", with_resets, show_reset=False), "Claude 42%")

        no_resets = usage.render(usage.ok([usage.window("7d", 15, None)]), now)
        self.assertEqual(app._panel_entry("Codex", no_resets, show_reset=True), "Codex 15%")

        fallback_reset = usage.render(usage.ok([
            usage.window("5h", 10, now + 1800),
            usage.window("7d", 50, None),
        ]), now)
        self.assertEqual(app._panel_entry("Gemini", fallback_reset, show_reset=True), "Gemini 50% [30m]")

        reset_available = usage.render(usage.ok([
            usage.window("5h", 10, now - 10),
            usage.window("7d", 42, now + 86400),
        ]), now)
        self.assertEqual(app._panel_entry("Claude", reset_available, show_reset=True), "Claude 42%* [1d 0h]")
        self.assertEqual(app._panel_entry("Claude", reset_available, show_reset=False), "Claude 42%*")

        app.set_setting("show_reset_in_panel", "off")
        self.assertFalse(Config().settings.show_reset_in_panel)


if __name__ == "__main__":
    unittest.main()
