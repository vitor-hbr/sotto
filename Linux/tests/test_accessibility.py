import os
from pathlib import Path
import subprocess
import sys
import signal
import time
import unittest
from gi.repository import GLib
from sotto_linux.delivery import Delivery, FocusTracker, read_text


class AccessibilityTests(unittest.TestCase):
    def setUp(self):
        self.tracker = FocusTracker()
        self.addCleanup(self.tracker.close)
        self.editor = subprocess.Popen([sys.executable, str(Path(__file__).with_name("gtk_editor.py"))],
                                       env=dict(os.environ, GTK_A11Y="atspi"), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.close_editor)
        self.assertEqual(self.editor.stdout.readline().strip(), b"READY")
        deadline = time.monotonic() + 5
        self.anchor = None
        while time.monotonic() < deadline:
            GLib.MainContext.default().iteration(False)
            self.anchor = self.tracker.snapshot()
            if self.anchor:
                break
            time.sleep(0.01)
        self.assertIsNotNone(self.anchor, "Native GTK editor did not expose a focused text field")

    def close_editor(self):
        self.editor.terminate()
        self.editor.communicate(timeout=5)

    def test_native_gtk_unicode_insertion_at_the_original_caret(self):
        delivery = Delivery(self.tracker)
        status, message = delivery.insert("real-test", "olá 🐧 ", self.anchor, "test-server")
        self.assertEqual(status, "inserted", message)
        self.assertEqual(read_text(self.anchor.target.get_text_iface(), 0, -1), "before olá 🐧 after")

    def test_real_caret_movement_blocks_insertion(self):
        self.editor.send_signal(signal.SIGUSR1)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self.anchor.target.get_text_iface().get_caret_offset() != 0:
            GLib.MainContext.default().iteration(False)
            time.sleep(0.01)
        status, _ = Delivery(self.tracker).insert("changed", "wrong place", self.anchor, "test-server")
        self.assertEqual(status, "none")
        self.assertEqual(read_text(self.anchor.target.get_text_iface(), 0, -1), "before after")


if __name__ == "__main__":
    unittest.main()
