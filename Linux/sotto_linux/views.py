"""Shared server preferences, dictionary editor, and paginated history."""

import copy
from pathlib import Path
import tempfile
import threading

from gi.repository import GLib, Gtk, Gst

from .api import APIError
from .dictionary import Dictionary


def background(work, done):
    def run():
        try:
            result, error = work(), None
        except Exception as exc:
            result = None
            error = str(exc) if isinstance(exc, (APIError, ValueError)) else "Operation failed. Check the server connection and local permissions."
        GLib.idle_add(done, result, error)
    threading.Thread(target=run, daemon=True).start()


def text_value(view):
    buffer = view.get_buffer()
    return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)


def text_editor(parent, label, height=90):
    parent.append(Gtk.Label(label=label, xalign=0))
    view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
    scroll = Gtk.ScrolledWindow(min_content_height=height)
    scroll.set_child(view)
    parent.append(scroll)
    return view


def page():
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    for side in ("top", "bottom", "start", "end"):
        getattr(box, "set_margin_" + side)(20)
    return box


class Preferences:
    def __init__(self, client):
        self.client = client
        self.widget = page()
        self.snapshot = None
        self.status = Gtk.Label(label="Load server preferences to edit shared settings.", wrap=True, xalign=0)
        self.widget.append(self.status)
        self.reload = Gtk.Button(label="Load / reload from server")
        self.reload.connect("clicked", lambda _: self.load())
        self.widget.append(self.reload)
        self.form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, sensitive=False)
        self.widget.append(self.form)
        self.form.append(Gtk.Label(label="Language", xalign=0))
        self.language = Gtk.ComboBoxText()
        for code in ("auto", "en", "es", "fr", "de", "it", "pt", "nl", "ja", "zh", "ko", "hi", "ar", "pl", "ru", "uk", "sv"):
            self.language.append(code, code)
        self.form.append(self.language)
        self.correction = Gtk.CheckButton(label="Proofread with the server's text model")
        self.retention = Gtk.CheckButton(label="Keep original microphone audio on the server")
        self.form.append(self.correction)
        self.form.append(self.retention)
        self.prompt = text_editor(self.form, "Cleanup instructions", 150)
        self.vocabulary = text_editor(self.form, "Recognition hints (one per line or comma-separated)")
        self.form.append(Gtk.Label(label="Dictionary — preferred words, recognition aliases, and named lists", xalign=0, wrap=True))
        self.dictionary = Dictionary()
        self.form.append(self.dictionary.widget)
        save = Gtk.Button(label="Save shared preferences")
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda _: self.save())
        self.form.append(save)

    def busy(self, active):
        self.reload.set_sensitive(not active)
        self.form.set_sensitive(not active and self.snapshot is not None)

    def load(self):
        try:
            candidate = self.client()
        except APIError as exc:
            self.status.set_text(str(exc))
            return
        self.busy(True)
        self.status.set_text("Loading…")
        def loaded(snapshot, error):
            if not error:
                self.loaded_client = candidate
            self.loaded(snapshot, error)
        background(candidate.preferences, loaded)

    def loaded(self, snapshot, error):
        self.busy(False)
        if error:
            self.status.set_text(error + " Your edits have been kept.")
            return
        self.snapshot = snapshot
        preferences = snapshot["preferences"]
        self.language.set_active_id(preferences["language"])
        self.correction.set_active(preferences["textCorrectionEnabled"])
        self.retention.set_active(preferences["keepOriginalAudio"])
        self.prompt.get_buffer().set_text(preferences["proofreadingPrompt"])
        self.vocabulary.get_buffer().set_text(preferences["vocabulary"])
        self.dictionary.load(preferences["dictionary"])
        self.busy(False)
        self.status.set_text(f"Shared settings loaded (revision {snapshot['revision']}).")

    def save(self):
        if self.snapshot is None:
            return
        try:
            if self.client().endpoint != self.loaded_client.endpoint:
                raise ValueError("Server address changed. Reload preferences before saving.")
            snapshot = copy.deepcopy(self.snapshot)
            preferences = snapshot["preferences"]
            preferences.update(language=self.language.get_active_id(), textCorrectionEnabled=self.correction.get_active(),
                               keepOriginalAudio=self.retention.get_active(), proofreadingPrompt=text_value(self.prompt),
                               vocabulary=text_value(self.vocabulary))
            preferences["dictionary"] = self.dictionary.value()
        except (APIError, ValueError) as exc:
            self.status.set_text(str(exc))
            return
        self.busy(True)
        self.status.set_text("Saving…")
        background(lambda: self.loaded_client.preferences(snapshot), self.saved)

    def saved(self, snapshot, error):
        if error:
            self.busy(False)
            self.status.set_text(error + " Edits kept. If another device changed settings, reload before saving again.")
        else:
            self.loaded(snapshot, None)
            self.status.set_text("Shared preferences saved.")


