#!/bin/bash
# Runs inside the container: fake accounts, headless GNOME Shell, open menu, screenshot.
set -u
export HOME=/home/tester XDG_RUNTIME_DIR=/tmp/xdg
mkdir -p $HOME $XDG_RUNTIME_DIR && chmod 700 $XDG_RUNTIME_DIR
cd /src && ./install.sh --gnome >/dev/null 2>&1 || ./install.sh --backend
mkdir -p $HOME/.local/share/gnome-shell/extensions && cp -r /src/gnome-extension/ai-account-switcher@dylanmtaylor.github.io $HOME/.local/share/gnome-shell/extensions/
python3 /src/tests/gnome/seed.py
dbus-run-session -- bash -c '
  gsettings set org.gnome.shell disable-user-extensions false
  gsettings set org.gnome.shell enabled-extensions "[\"ai-account-switcher@dylanmtaylor.github.io\"]"
  gnome-shell --version
  gnome-shell --headless --wayland --no-x11 --virtual-monitor 1280x800 --unsafe-mode > /out/shell.log 2>&1 &
  for i in $(seq 1 40); do gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell --method org.gnome.Shell.Eval "1" >/dev/null 2>&1 && break; sleep 1; done
  sleep 6
  python3 /src/tests/gnome/seed.py >/dev/null 2>&1
  gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell --method org.gnome.Shell.Eval "Main.extensionManager.lookup(\"ai-account-switcher@dylanmtaylor.github.io\")?.state + \" \" + (Main.extensionManager.lookup(\"ai-account-switcher@dylanmtaylor.github.io\")?.error ?? \"\")" > /out/state.txt 2>&1
  PATH=$HOME/.local/bin:$PATH ai-account-switcher --json open | python3 -c "import json,sys; print(\"open via launcher:\", json.load(sys.stdin)[\"opened\"])" >> /out/state.txt 2>&1
  sleep 3
  gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell --method org.gnome.Shell.Eval "const m = Main.panel.statusArea[\"ai-account-switcher@dylanmtaylor.github.io\"].menu; const items = m._getMenuItems().filter(i => i._ornamentIcon); JSON.stringify(items.map(i => [i._ornament, i._ornamentIcon.icon_name, i._ornamentIcon.visible, i._ornamentIcon.width, i._ornamentIcon.mapped]))" >> /out/state.txt 2>&1
  gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell/Screenshot --method org.gnome.Shell.Screenshot.Screenshot false false /out/menu.png >> /out/state.txt 2>&1
  kill %1
'
