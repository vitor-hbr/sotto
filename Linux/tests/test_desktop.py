"""Real GTK/GStreamer checks; run inside a graphical session or xvfb-run."""

import threading
import time
import os
import subprocess
import tempfile
import sys
import unittest
from unittest.mock import patch

from sotto_linux.app import Application
from sotto_linux.audio import Microphone
from gi.repository import Gio, GLib, Gst


class DesktopTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
