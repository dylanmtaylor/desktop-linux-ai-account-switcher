#!/bin/sh
# Remove AI Account Switcher. Saved logins and settings stay unless you pass --purge.
set -eu

DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
UUID="ai-account-switcher@dylanmtaylor.github.io"
PLASMOID="io.github.dylanmtaylor.aiaccountswitcher"

command -v gnome-extensions >/dev/null && gnome-extensions disable "$UUID" 2>/dev/null || true
rm -rf "$DATA/gnome-shell/extensions/$UUID"
command -v kpackagetool6 >/dev/null && kpackagetool6 -t Plasma/Applet -r "$PLASMOID" >/dev/null 2>&1 || true
rm -f "$HOME/.local/bin/ai-account-switcher"
rm -rf "$DATA/ai-account-switcher/backend"
echo "Removed the panel apps and the backend."

if [ "${1:-}" = "--purge" ]; then
  if command -v secret-tool >/dev/null; then
    secret-tool clear application ai-account-switcher 2>/dev/null || true
  fi
  rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/ai-account-switcher" \
         "${XDG_CACHE_HOME:-$HOME/.cache}/ai-account-switcher" \
         "$DATA/ai-account-switcher"
  echo "Deleted saved logins and settings. Your active Claude Code and Codex logins were not touched."
else
  echo "Saved logins and settings were kept. Run with --purge to delete them too."
fi
