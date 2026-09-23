"""Direct GNOME clipboard + local input-device paste, without RemoteDesktop."""
import subprocess

from gi.repository import Gio, GLib


class GnomePaste:
    # There is no permission handshake. Dependencies are checked when delivering;
    # an unavailable extension/daemon must not consume the first recording press.
    requires_permission = False
    ready = True

    def __init__(self, changed):
        self.changed = changed

    def enable(self):
        self.changed("GNOME paste uses Vicinae and ydotool. No Remote Desktop session is opened.")

    def paste(self, text, terminal=False, guard=lambda: True):
        if not guard():
            return False
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        # Force plain UTF-8 rather than Vicinae's content-type heuristics.
        # Vicinae sets both CLIPBOARD and PRIMARY; do not restore them while a
        # destination may still be reading the paste asynchronously.
        bus.call_sync("org.gnome.Shell", "/org/gnome/Shell/Extensions/Clipboard",
                      "org.gnome.Shell.Extensions.Clipboard", "SetContentBinary",
                      GLib.Variant("(ays)", (text.encode("utf-8"), "text/plain;charset=utf-8")),
                      GLib.VariantType.new("()"), Gio.DBusCallFlags.NO_AUTO_START, 2000, None)
        if not guard():
            return False
        # Linux input keycodes: Left Ctrl, optional Left Shift, V. No Enter,
        # no shell, and no dictated text in process arguments.
        keys = [29, 42, 47] if terminal else [29, 47]
        releases = [f"{key}:0" for key in reversed(keys)]
        try:
            # Keep the complete chord in one invocation so normal delivery does
            # not depend on a second process starting to release held keys.
            self.keys([f"{key}:1" for key in keys] + releases)
        finally:
            # Even a timeout/ambiguous failure may have pressed a modifier.
            # Release only; never repeat a paste after an uncertain outcome.
            self.keys(releases)
        return True

    @staticmethod
    def keys(events):
        subprocess.run(["ydotool", "key", "--key-delay=12", *events], check=True,
                       timeout=2, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
