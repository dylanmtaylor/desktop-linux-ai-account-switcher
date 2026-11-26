# Desktop Linux AI Account Switcher

Switch between multiple **Claude Code** and **Codex CLI** accounts, and between **Claude Desktop** profiles, from your Linux desktop panel. See each account's email, usage, and when its limits reset. If you use **Antigravity (`agy`)**, your Gemini subscription usage shows up too.

It ships as:

- a **GNOME Shell extension** (GNOME 50 and later)
- a **KDE Plasma 6 widget**, for the panel or the system tray
- an **`ai-account-switcher` CLI**, which both panel apps use and which also works on its own

## Features

- **One-click switching** between saved Claude Code accounts and, separately, saved Codex accounts.
- **Usage at a glance:** 5-hour and weekly usage for every saved account, with a progress bar and a countdown to the next reset ("resets in 2h 10m (Tue 14:00)"). The panel shows the active accounts' usage, e.g. `Claude 42% · Codex 12% · Gemini 33%`.
- **Easy add and remove:** *Add account…* opens a terminal running the CLI's own login (`claude auth login` / `codex login`) and saves the new account when sign-in finishes. If you cancel, your previous account is restored. Each inactive account has a remove button, and removal asks for confirmation.
- **Auto-switch (optional, per provider):** when the active account hits its limit, it moves to the least-used saved account that still has room.
- **Claude Desktop profiles:** each profile is its own Claude Desktop data folder. Switching quits Claude Desktop and reopens it on the other profile, so each profile stays signed in. Uses the same `~/.config/Claude-accounts/` layout as [claude-desktop-switcher](https://github.com/Adam-Zaghloul/claude-desktop-switcher), so profiles you already made there appear here. Removing a profile moves its folder to the Trash.
- **App launcher:** *AI Account Switcher* in your application menu opens the top-bar menu on GNOME, or the widget in a window on KDE.
- **Gemini via Antigravity (read-only):** shows the Google account signed in to `agy` and its Gemini and Claude/GPT quota buckets, read from `agy`'s local `/usage` command.
- **Saved logins live in your keyring** (GNOME Keyring or KWallet, through the Secret Service), not in plain config files.

## Install

Requirements: Python 3.10+, `secret-tool` (package `libsecret` / `libsecret-tools`; optional but recommended), and `claude` and/or `codex` on your machine.

```sh
git clone https://github.com/dylanmtaylor/desktop-linux-ai-account-switcher.git
cd desktop-linux-ai-account-switcher
./install.sh          # backend + the panel app for your current desktop
./install.sh --gnome  # or force GNOME / --kde / --all / --backend
```

- **GNOME:** on Wayland, log out and back in once, then check that the extension is enabled (`gnome-extensions enable ai-account-switcher@dylanmtaylor.github.io`).
- **KDE Plasma:** right-click the panel → *Add or Manage Widgets* → **AI Account Switcher**, or put it in the system tray instead: *System Tray Settings → Entries → AI Account Switcher → Always shown*.

To uninstall, run `./uninstall.sh`. Add `--purge` to also delete saved logins and settings.

## CLI

```sh
ai-account-switcher status                       # accounts + last known usage (no network)
ai-account-switcher refresh                      # save current logins, fetch usage, auto-switch
ai-account-switcher add claude                   # sign in to another account in a terminal
ai-account-switcher switch claude you@work.com
ai-account-switcher remove codex old@example.com
ai-account-switcher add desktop --name work      # new Claude Desktop profile
ai-account-switcher switch desktop default
ai-account-switcher open                         # what the app launcher runs
ai-account-switcher auto-switch claude on
ai-account-switcher set show_usage_in_panel off
```

Add `--json` for machine-readable output and `--notify` for desktop notifications.

Other settings: `auto_switch_threshold` (1–100), `refresh_minutes`, `terminal` (e.g. `"konsole -e"`), `claude_config_dir`, `claude_command`, `codex_command`, `desktop_command` (defaults to `claude-desktop`; may include arguments, e.g. `flatpak run <app-id>`), and `secret_backend` (`secret-service` or `file`).

## How it works

| | Active login (what the CLI reads) | Saved accounts |
|---|---|---|
| Claude Code | `~/.claude/.credentials.json` + `oauthAccount` in `~/.claude.json` (or inside `$CLAUDE_CONFIG_DIR`) | Secret Service item `application=ai-account-switcher provider=claude account=<email>` |
| Codex | `~/.codex/auth.json` (or `$CODEX_HOME`) | `… provider=codex …` |
| Claude Desktop | the Electron data folder: `~/.config/Claude` ("default") or `~/.config/Claude-accounts/<name>` via `--user-data-dir` | the folders themselves (the session is encrypted to its folder, so it is never copied) |
| Gemini (agy) | `agy`'s own keyring entry, read only for the email | not saved, status only |

- **Switching:** the switcher first re-saves the current login, so tokens the CLI has refreshed since are never lost. It then writes the target account's saved login back, atomically and with `0600` permissions. Other keys in `~/.claude.json` are left untouched.
- **Account matching:** accounts are matched by account ID, so a login you made by hand with the CLI is never saved under the wrong email.
- **Running sessions:** they keep the account they started with. Restart them to pick up a switch; the notification says how many are running.
- **Usage sources:** Claude usage comes from the OAuth usage endpoint, Codex usage from the ChatGPT backend, and Gemini usage from `agy -p /usage --output-format json`. That `agy` command answers locally and spends no tokens. If an `agy` build ever answers it with a model turn instead, the switcher stops calling it until `agy` is updated.
- **Codex tokens:** the active Codex login is never refreshed by the switcher, since a running `codex` holds the same refresh token. Saved inactive logins are refreshed when needed.
- **Architecture:** the panel apps are thin. They run the backend asynchronously and draw its JSON, so the shell never blocks on disk, keyring or network.

### Codex keyring mode

If Codex is set to store credentials in the keyring (`cli_auth_credentials_store = "keyring"`), there is no file to switch. Set `cli_auth_credentials_store = "file"` in `~/.codex/config.toml`, or use *Add Codex account*, which signs in with file storage.

## Development

```sh
python3 -m unittest discover -s tests   # backend tests (sandboxed HOME, no network)
./bin/ai-account-switcher status         # run the backend from the checkout
plasmawindowed io.github.dylanmtaylor.aiaccountswitcher   # preview the Plasma widget
```

## Credits

Inspired by [Symbioose/claude-account-switcher](https://github.com/Symbioose/claude-account-switcher) (a macOS menu bar app for Claude Code and Codex) and [Adam-Zaghloul/claude-desktop-switcher](https://github.com/Adam-Zaghloul/claude-desktop-switcher) (Claude Desktop profiles on Linux), both MIT-licensed. This is an independent Linux implementation.

Not affiliated with Anthropic, OpenAI or Google.

## License

Licensed under either of

- Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE) or <https://www.apache.org/licenses/LICENSE-2.0>)
- MIT license ([LICENSE-MIT](LICENSE-MIT) or <https://opensource.org/licenses/MIT>)

at your option.

Unless you explicitly state otherwise, any contribution intentionally submitted for inclusion in this project by you, as defined in the Apache-2.0 license, shall be dual-licensed as above, without any additional terms or conditions.
