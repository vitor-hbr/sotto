"""Real GTK/GStreamer checks; run inside a graphical session or xvfb-run."""

import threading
import time
import os
import subprocess
import tempfile
import sys
import json
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest.mock import patch

from sotto_linux.app import Application
from sotto_linux.audio import Microphone
from sotto_linux.api import Client
from sotto_linux.session import record
from gi.repository import Gio, GLib, Gst


class DesktopTests(unittest.TestCase):
    def test_capture_to_real_http_upload_and_completed_transcript(self):
        frames, sequences, finish = {}, {}, []
        generation_id = str(uuid.uuid4())
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_POST(self):
                data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                path = urlsplit(self.path)
                if path.path == "/v1/generations":
                    result = {"id": generation_id, "settings": {"preferences": {"keepOriginalAudio": True}}}
                elif "/audio/" in path.path:
                    kind = path.path.rsplit("/", 1)[1]
                    query = parse_qs(path.query)
                    sequence = int(query["sequence"][0])
                    assert sequence == sequences.get(kind, 0)
                    sequences[kind] = sequence + 1
                    frames[kind] = frames.get(kind, 0) + len(data) // (4 * int(query["channels"][0]))
                    result = {"nextSequence": sequence + 1, "frameCount": frames[kind]}
                else:
                    finish.append(json.loads(data))
                    result = {"id": generation_id, "status": "completed", "insertionText": "Olá, Linux!"}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stop = threading.Event()
        timer = threading.Timer(0.65, stop.set)
        parse_launch = Gst.parse_launch
        def synthetic(description):
            return parse_launch(description.replace("pulsesrc name=source", "audiotestsrc name=source is-live=true ! audio/x-raw,rate=48000,channels=2"))
        with patch("sotto_linux.audio.Gst.parse_launch", side_effect=synthetic):
            microphone = Microphone(stop)
        try:
            timer.start()
            client = Client(f"http://127.0.0.1:{server.server_port}")
            result = record(client, {"id": "test", "name": "Linux"}, lambda: microphone,
                            stop, threading.Event(), lambda _: None)
            self.assertEqual(result["insertionText"], "Olá, Linux!")
            self.assertEqual(finish, [{"inferenceFrames": frames["inference"], "originalFrames": frames["original"]}])
            self.assertLessEqual(abs(frames["original"] - frames["inference"] * 3), 3)
            self.assertEqual(microphone.pipeline.get_state(0)[1], Gst.State.NULL)
        finally:
            timer.cancel()
            microphone.close()
            server.shutdown()
            server.server_close()
            thread.join()

    def test_remote_quit_reaches_running_instance(self):
        with tempfile.TemporaryDirectory() as config:
            env = dict(os.environ, XDG_CONFIG_HOME=config)
            process = subprocess.Popen([sys.executable, "-m", "sotto_linux", "--stop"], env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    reply = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                          "org.freedesktop.DBus", "NameHasOwner",
                                          GLib.Variant("(s)", ("io.github.vitor_hbr.Sotto",)),
                                          GLib.VariantType.new("(b)"), Gio.DBusCallFlags.NONE, 1000, None)
                    if reply.unpack()[0]:
                        break
                    time.sleep(0.05)
                else:
                    self.fail("Primary application did not register on the session bus")
                remote = subprocess.run([sys.executable, "-m", "sotto_linux", "--quit"],
                                        env=env, capture_output=True, timeout=5)
                self.assertEqual(remote.returncode, 0, remote.stderr.decode())
                _stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr.decode())
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate()

    def test_window_builds_and_transcript_is_copied(self):
        app = Application()
        app.set_application_id("io.github.vitor_hbr.Sotto.Test")
        self.assertTrue(app.register(None))
        app.ensure_window()
        app.window.present()
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            GLib.MainContext.default().iteration(False)
        app.transcript.get_buffer().set_text("Olá, Linux!")
        class Client:
            def delivery(self, generation_id):
                pass
        app.delivery_target = (Client(), "test")
        app.copy_transcript(None)
        clipboard_result = []
        def received(clipboard, result):
            clipboard_result.append(clipboard.read_text_finish(result))
        app.window.get_clipboard().read_text_async(None, received)
        deadline = time.monotonic() + 2
        while not clipboard_result and time.monotonic() < deadline:
            GLib.MainContext.default().iteration(False)
        self.assertEqual(clipboard_result, ["Olá, Linux!"])
        app.window.destroy()
        app.quit()

    def test_direct_mode_can_be_selected_and_saved_without_accessibility_service(self):
        with tempfile.TemporaryDirectory() as config, patch.dict(os.environ, {"XDG_CONFIG_HOME": config}), \
                patch("sotto_linux.app.FocusTracker", side_effect=RuntimeError("No accessibility bus")):
            app = Application()
            app.set_application_id("io.github.vitor_hbr.Sotto.Test.NoAtspi")
            self.assertTrue(app.register(None))
            try:
                app.ensure_window()
                self.assertIsNone(app.tracker)
                self.assertIsNotNone(app.delivery)
                self.assertIs(app.delivery.paste, app.paste)
                app.desktop.delivery_mode.set_active_id("direct_terminal")
                self.assertEqual(app.config["delivery_mode"], "direct_terminal")
                saved = json.loads(app.settings.path.read_text())
                self.assertEqual(saved["delivery_mode"], "direct_terminal")
                app.desktop.delivery_mode.set_active_id("direct_auto")
                app.desktop.notifications.set_active(False)
                saved = json.loads(app.settings.path.read_text())
                self.assertEqual(saved["delivery_mode"], "direct_auto")
                self.assertFalse(saved["notifications"])
            finally:
                app.window.destroy()
                app.quit()
                if app.portal:
                    app.portal.close()

    def test_portal_shortcut_routes_to_external_dictation(self):
        with tempfile.TemporaryDirectory() as config, patch.dict(os.environ, {"XDG_CONFIG_HOME": config}), \
                patch("sotto_linux.app.Shortcuts") as shortcuts:
            app = Application()
            app.set_application_id("io.github.vitor_hbr.Sotto.Test.Shortcut")
            self.assertTrue(app.register(None))
            try:
                pressed = shortcuts.call_args.args[1]
                with patch.object(app, "start_recording") as start:
                    pressed()
                    start.assert_called_once_with(external=True)
            finally:
                app.quit()
                if app.portal:
                    app.portal.close()
                if app.tracker:
                    app.tracker.close()

    def test_native_startup_and_window_never_construct_remote_desktop(self):
        from sotto_linux.settings import Settings
        from sotto_linux.gnome_paste import GnomePaste
        with tempfile.TemporaryDirectory() as config, patch.dict(os.environ, {"XDG_CONFIG_HOME": config}), \
                patch("sotto_linux.app.Portal") as portal, patch("sotto_linux.app.Paste") as paste:
            settings = Settings()
            settings.data.update(paste_backend="gnome", delivery_mode="direct_auto", notifications=False)
            settings.save()
            app = Application()
            app.set_application_id("io.github.vitor_hbr.Sotto.Test.NativePaste")
            self.assertTrue(app.register(None))
            try:
                app.ensure_window()
                self.assertIsInstance(app.paste, GnomePaste)
                self.assertTrue(app.paste.ready)
                self.assertIsNone(app.portal)
                self.assertIsNone(app.shortcuts)
                app.enable_paste()
                portal.assert_not_called()
                paste.assert_not_called()
                self.assertIn("No Remote Desktop", app.desktop.paste_status.get_text())
                self.assertEqual(app.desktop.paste_backend.get_active_id(), "gnome")
                self.assertFalse(app.desktop.notifications.get_active())
            finally:
                app.quitting = True
                app.window.destroy()
                app.quit()
                if app.tracker:
                    app.tracker.close()

    def test_saved_direct_mode_requests_paste_permission_on_startup(self):
        from sotto_linux.settings import Settings
        with tempfile.TemporaryDirectory() as config, patch.dict(os.environ, {"XDG_CONFIG_HOME": config}), \
                patch("sotto_linux.app.Paste") as paste:
            settings = Settings()
            settings.data["delivery_mode"] = "direct_auto"
            settings.save()
            app = Application()
            app.set_application_id("io.github.vitor_hbr.Sotto.Test.PasteStartup")
            self.assertTrue(app.register(None))
            try:
                app.ensure_window()
                deadline = time.monotonic() + 2
                while not paste.return_value.enable.called and time.monotonic() < deadline:
                    GLib.MainContext.default().iteration(False)
                paste.return_value.enable.assert_called_once_with()
            finally:
                app.window.destroy()
                app.quitting = True
                app.quit()
                if app.portal:
                    app.portal.close()
                if app.tracker:
                    app.tracker.close()

    def test_real_gstreamer_conversion_and_stop_drains_queue(self):
        original = Gst.parse_launch
        def synthetic(description):
            return original(description.replace("pulsesrc", "audiotestsrc is-live=true"))
        stop = threading.Event()
        with patch("sotto_linux.audio.Gst.parse_launch", side_effect=synthetic):
            microphone = Microphone(stop)
        try:
            microphone.start()
            pcm = None
            deadline = time.monotonic() + 3
            while pcm is None and time.monotonic() < deadline:
                pcm = microphone.read()
            self.assertTrue(pcm)
            self.assertEqual(len(pcm), 16000)
            caps = microphone.sink.get_static_pad("sink").get_current_caps().get_structure(0)
            self.assertEqual(caps.get_value("format"), "F32LE")
            self.assertEqual(caps.get_value("rate"), 16000)
            self.assertEqual(caps.get_value("channels"), 1)
            stop.set()
            microphone.finish()
            while microphone.read() != b"":
                pass
            self.assertEqual(microphone.read(), b"")
        finally:
            microphone.close()

    def test_original_stereo_audio_keeps_source_rate_and_matches_inference_interval(self):
        original = Gst.parse_launch
        def synthetic(description):
            return original(description.replace("pulsesrc name=source", "audiotestsrc name=source is-live=true ! audio/x-raw,rate=48000,channels=2"))
        stop = threading.Event()
        with patch("sotto_linux.audio.Gst.parse_launch", side_effect=synthetic):
            microphone = Microphone(stop)
        microphone.keep_original = True
        try:
            microphone.start()
            inference_frames = 0
            deadline = time.monotonic() + 0.65
            while time.monotonic() < deadline:
                pcm = microphone.read()
                if pcm:
                    inference_frames += len(pcm) // 4
                    source, rate, channels, frames = microphone.original_audio(inference_frames)
                    self.assertEqual((rate, channels), (48000, 2))
                    self.assertEqual(frames, inference_frames * 3)
                    self.assertEqual(len(source) % 8, 0)
            stop.set()
            microphone.finish()
            while True:
                pcm = microphone.read()
                if pcm == b"":
                    break
                if pcm:
                    inference_frames += len(pcm) // 4
                    source, rate, channels, frames = microphone.original_audio(inference_frames)
            self.assertGreater(inference_frames, 4000)
            self.assertLessEqual(abs(frames - inference_frames * 3), 3)
        finally:
            microphone.close()


if __name__ == "__main__":
    unittest.main()
