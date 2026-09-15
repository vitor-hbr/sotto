import copy
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk
from sotto_linux.api import APIError
from sotto_linux.dictionary import Dictionary
from sotto_linux.settings import Settings
from sotto_linux.views import History, Preferences, text_value


SNAPSHOT = {"revision": 3, "preferences": {"language": "pt", "proofreadingPrompt": "Keep meaning.",
    "vocabulary": "Sotto", "textCorrectionEnabled": True, "keepOriginalAudio": False,
    "dictionary": {"lists": [{"id": "a", "name": "Personal", "entries": [
        {"id": "word", "term": "Sotto", "aliases": ["sóto", "so;to"], "isPriority": True}]},
        {"id": "b", "name": "Personal", "entries": []}]}}}


def spin(predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        GLib.MainContext.default().iteration(False)
        time.sleep(0.005)
    if not predicate():
        raise AssertionError("UI operation did not finish")


class Server:
    endpoint = "https://server.example"
    def __init__(self):
        self.fail = False
        self.saved = []
        self.pages = []

    def preferences(self, snapshot=None):
        if self.fail:
            raise APIError("Server returned HTTP 409.")
        if snapshot:
            self.saved.append(snapshot)
            result = copy.deepcopy(snapshot)
            result["revision"] += 1
            return result
        return copy.deepcopy(SNAPSHOT)

    def history(self, cursor=None):
        self.pages.append(cursor)
        record = {"id": str(uuid.uuid4()), "createdAt": "2026-09-15T12:00:00Z", "device": {"name": "Linux"},
                  "status": "completed", "finalText": "Hello", "rawText": "hello"}
        return {"items": [record], "nextCursor": None if cursor else record["id"]}


class ViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Gtk.init()

    def test_dictionary_round_trip_preserves_ids_empty_lists_and_alias_punctuation(self):
        editor = Dictionary()
        value = SNAPSHOT["preferences"]["dictionary"]
        editor.load(value)
        self.assertEqual(editor.value(), value)
        editor.groups[1]["name"].set_text("Renamed")
        self.assertEqual(editor.value()["lists"][1]["id"], "b")

    def test_revisioned_preferences_save_preserves_dictionary_and_updates_revision(self):
        server = Server()
        view = Preferences(lambda: server)
        view.load()
        spin(lambda: view.snapshot is not None)
        view.language.set_active_id("en")
        view.save()
        spin(lambda: view.snapshot["revision"] == 4)
        self.assertEqual(server.saved[0]["revision"], 3)
        self.assertEqual(server.saved[0]["preferences"]["language"], "en")
        self.assertEqual(server.saved[0]["preferences"]["dictionary"], SNAPSHOT["preferences"]["dictionary"])

    def test_conflict_keeps_unsaved_edits(self):
        server = Server()
        view = Preferences(lambda: server)
        view.load()
        spin(lambda: view.snapshot is not None)
        view.prompt.get_buffer().set_text("My unsaved instructions")
        server.fail = True
        view.save()
        spin(lambda: view.reload.get_sensitive())
        self.assertEqual(text_value(view.prompt), "My unsaved instructions")
        self.assertEqual(view.snapshot["revision"], 3)
        self.assertIn("Edits kept", view.status.get_text())

    def test_failed_reload_cannot_save_old_settings_to_new_server(self):
        first, second = Server(), Server()
        second.endpoint = "https://different.example"
        second.fail = True
        current = [first]
        view = Preferences(lambda: current[0])
        view.load()
        spin(lambda: view.snapshot is not None)
        current[0] = second
        view.load()
        spin(lambda: view.reload.get_sensitive())
        view.save()
        self.assertIn("Server address changed", view.status.get_text())
        self.assertFalse(second.saved)

    def test_history_paginates_without_delivering_and_shows_raw_text(self):
        server = Server()
        parent = Gtk.Window()
        self.addCleanup(parent.destroy)
        history = History(lambda: server, parent)
        history.load(False)
        spin(lambda: len(history.records) == 1)
        cursor = history.cursor
        history.load(True)
        spin(lambda: len(history.records) == 2)
        self.assertEqual(server.pages, [None, cursor])
        history.list.select_row(history.list.get_row_at_index(0))
        self.assertEqual(text_value(history.detail), "Hello")
        self.assertEqual(text_value(history.original), "hello")
        self.assertFalse(history.more.get_sensitive())


class SettingsTests(unittest.TestCase):
    def test_private_settings_round_trip_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}):
            settings = Settings()
            settings.data["microphone"] = "device-name"
            settings.data["hold_to_talk"] = True
            settings.save()
            self.assertEqual(Settings().data, settings.data)
            self.assertEqual(settings.path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("token", settings.path.read_text())

    def test_autostart_desktop_file_validates_and_can_be_removed(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}):
            with patch("pathlib.Path.home", return_value=Path(directory)):
                launcher = Path(directory) / ".local/bin/sotto-linux"
                launcher.parent.mkdir(parents=True)
                launcher.write_text("#!/bin/sh\n")
                settings = Settings()
                settings.autostart(True)
                result = subprocess.run(["desktop-file-validate", str(settings.autostart_path)], capture_output=True)
                self.assertEqual(result.returncode, 0, result.stdout.decode())
                settings.autostart(False)
                self.assertFalse(settings.autostart_path.exists())


if __name__ == "__main__":
    unittest.main()
