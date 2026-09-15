#!/bin/sh
set -eu
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
data_dir="${XDG_DATA_HOME:-$HOME/.local/share}"
bin_dir="$HOME/.local/bin"
install -d "$data_dir/sotto/sotto_linux" "$data_dir/applications" "$bin_dir"
install -m 644 "$source_dir"/sotto_linux/*.py "$data_dir/sotto/sotto_linux/"
cat > "$bin_dir/sotto-linux" <<'LAUNCHER'
#!/bin/sh
set -eu
export PYTHONPATH="${XDG_DATA_HOME:-$HOME/.local/share}/sotto${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 -m sotto_linux "$@"
LAUNCHER
chmod 755 "$bin_dir/sotto-linux"
# sh resolves HOME at launch, avoiding unescaped paths in the desktop format.
cat > "$data_dir/applications/io.github.vitor_hbr.Sotto.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Sotto
Comment=Native Linux dictation
Exec=sh -c "exec \"\$HOME/.local/bin/sotto-linux\""
Icon=audio-input-microphone
Terminal=false
Categories=AudioVideo;Audio;
StartupNotify=true
DESKTOP
printf 'Installed Sotto. Launch it from Applications or %s/sotto-linux\n' "$bin_dir"
