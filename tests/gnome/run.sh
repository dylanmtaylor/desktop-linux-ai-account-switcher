#!/bin/sh
# Load the GNOME Shell extension in a headless GNOME Shell (Fedora, systemd as PID 1
# so logind exists), seed fake accounts, open the menu and save a screenshot.
#   tests/gnome/run.sh [out-dir]     -> out-dir/menu.png, state.txt, shell.log
# state.txt's first line is the extension state ("1" = active) plus any load error.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
OUT=$(mkdir -p "${1:-$REPO/dist/gnome-test}" && cd "${1:-$REPO/dist/gnome-test}" && pwd)
rm -f "$OUT"/*
podman build --isolation=chroot -q -t aias-gnome-test "$HERE" >/dev/null
podman rm -f aias-gnome-test-run >/dev/null 2>&1 || true
podman run -d --name aias-gnome-test-run --systemd=always --network=none \
  --security-opt label=disable -v "$REPO:/src:ro" -v "$OUT:/out" aias-gnome-test /sbin/init >/dev/null
trap 'podman rm -f aias-gnome-test-run >/dev/null 2>&1' EXIT
sleep 5
podman exec aias-gnome-test-run /src/tests/gnome/in-container.sh >/dev/null 2>&1 || true
cat "$OUT/state.txt"
if grep -q 'JS ERROR' "$OUT/shell.log" && grep 'JS ERROR' "$OUT/shell.log" | grep -q ai-account; then
  echo "extension errors in $OUT/shell.log" >&2; exit 1
fi
head -1 "$OUT/state.txt" | grep -q '"1 "' || { echo "extension did not load" >&2; exit 1; }
echo "screenshot: $OUT/menu.png"
