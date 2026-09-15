#!/bin/sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"
mkdir -p build
# Stage on a POSIX filesystem: a checkout on WSL's Windows mount may report
# every directory as 0777, which Debian correctly rejects for control metadata.
package_dir=$(mktemp -d "${TMPDIR:-/tmp}/sotto-package.XXXXXX")
trap 'rm -rf "$package_dir"' EXIT
install -d "$package_dir/DEBIAN" "$package_dir/usr/lib/sotto/sotto_linux" \
    "$package_dir/usr/bin" "$package_dir/usr/share/applications" "$package_dir/usr/share/doc/sotto-linux"
install -m 644 Linux/sotto_linux/*.py "$package_dir/usr/lib/sotto/sotto_linux/"
install -m 644 LICENSE "$package_dir/usr/share/doc/sotto-linux/copyright"
install -m 644 Linux/README.md "$package_dir/usr/share/doc/sotto-linux/README.md"
cat > "$package_dir/DEBIAN/control" <<'CONTROL'
Package: sotto-linux
Version: 0.2.0
Section: sound
Priority: optional
Architecture: all
Maintainer: Sotto Linux contributors
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0, gir1.2-gst-plugins-base-1.0, gir1.2-atspi-2.0, at-spi2-core, gir1.2-secret-1, gstreamer1.0-plugins-good, gstreamer1.0-pulseaudio
Recommends: xdg-desktop-portal, xdg-desktop-portal-gnome, gnome-keyring
Homepage: https://github.com/vitor-hbr/sotto
Description: Native Linux desktop client for Sotto dictation
 Hold-to-talk, guarded text insertion, microphone capture, and shared history.
 Requires a separately running Sotto model server.
CONTROL
cat > "$package_dir/usr/bin/sotto-linux" <<'LAUNCHER'
#!/bin/sh
export PYTHONPATH="/usr/lib/sotto${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 -m sotto_linux "$@"
LAUNCHER
chmod 755 "$package_dir/usr/bin/sotto-linux"
cat > "$package_dir/usr/share/applications/io.github.vitor_hbr.Sotto.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Sotto
Comment=Native Linux dictation
Exec=/usr/bin/sotto-linux
Icon=audio-input-microphone
Terminal=false
Categories=AudioVideo;Audio;
StartupNotify=true
DESKTOP
dpkg-deb --root-owner-group --build "$package_dir" "$project_dir/build/sotto-linux_0.2.0_all.deb"
