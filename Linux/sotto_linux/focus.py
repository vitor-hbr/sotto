"""Optional GNOME focused-app lookup; no window titles are used or retained."""
import json

from gi.repository import Gio, GLib

TERMINALS = frozenset({
    "com.mitchellh.ghostty", "org.gnome.terminal", "gnome-terminal-server",
    "org.gnome.console", "org.kde.konsole", "konsole", "kitty", "alacritty",
    "foot", "footclient", "com.vitor.btop",
})


def focused_terminal(windows):
    if not isinstance(windows, list):
        return False
    focused = [window for window in windows if isinstance(window, dict) and window.get("has_focus") is True]
    if len(focused) != 1:
        return False
    return any(isinstance(focused[0].get(key), str) and focused[0][key].casefold() in TERMINALS
               for key in ("wm_class", "wm_class_instance"))


def terminal_paste():
    """Use Vicinae's existing window API when available, otherwise standard paste."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        result = bus.call_sync("org.gnome.Shell", "/org/gnome/Shell/Extensions/Windows",
                              "org.gnome.Shell.Extensions.Windows", "GetFocusedWindowSync", None,
                              GLib.VariantType.new("(s)"), Gio.DBusCallFlags.NO_AUTO_START, 500, None)
        return focused_terminal([json.loads(result.unpack()[0])])
    except (GLib.Error, ValueError, TypeError):
        return False
