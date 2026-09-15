#!/bin/sh
set -eu
if [ "${SOTTO_WAYLAND_TEST_X11:-}" != 1 ]; then
    exec xvfb-run -a env SOTTO_WAYLAND_TEST_X11=1 sh "$0"
fi
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"
test_runtime=$(mktemp -d)
chmod 700 "$test_runtime"
export XDG_RUNTIME_DIR="$test_runtime"
export WAYLAND_DISPLAY=sotto-test
export GDK_BACKEND=wayland
export PYTHONPATH="$project_dir/Linux"
weston --backend=x11 --renderer=pixman --socket="$WAYLAND_DISPLAY" --idle-time=0 \
    --width=1280 --height=800 --no-config --log="$test_runtime/weston.log" &
weston_pid=$!
trap 'kill "$weston_pid" 2>/dev/null || true; wait "$weston_pid" 2>/dev/null || true; rm -rf "$test_runtime"' EXIT
attempt=0
until [ -S "$test_runtime/$WAYLAND_DISPLAY" ]; do
    attempt=$((attempt + 1))
    if [ "$attempt" -gt 50 ]; then
        cat "$test_runtime/weston.log"
        exit 1
    fi
    sleep 0.1
done
dbus-run-session -- /usr/bin/python3 -m unittest discover -s Linux/tests -p test_accessibility.py -v
