import json
import unittest
from unittest.mock import Mock, patch

from gi.repository import Gio, GLib
from sotto_linux.focus import focused_terminal, terminal_paste


class FocusTests(unittest.TestCase):
    def test_only_focused_window_identity_selects_terminal_paste(self):
        for name, expected in (("com.mitchellh.ghostty", True), ("Helium", False),
                               ("slack", False), ("t3code", False)):
            with self.subTest(app=name):
                windows = [{"wm_class": "com.mitchellh.ghostty", "has_focus": False},
                           {"wm_class": name, "has_focus": True, "title": "com.mitchellh.ghostty"}]
                self.assertEqual(focused_terminal(windows), expected)
        self.assertTrue(focused_terminal([{"wm_class_instance": "COM.MITCHELLH.GHOSTTY", "has_focus": True}]))

    def test_absent_ambiguous_or_malformed_focus_uses_standard_paste(self):
        for windows in (None, {}, [], [None], [{"wm_class": "kitty"}],
                        [{"wm_class": 42, "has_focus": True}],
                        [{"wm_class": "kitty", "has_focus": "true"}],
                        [{"wm_class": "kitty", "has_focus": True}, {"wm_class": "slack", "has_focus": True}]):
            with self.subTest(windows=windows):
                self.assertFalse(focused_terminal(windows))

    def test_live_lookup_contract_is_bounded_and_does_not_activate_shell(self):
        bus = Mock()
        bus.call_sync.return_value = GLib.Variant("(s)", (json.dumps(
            {"wm_class": "com.mitchellh.ghostty", "has_focus": True}),))
        with patch("sotto_linux.focus.Gio.bus_get_sync", return_value=bus):
            self.assertTrue(terminal_paste())
        args = bus.call_sync.call_args.args
        self.assertEqual(args[:4], ("org.gnome.Shell", "/org/gnome/Shell/Extensions/Windows",
                                   "org.gnome.Shell.Extensions.Windows", "GetFocusedWindowSync"))
        self.assertEqual(args[6:8], (Gio.DBusCallFlags.NO_AUTO_START, 500))

    def test_missing_extension_and_invalid_json_use_standard_paste(self):
        for response in (GLib.Error("No extension"), GLib.Variant("(s)", ("invalid",))):
            bus = Mock()
            if isinstance(response, Exception):
                bus.call_sync.side_effect = response
            else:
                bus.call_sync.return_value = response
            with patch("sotto_linux.focus.Gio.bus_get_sync", return_value=bus):
                self.assertFalse(terminal_paste())
