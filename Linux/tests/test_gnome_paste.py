import subprocess
import unittest
from unittest.mock import Mock, patch

from gi.repository import GLib
from sotto_linux.delivery import Delivery
from sotto_linux.gnome_paste import GnomePaste


class GnomePasteTests(unittest.TestCase):
    def setUp(self):
        self.bus = Mock()
        self.connection = patch("sotto_linux.gnome_paste.Gio.bus_get_sync", return_value=self.bus)
        self.connection.start()
        self.addCleanup(self.connection.stop)
        self.command = patch("sotto_linux.gnome_paste.subprocess.run")
        self.run = self.command.start()
        self.addCleanup(self.command.stop)
        self.paste = GnomePaste(Mock())

    def test_unicode_clipboard_and_exact_app_shortcut_without_portal(self):
        text = "Olá 🐧\n$(not a shell command)"
        for terminal, keys in ((False, [29, 47]), (True, [29, 42, 47])):
            with self.subTest(terminal=terminal):
                self.bus.reset_mock()
                self.run.reset_mock()
                self.assertTrue(self.paste.paste(text, terminal=terminal))
                self.bus.call_sync.assert_called_once()
                args = self.bus.call_sync.call_args.args
                self.assertEqual(args[:4], ("org.gnome.Shell", "/org/gnome/Shell/Extensions/Clipboard",
                                           "org.gnome.Shell.Extensions.Clipboard", "SetContentBinary"))
                data, mime = args[4].unpack()
                self.assertEqual(bytes(data).decode("utf-8"), text)
                self.assertEqual(mime, "text/plain;charset=utf-8")
                self.assertEqual(self.run.call_args_list[0].args[0],
                                 ["ydotool", "key", "--key-delay=12", *[f"{key}:1" for key in keys],
                                  *[f"{key}:0" for key in reversed(keys)]])
                self.assertEqual(self.run.call_args_list[1].args[0],
                                 ["ydotool", "key", "--key-delay=12", *[f"{key}:0" for key in reversed(keys)]])
                for call in self.run.call_args_list:
                    self.assertTrue(call.kwargs["check"])
                    self.assertEqual(call.kwargs["timeout"], 2)
                    self.assertNotIn("shell", call.kwargs)
                    self.assertNotIn(text, call.args[0])

    def test_cancel_before_or_after_clipboard_never_sends_keys(self):
        self.assertFalse(self.paste.paste("no", guard=lambda: False))
        self.bus.call_sync.assert_not_called()
        self.assertFalse(self.paste.paste("no", guard=Mock(side_effect=[True, False])))
        self.bus.call_sync.assert_called_once()
        self.run.assert_not_called()

    def test_missing_extension_never_sends_keys_or_uses_portal(self):
        self.bus.call_sync.side_effect = GLib.Error("Extension unavailable")
        with self.assertRaises(GLib.Error):
            self.paste.paste("no")
        self.run.assert_not_called()
        self.assertEqual(self.bus.call_sync.call_count, 1)
        self.assertTrue(self.paste.ready)  # no permission state to re-enable

    def test_ambiguous_key_error_releases_keys_and_never_retries_paste(self):
        for error in (subprocess.TimeoutExpired("ydotool", 2),
                      subprocess.CalledProcessError(1, "ydotool"), FileNotFoundError("ydotool")):
            with self.subTest(error=type(error).__name__):
                self.run.reset_mock()
                self.bus.reset_mock()
                self.run.side_effect = [error, None]
                delivery = Delivery(None, self.paste)
                self.assertEqual(delivery.direct_paste("take", "hello", "server", terminal=True)[0], "unconfirmed")
                self.assertEqual(self.run.call_count, 2)
                self.assertEqual(self.run.call_args.args[0][-3:], ["47:0", "42:0", "29:0"])
                delivery.direct_paste("take", "hello", "server", terminal=True)
                self.assertEqual(self.run.call_count, 2)
                self.assertEqual(self.bus.call_sync.call_count, 1)

    def test_successful_chord_releases_keys_even_if_cleanup_process_fails(self):
        held = set()
        def chord(argv, **_kwargs):
            for event in argv[3:]:
                key, pressed = event.split(":")
                if pressed == "1":
                    held.add(key)
                else:
                    held.discard(key)
        # Model key events from the successful first process, but not the failed cleanup.
        def run(argv, **kwargs):
            if self.run.call_count == 1:
                chord(argv, **kwargs)
            else:
                raise subprocess.CalledProcessError(1, "ydotool")
        self.run.side_effect = run
        with self.assertRaises(subprocess.CalledProcessError):
            self.paste.paste("one paste", terminal=True)
        self.assertEqual(held, set())
        self.assertEqual(self.run.call_count, 2)

    def test_enable_is_only_status_no_permission_request(self):
        self.assertFalse(self.paste.requires_permission)
        self.assertTrue(self.paste.ready)
        self.paste.enable()
        self.bus.call_sync.assert_not_called()
        self.run.assert_not_called()
