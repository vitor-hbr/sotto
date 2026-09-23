"""Delivery routing checks: no microphone, live portal, or model server."""
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sotto_linux.app import Application
from sotto_linux.delivery import Delivery
from sotto_linux.settings import DELIVERY_MODES, Settings


def immediately(work, done):
    done(work(), None)


class ApplicationDeliveryTests(unittest.TestCase):
    def app(self, mode="direct"):
        app = Mock()
        app.quitting = app.busy = False
        app.stop = threading.Event()
        app.cancel = threading.Event()
        app.config = {"delivery_mode": mode, "auto_insert": True,
                      "microphone": "", "device_id": "test"}
        app.client.return_value = SimpleNamespace(endpoint="http://localhost:8391")
        app.tracker = None
        app.paste = Mock(ready=True)
        app.paste.paste.return_value = True
        app.delivery = Delivery(None, app.paste)
        return app

    def complete(self, app, **kwargs):
        result = {"id": "completed-take", "insertionText": "Olá, Linux!"}
        with patch("sotto_linux.app.background", side_effect=immediately):
            Application.complete(app, app.client(), result, None, **kwargs)
        return result

    def test_direct_completion_without_tracker_or_anchor_keeps_transcript_and_reports_unconfirmed(self):
        for mode, terminal in (("direct", False), ("direct_terminal", True)):
            with self.subTest(mode=mode):
                app = self.app(mode)
                self.complete(app, auto_insert=True, delivery_mode=mode)
                app.paste.paste.assert_called_once()
                self.assertEqual(app.paste.paste.call_args.kwargs["terminal"], terminal)
                app.transcript.get_buffer().set_text.assert_called_once_with("Olá, Linux!")
                self.assertEqual(app.report.call_args.args[0], "unconfirmed")
                self.assertFalse(app.finish_ui.call_args.kwargs["show"])
                self.assertIsNone(app.delivery.previous)

    def test_automatic_mode_selects_shortcut_at_delivery_not_recording(self):
        for terminal in (True, False):
            app = self.app("direct_auto")
            with patch("sotto_linux.app.terminal_paste", return_value=terminal) as focused:
                with patch("sotto_linux.app.background"):
                    Application.start_recording(app, external=True)
                focused.assert_not_called()
                self.complete(app, auto_insert=True, delivery_mode="direct_auto")
                focused.assert_called_once_with()
            self.assertEqual(app.paste.paste.call_args.kwargs["terminal"], terminal)
            self.assertEqual(app.report.call_args.args[0], "unconfirmed")

    def test_native_backend_starts_on_first_press_without_permission_gate(self):
        from sotto_linux.gnome_paste import GnomePaste
        app = self.app("direct_auto")
        app.paste = GnomePaste(Mock())
        with patch("sotto_linux.app.background") as background, patch.object(app.paste, "enable") as enable:
            Application.start_recording(app, external=True)
        self.assertTrue(app.busy)
        background.assert_called_once()
        enable.assert_not_called()
        app.window.present.assert_not_called()

    def test_expired_permission_reconnects_before_opening_microphone(self):
        for mode in ("direct", "direct_terminal", "direct_auto"):
            app = self.app(mode)
            app.paste.ready = False
            with patch("sotto_linux.app.background") as background:
                Application.start_recording(app, external=True)
            background.assert_not_called()
            app.paste.enable.assert_called_once_with()
            self.assertFalse(app.busy)
            app.window.present.assert_not_called()

    def test_manual_recording_remains_available_without_paste_permission(self):
        for options in ({}, {"mode": "test", "external": True}):
            app = self.app("direct_auto")
            app.paste.ready = False
            with patch("sotto_linux.app.background") as background:
                Application.start_recording(app, **options)
            background.assert_called_once()
            app.paste.enable.assert_not_called()

    def test_notifications_can_be_disabled_without_disabling_status_updates(self):
        app = self.app()
        app.config["notifications"] = False
        Application.notify(app, "Recording…")
        app.send_notification.assert_not_called()
        app.config["notifications"] = True
        Application.notify(app, "Recording…")
        app.send_notification.assert_called_once()

    def test_unavailable_permission_keeps_transcript_for_copying(self):
        app = self.app()
        app.paste.ready = False
        self.complete(app, auto_insert=True, delivery_mode="direct")
        app.paste.paste.assert_not_called()
        app.transcript.get_buffer().set_text.assert_called_once_with("Olá, Linux!")
        self.assertTrue(app.finish_ui.call_args.kwargs["show"])
        app.report.assert_not_called()

    def test_guarded_completion_still_requires_an_anchor(self):
        app = self.app("guarded")
        self.complete(app, auto_insert=True, delivery_mode="guarded")
        app.paste.paste.assert_not_called()

    def test_auto_insert_disabled_and_microphone_test_never_paste(self):
        for options in ({"auto_insert": False}, {"auto_insert": True, "mode": "test"}):
            app = self.app()
            self.complete(app, delivery_mode="direct", **options)
            app.paste.paste.assert_not_called()

    def test_cancelled_completion_never_pastes(self):
        app = self.app()
        app.cancel.set()
        self.complete(app, auto_insert=True, delivery_mode="direct")
        app.paste.paste.assert_not_called()

    def test_start_freezes_mode_and_gui_start_is_manual(self):
        for external in (True, False):
            app = self.app("direct_terminal")
            app.delivery.previous = ("old", None, "server", 0)
            with patch("sotto_linux.app.background") as background:
                Application.start_recording(app, external=external)
            self.assertIsNone(app.delivery.previous)
            app.config["delivery_mode"] = "guarded"
            # Complete the take without ever running recording/network work.
            background.call_args.args[1]({"id": "test"}, None)
            args = app.complete.call_args.args
            self.assertIsNone(args[4])  # no accessible anchor required
            self.assertEqual(args[5], external)  # GUI starts do not auto-paste
            self.assertEqual(args[6], "direct_terminal")  # frozen at start

    def test_start_does_not_inspect_accessibility_in_direct_mode(self):
        app = self.app()
        app.tracker = Mock()
        with patch("sotto_linux.app.background"):
            Application.start_recording(app, external=True)
        app.tracker.snapshot.assert_not_called()

    def test_command_line_and_toggle_opt_in_to_external_delivery(self):
        for command in ("start", "toggle"):
            app = self.app()
            cli = Mock()
            cli.get_options_dict().end().unpack.return_value = {command: True}
            Application.do_command_line(app, cli)
            method = app.start_recording if command == "start" else app.toggle
            method.assert_called_once_with(external=True)
        app = self.app()
        Application.toggle(app, external=True)
        app.start_recording.assert_called_once_with(external=True)


