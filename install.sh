#!/bin/sh
# Install AI Account Switcher for the current user.
#   ./install.sh            backend + the panel app for the current desktop
#   ./install.sh --gnome    backend + GNOME Shell extension
#   ./install.sh --kde      backend + Plasma widget
#   ./install.sh --all      backend + both
#   ./install.sh --backend  backend only (CLI)
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
BIN="$HOME/.local/bin"
UUID="ai-account-switcher@dylanmtaylor.github.io"
PLASMOID="io.github.dylanmtaylor.aiaccountswitcher"

want_gnome=0
want_kde=0
case "${1:-auto}" in
  --gnome) want_gnome=1 ;;
  --kde) want_kde=1 ;;
  --all) want_gnome=1; want_kde=1 ;;
  --backend) ;;
  auto)
    case "${XDG_CURRENT_DESKTOP:-}" in
      *GNOME*|*gnome*) want_gnome=1 ;;
      *KDE*|*kde*|*Plasma*) want_kde=1 ;;
      *) echo "Desktop not detected; installing the backend only. Use --gnome or --kde." ;;
    esac ;;
  -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
  *) echo "Unknown option: $1" >&2; exit 2 ;;
esac

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "Python 3.10 or newer is required" >&2; exit 1; }
command -v secret-tool >/dev/null || echo "Note: secret-tool (libsecret) not found; saved logins will use 0600 files instead of the keyring."

# ---- backend
mkdir -p "$DATA/ai-account-switcher" "$BIN"
rm -rf "$DATA/ai-account-switcher/backend"
cp -r "$HERE/backend" "$DATA/ai-account-switcher/backend"
find "$DATA/ai-account-switcher/backend" -name __pycache__ -type d -prune -exec rm -rf {} +
cat > "$BIN/ai-account-switcher" <<EOF
#!/bin/sh
exec python3 -I -c 'import sys; sys.path.insert(0, "$DATA/ai-account-switcher/backend"); from ai_account_switcher.cli import main; sys.exit(main(sys.argv[1:]))' "\$@"
EOF
chmod 755 "$BIN/ai-account-switcher"
echo "Installed backend: $BIN/ai-account-switcher"

# ---- GNOME Shell extension
if [ "$want_gnome" = 1 ]; then
  dest="$DATA/gnome-shell/extensions/$UUID"
  rm -rf "$dest"
  mkdir -p "$(dirname "$dest")"
  cp -r "$HERE/gnome-extension/$UUID" "$dest"
  if command -v gnome-extensions >/dev/null && gnome-extensions enable "$UUID" 2>/dev/null; then
    echo "Installed and enabled the GNOME extension."
  else
    echo "Installed the GNOME extension. Enable it with: gnome-extensions enable $UUID"
  fi
  echo "On Wayland, log out and back in once so GNOME Shell loads it."
fi

# ---- Plasma widget
if [ "$want_kde" = 1 ]; then
  if command -v kpackagetool6 >/dev/null; then
    if kpackagetool6 -t Plasma/Applet -s "$PLASMOID" >/dev/null 2>&1; then
      kpackagetool6 -t Plasma/Applet -u "$HERE/kde-plasmoid/$PLASMOID" >/dev/null
    else
      kpackagetool6 -t Plasma/Applet -i "$HERE/kde-plasmoid/$PLASMOID" >/dev/null
    fi
    echo "Installed the Plasma widget. Right-click the panel > Add or Manage Widgets > AI Account Switcher."
    echo "(After upgrading, run: systemctl --user restart plasma-plasmashell)"
  else
    echo "kpackagetool6 not found; is this Plasma 6?" >&2
    exit 1
  fi
fi

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "Note: $BIN is not on your PATH; the panel apps find it anyway." ;;
esac
