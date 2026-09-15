import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from gi.repository import GLib
from sotto_linux.portals import Paste, Portal, Shortcuts


def spin(predicate, seconds=4):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        GLib.MainContext.default().iteration(False)
        time.sleep(0.005)
    if not predicate():
        raise AssertionError("Timed out waiting for portal result")


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.log = Path(self.directory.name) / "calls.jsonl"
        self.service = None
        self.portal = None

    def start(self, deny=False):
        env = dict(os.environ)
        if deny:
            env["SOTTO_TEST_DENY"] = "1"
        self.service = subprocess.Popen([sys.executable, str(Path(__file__).with_name("portal_service.py")), str(self.log)],
                                        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(self.service.stdout.readline().strip(), "READY")
        self.portal = Portal()

    def tearDown(self):
        if self.portal:
            self.portal.close()
        if self.service:
            self.service.terminate()
            self.service.communicate(timeout=5)
        self.directory.cleanup()

    def test_hold_release_and_duplicate_press_over_real_dbus(self):
        self.start()
        events, messages = [], []
        shortcuts = Shortcuts(self.portal, lambda: events.append("press"), lambda: events.append("release"), messages.append)
        shortcuts.enable()
        spin(lambda: len(events) == 2)
        self.assertEqual(events, ["press", "release"])
        self.assertTrue(any("enabled" in message for message in messages))
        self.assertFalse(self.portal.pending)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(calls[0], ["Register", ["io.github.vitor_hbr.Sotto", {}]])

    def test_denied_permission_does_not_leave_live_session(self):
        self.start(deny=True)
        messages = []
        shortcuts = Shortcuts(self.portal, lambda: self.fail("No activation expected"), lambda: None, messages.append)
        shortcuts.enable()
        spin(lambda: not shortcuts.enabling)
        self.assertIsNone(shortcuts.session)
        self.assertIn("declined", messages[-1])

    def test_paste_requests_only_keyboard_and_releases_modifiers(self):
        self.start()
        paste = Paste(self.portal, lambda _: None)
        paste.enable()
        spin(lambda: paste.ready)
        paste.paste("olá 🐧\nsecond line", terminal=True)
        spin(lambda: '"Transferred"' in self.log.read_text())
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(next(args[0] for method, args in calls if method == "Transferred"), "olá 🐧\nsecond line")
        select = next(args for method, args in calls if method == "SelectDevices")
        self.assertEqual(select[1]["types"], 1)
        keys = [(args[2], args[3]) for method, args in calls if method == "NotifyKeyboardKeysym"]
        self.assertEqual(keys, [(0xffe3, 1), (0xffe1, 1), (0x76, 1), (0x76, 0), (0xffe1, 0), (0xffe3, 0)])
        self.assertNotIn("SelectSources", [method for method, _ in calls])

    def test_disable_while_permission_pending_cannot_resurrect_session(self):
        self.start()
        shortcuts = Shortcuts(self.portal, lambda: None, lambda: None, lambda _: None)
        shortcuts.enable()
        shortcuts.disable()
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            GLib.MainContext.default().iteration(False)
            time.sleep(0.01)
        self.assertIsNone(shortcuts.session)
        self.assertFalse(self.portal.pending)

    def test_paste_rechecks_destination_after_clipboard_claim(self):
        self.start()
        paste = Paste(self.portal, lambda _: None)
        paste.enable()
        spin(lambda: paste.ready)
        self.assertFalse(paste.paste("hello", guard=lambda: False))
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertNotIn("NotifyKeyboardKeysym", [method for method, _ in calls])


if __name__ == "__main__":
    unittest.main()