class History:
    def __init__(self, client, parent):
        self.client = client
        self.parent = parent
        self.widget = page()
        self.cursor = None
        self.selected = None
        self.records = []
        self.player = None
        self.audio_path = None
        self.playback_epoch = 0
        self.status = Gtk.Label(label="Recordings are stored on your server.", wrap=True, xalign=0)
        self.widget.append(self.status)
        buttons = Gtk.Box(spacing=8)
        self.refresh = Gtk.Button(label="Refresh history")
        self.more = Gtk.Button(label="Load older", sensitive=False)
        self.refresh.connect("clicked", lambda _: self.load(False))
        self.more.connect("clicked", lambda _: self.load(True))
        buttons.append(self.refresh)
        buttons.append(self.more)
        self.widget.append(buttons)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.list.connect("row-selected", self.select)
        scroll = Gtk.ScrolledWindow(min_content_height=180)
        scroll.set_child(self.list)
        self.widget.append(scroll)
        self.detail = text_editor(self.widget, "Transcript", 160)
        self.detail.set_editable(False)
        self.original = text_editor(self.widget, "Raw recognition", 90)
        self.original.set_editable(False)
        self.actions = Gtk.Box(spacing=8, sensitive=False)
        for label, callback in (("Copy", self.copy), ("Play audio", self.play),
                                ("Stop audio", lambda _: self.stop_audio()), ("Export text", self.export),
                                ("Delete…", self.delete)):
            button = Gtk.Button(label=label)
            button.connect("clicked", callback)
            self.actions.append(button)
        self.widget.append(self.actions)

    def load(self, older):
        try:
            client = self.client()
        except APIError as exc:
            self.status.set_text(str(exc))
            return
        if older and client.endpoint != self.loaded_client.endpoint:
            self.status.set_text("Server changed. Refresh history first.")
            return
        cursor = self.cursor if older else None
        self.refresh.set_sensitive(False)
        self.more.set_sensitive(False)
        self.actions.set_sensitive(False)
        def loaded(result, error):
            self.refresh.set_sensitive(True)
            if error:
                self.status.set_text(error)
                return
            if not older:
                while self.list.get_row_at_index(0):
                    self.list.remove(self.list.get_row_at_index(0))
                self.records.clear()
                self.detail.get_buffer().set_text("")
                self.original.get_buffer().set_text("")
            self.loaded_client = client
            for record in result["items"]:
                self.records.append(record)
                text = record.get("finalText") or record.get("rawText") or record["status"]
                label = Gtk.Label(label=f"{record['createdAt']} · {record['device']['name']}\n{text[:100]}",
                                  xalign=0, wrap=True, margin_top=8, margin_bottom=8)
                self.list.append(label)
            self.cursor = result.get("nextCursor")
            self.more.set_sensitive(bool(self.cursor))
            self.status.set_text(f"{len(self.records)} recordings loaded. Selecting history never inserts text.")
        background(lambda: client.history(cursor), loaded)

    def select(self, _list, row):
        self.stop_audio()
        self.selected = self.records[row.get_index()] if row is not None else None
        self.actions.set_sensitive(self.selected is not None)
        if self.selected:
            self.detail.get_buffer().set_text(self.selected.get("finalText", ""))
            self.original.get_buffer().set_text(self.selected.get("rawText", ""))

    def copy(self, _button):
        self.parent.get_clipboard().set(text_value(self.detail))
        self.status.set_text("Transcript copied.")

    def play(self, _button):
        if not self.selected:
            return
        self.stop_audio()
        epoch = self.playback_epoch
        record_id = self.selected["id"]
        client = self.loaded_client
        self.actions.set_sensitive(False)
        def loaded(audio, error):
            self.actions.set_sensitive(self.selected is not None)
            if error:
                self.status.set_text(error)
                return
            if epoch != self.playback_epoch or not self.selected or self.selected["id"] != record_id:
                return
            with tempfile.NamedTemporaryFile(prefix="sotto-", suffix=".wav", delete=False) as stream:
                stream.write(audio)
                self.audio_path = Path(stream.name)
            self.player = Gst.ElementFactory.make("playbin", None)
            self.player.set_property("uri", self.audio_path.as_uri())
            bus = self.player.get_bus()
            bus.add_signal_watch()
            bus.connect("message", self.playback_message)
            if self.player.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                self.stop_audio()
                self.status.set_text("Audio playback could not start.")
        background(lambda: client.artifact(record_id, "inference.wav"), loaded)

    def playback_message(self, _bus, message):
        if message.type in (Gst.MessageType.ERROR, Gst.MessageType.EOS):
            self.stop_audio()
            if message.type == Gst.MessageType.ERROR:
                self.status.set_text("Audio playback failed. Check your output device.")

    def stop_audio(self):
        self.playback_epoch += 1
        if self.player:
            self.player.set_state(Gst.State.NULL)
            self.player.get_bus().remove_signal_watch()
            self.player = None
        if self.audio_path:
            self.audio_path.unlink(missing_ok=True)
            self.audio_path = None

    def export(self, _button):
        text = text_value(self.detail)
        chooser = Gtk.FileChooserNative(title="Export transcript", transient_for=self.parent,
                                        action=Gtk.FileChooserAction.SAVE, accept_label="Save", cancel_label="Cancel")
        chooser.set_current_name("sotto-transcript.txt")
        def response(dialog, result):
            if result == Gtk.ResponseType.ACCEPT:
                try:
                    file = dialog.get_file()
                    if file.get_path() is None:
                        raise OSError("Choose a local file")
                    Path(file.get_path()).write_text(text, encoding="utf-8")
                    self.status.set_text("Transcript exported.")
                except OSError:
                    self.status.set_text("Could not write the chosen file.")
            dialog.destroy()
        chooser.connect("response", response)
        chooser.show()

    def delete(self, _button):
        if not self.selected:
            return
        record_id = self.selected["id"]
        client = self.loaded_client
        dialog = Gtk.MessageDialog(transient_for=self.parent, modal=True,
                                   message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.OK_CANCEL,
                                   text="Delete this recording and its audio from the server?")
        def response(window, result):
            window.destroy()
            if result == Gtk.ResponseType.OK:
                self.actions.set_sensitive(False)
                def deleted(_value, error):
                    if error:
                        self.actions.set_sensitive(True)
                        self.status.set_text(error)
                    else:
                        self.load(False)
                background(lambda: client.delete(record_id), deleted)
        dialog.connect("response", response)
        dialog.show()