class DeliverySettingsTests(unittest.TestCase):
    def test_modes_round_trip_and_existing_settings_default_to_guarded(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"XDG_CONFIG_HOME": root}):
            settings = Settings()
            self.assertEqual(settings.data["delivery_mode"], "guarded")
            for mode in DELIVERY_MODES:
                settings.data["delivery_mode"] = mode
                settings.save()
                self.assertEqual(Settings().data["delivery_mode"], mode)
            del settings.data["delivery_mode"]
            settings.save()
            self.assertEqual(Settings().data["delivery_mode"], "guarded")

    def test_backend_round_trips_and_invalid_values_do_not_enable_native_input(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"XDG_CONFIG_HOME": root}):
            settings = Settings()
            self.assertEqual(settings.data["paste_backend"], "portal")
            settings.data["paste_backend"] = "gnome"
            settings.save()
            self.assertEqual(Settings().data["paste_backend"], "gnome")
            for invalid in (None, True, "unknown", ["gnome"]):
                settings.data["paste_backend"] = invalid
                settings.save()
                self.assertEqual(Settings().data["paste_backend"], "portal")

    def test_notification_preference_round_trips_and_rejects_non_booleans(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"XDG_CONFIG_HOME": root}):
            settings = Settings()
            self.assertTrue(settings.data["notifications"])
            settings.data["notifications"] = False
            settings.save()
            self.assertFalse(Settings().data["notifications"])
            settings.data["notifications"] = "false"
            settings.save()
            self.assertTrue(Settings().data["notifications"])

    def test_invalid_mode_never_enables_direct_paste(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"XDG_CONFIG_HOME": root}):
            settings = Settings()
            for invalid in ("unknown", True, None, ["direct"], {"direct": True}):
                settings.data["delivery_mode"] = invalid
                settings.save()
                self.assertEqual(Settings().data["delivery_mode"], "guarded")


if __name__ == "__main__":
    unittest.main()
