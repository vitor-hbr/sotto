"""Native Linux dictation application and recording/delivery coordinator."""
import os
from pathlib import Path
import socket
import sys
import threading
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk
from .api import APIError, Client
from .audio import Microphone
from .delivery import Delivery, FocusTracker
from .desktop import Desktop
from .portals import Paste, Portal, Shortcuts
from .session import record
from .settings import Keyring, Settings
from .views import History, Preferences, background, page


class Application(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="io.github.vitor_hbr.Sotto", flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        for name, description in {"toggle": "Start or stop recording", "start": "Start recording (idempotent)",
                "stop": "Finish recording", "cancel": "Cancel recording", "quit": "Quit Sotto",
                "background": "Run without opening the window"}.items():
            self.add_main_option(name, 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE, description, None)
        self.settings = Settings()
        self.config = self.settings.data
        self.window = None
        self.busy = self.quitting = False
        self.stop, self.cancel = threading.Event(), threading.Event()
        self.portal = self.shortcuts = self.paste = self.tracker = self.delivery = None
        self.desktop = self.delivery_target = None
        self.reported = False

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.hold()
        try:
            self.portal = Portal()
            self.shortcuts = Shortcuts(self.portal, self.start_recording, self.stop_recording, self.shortcut_status)
            self.paste = Paste(self.portal, self.paste_status)
        except GLib.Error:
            pass
        try:
            self.tracker = FocusTracker()
            self.delivery = Delivery(self.tracker, self.paste)
        except (GLib.Error, RuntimeError):
            pass
        show = Gio.SimpleAction.new("show", None)
        show.connect("activate", lambda *_: self.activate())
        self.add_action(show)

    def do_shutdown(self):
        self.quitting = True
        if self.portal:
            self.portal.close()
        if self.tracker:
            self.tracker.close()
        if hasattr(self, "history"):
            self.history.stop_audio()
        Gtk.Application.do_shutdown(self)

    def do_command_line(self, command_line):
        options = command_line.get_options_dict().end().unpack()
        self.ensure_window()
        if "quit" in options:
            self.request_quit()
        elif "cancel" in options:
            self.cancel.set()
            self.stop_recording()
        elif "stop" in options:
            self.stop_recording()
        elif "start" in options:
            self.start_recording()
        elif "toggle" in options:
            self.toggle()
        elif "background" not in options:
            self.window.present()
        return 0

    def do_activate(self):
        self.ensure_window()
        self.window.present()

    def ensure_window(self):
        if self.window:
            return
        self.window = Gtk.ApplicationWindow(application=self, title="Sotto", default_width=740, default_height=700)
        self.window.set_hide_on_close(True)
        shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.window.set_child(shell)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        shell.append(Gtk.StackSwitcher(stack=self.stack, halign=Gtk.Align.CENTER, margin_top=12, margin_bottom=8))
        shell.append(self.stack)
        self.build_dictation()
        self.desktop = Desktop(self)
        self.preferences = Preferences(self.client)
        self.history = History(self.client, self.window)
        for name, title, widget in (("desktop", "Desktop", self.desktop.widget), ("server", "Server", self.preferences.widget),
                                     ("history", "History", self.history.widget)):
            scroll = Gtk.ScrolledWindow()
            scroll.set_child(widget)
            self.stack.add_titled(scroll, name, title)
        if self.settings.error:
            self.set_status(self.settings.error)
        self.load_token()
        if self.config["hold_to_talk"] and self.shortcuts:
            GLib.idle_add(self.enable_shortcuts)

    def enable_shortcuts(self):
        if not self.quitting:
            self.shortcuts.enable()
        return GLib.SOURCE_REMOVE

    def build_dictation(self):
        box = page()
        title = Gtk.Label(label="Your voice, where you work.", xalign=0)
        title.add_css_class("title-1")
        box.append(title)
        box.append(Gtk.Label(label="Hold your shortcut to speak. Release to transcribe and insert.\nSet up hold-to-talk on the Desktop tab.", xalign=0, wrap=True))
        box.append(Gtk.Label(label="Server address", xalign=0))
        self.endpoint = Gtk.Entry(text=self.config["endpoint"])
        box.append(self.endpoint)
        self.token = Gtk.PasswordEntry(show_peek_icon=True)
        box.append(Gtk.Label(label="Server token — use Desktop to save it in GNOME Keyring", xalign=0, wrap=True))
        box.append(self.token)
        self.endpoint.connect("changed", self.endpoint_changed)
        check = Gtk.Button(label="Check server connection")
        check.connect("clicked", self.check_server)
        box.append(check)
        self.status = Gtk.Label(label="Ready to connect", xalign=0, wrap=True, selectable=True)
        box.append(self.status)
        self.level = Gtk.LevelBar(min_value=0, max_value=1, value=0)
        box.append(self.level)
        row = Gtk.Box(spacing=8)
        self.record_button = Gtk.Button(label="Start recording")
        self.record_button.add_css_class("suggested-action")
        self.record_button.connect("clicked", lambda _: self.toggle())
        self.cancel_button = Gtk.Button(label="Cancel", sensitive=False)
        self.cancel_button.connect("clicked", lambda _: (self.cancel.set(), self.stop_recording()))
        row.append(self.record_button)
        row.append(self.cancel_button)
        box.append(row)
        self.transcript = Gtk.TextView(editable=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        scroll = Gtk.ScrolledWindow(vexpand=True, min_content_height=120)
        scroll.set_child(self.transcript)
        box.append(scroll)
        footer = Gtk.Box(spacing=8)
        self.copy_button = Gtk.Button(label="Copy transcript", sensitive=False)
        self.copy_button.connect("clicked", self.copy_transcript)
        footer.append(self.copy_button)
        quit_button = Gtk.Button(label="Quit Sotto")
        quit_button.connect("clicked", lambda _: self.request_quit())
        footer.append(quit_button)
        box.append(footer)
        box.append(Gtk.Label(label="Closing the window keeps shortcuts active. Changed destinations keep their transcript in Sotto.", xalign=0, wrap=True))
        self.stack.add_titled(box, "dictation", "Dictation")

    def client(self):
        return Client(self.endpoint.get_text(), self.token.get_text())

    def endpoint_changed(self, _entry):
        self.token.set_text("")
        if self.delivery:
            self.delivery.previous = None

    def load_token(self):
        token_file = os.environ.get("SOTTO_CLIENT_TOKEN_FILE")
        if token_file:
            try:
                self.token.set_text(Path(token_file).read_text().strip())
            except (OSError, UnicodeError):
                self.set_status("Could not read SOTTO_CLIENT_TOKEN_FILE. Enter a token below.")
            return
        try:
            endpoint = self.client().endpoint
        except APIError:
            return
        def loaded(token, error):
            if not self.quitting and self.endpoint.get_text() == endpoint and not self.token.get_text() and token:
                self.token.set_text(token)
        Keyring.lookup(endpoint, loaded)

    def check_server(self, button):
        try:
            client = self.client()
            self.config["endpoint"] = client.endpoint
            self.settings.save()
        except (APIError, OSError) as exc:
            self.set_status(str(exc))
            return
        button.set_sensitive(False)
        def checked(health, error):
            button.set_sensitive(True)
            if error:
                self.set_status(error)
            elif not isinstance(health, dict) or health.get("apiVersion") != 1:
                self.set_status("This server uses an unsupported API version.")
            else:
                self.set_status("Server ready for dictation." if health.get("ready") else "Server reachable; models are warming up or unavailable.")
        background(lambda: client.request("GET", "/v1/health"), checked)

    def shortcut_status(self, message):
        if self.desktop:
            self.desktop.shortcuts_status.set_text(message)

    def paste_status(self, message):
        if self.desktop:
            self.desktop.paste_status.set_text(message)

    def notify(self, message):
        notification = Gio.Notification.new("Sotto")
        notification.set_body(message)
        notification.set_default_action("app.show")
        self.send_notification("dictation", notification)

    def set_status(self, message):
        if self.quitting:
            return GLib.SOURCE_REMOVE
        previous = self.status.get_text()
        self.status.set_text(message)
        if message.startswith(("Transcribing", "Proofreading")):
            self.record_button.set_sensitive(False)
            self.record_button.set_label("Processing…")
        if message != previous and message.startswith(("Recording", "Transcribing")):
            self.notify(message)
        return GLib.SOURCE_REMOVE

    def toggle(self):
        self.stop_recording() if self.busy else self.start_recording()

    def stop_recording(self):
        self.stop.set()
        if self.busy:
            self.record_button.set_sensitive(False)
            self.record_button.set_label("Finishing…")

    def start_recording(self, mode="dictation"):
        if self.busy or self.quitting:
            return
        self.ensure_window()
        try:
            client = self.client()
            self.config["endpoint"] = client.endpoint
            self.settings.save()
        except (APIError, OSError) as exc:
            self.set_status(str(exc) if isinstance(exc, APIError) else "Cannot save device settings.")
            self.window.present()
            return
        anchor = self.tracker.snapshot() if self.tracker and mode == "dictation" else None
        continuation = self.delivery.continuation(anchor, client.endpoint) if self.delivery and anchor else None
        self.stop.clear()
        self.cancel.clear()
        self.busy = True
        self.reported = False
        self.delivery_target = None
        self.endpoint.set_sensitive(False)
        self.token.set_sensitive(False)
        self.record_button.set_label("Stop recording")
        self.record_button.set_sensitive(True)
        self.cancel_button.set_sensitive(True)
        self.copy_button.set_sensitive(False)
        self.transcript.get_buffer().set_text("")
        device = {"id": self.config["device_id"], "name": socket.gethostname()[:128]}
        microphone, auto_insert = self.config["microphone"], self.config["auto_insert"]
        self.stack.set_visible_child_name("dictation")
        def capture():
            return Microphone(self.stop, microphone, lambda value: GLib.idle_add(self.set_level, value))
        def work():
            return record(client, device, capture, self.stop, self.cancel,
                          lambda message: GLib.idle_add(self.set_status, message), mode=mode, continuation_id=continuation)
        background(work, lambda result, error: self.complete(client, result, error, mode, anchor, auto_insert))

    def set_level(self, value):
        if self.busy:
            self.level.set_value(value)
        return GLib.SOURCE_REMOVE

    def complete(self, client, result, error, mode="dictation", anchor=None, auto_insert=False):
        if self.quitting:
            self.quit()
            return
        if error or self.cancel.is_set():
            self.finish_ui("Recording cancelled." if self.cancel.is_set() else error, show=True)
            return
        text = result["insertionText"]
        self.transcript.get_buffer().set_text(text)
        self.copy_button.set_sensitive(bool(text))
        self.delivery_target = (client, result["id"])
        if mode == "test":
            self.report("tested", "Microphone test; no text insertion.")
            self.finish_ui("Microphone test complete. No text was inserted.", show=True)
        elif auto_insert and self.delivery and anchor and not self.cancel.is_set():
            self.copy_button.set_sensitive(False)
            self.set_status("Inserting…")
            def delivered(outcome, error):
                status, message = outcome if outcome else ("unconfirmed", "Delivery could not be verified. Check the destination.")
                if status != "none":
                    self.report(status, message)
                self.finish_ui(message, show=status == "none")
                if status == "inserted":
                    GLib.timeout_add(250, self.remember_delivery, result["id"], client.endpoint)
            background(lambda: self.delivery.insert(result["id"], text, anchor, client.endpoint, self.cancel.is_set), delivered)
        else:
            self.finish_ui("Transcript ready — copy and paste it into your application." if text else "No text to insert.", show=True)

    def remember_delivery(self, generation_id, endpoint):
        if self.delivery and not self.busy and not self.quitting:
            self.delivery.remember(generation_id, endpoint)
        return GLib.SOURCE_REMOVE

    def finish_ui(self, message, show=False):
        self.busy = False
        self.level.set_value(0)
        self.endpoint.set_sensitive(True)
        self.token.set_sensitive(True)
        self.record_button.set_sensitive(True)
        self.record_button.set_label("Start recording")
        self.cancel_button.set_sensitive(False)
        self.copy_button.set_sensitive(bool(self.transcript.get_buffer().get_char_count()))
        self.set_status(message)
        self.notify(message)
        if self.quitting:
            self.quit()
        elif show:
            self.window.present()

    def copy_transcript(self, _button):
        if self.busy:
            return
        buffer = self.transcript.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        if text:
            self.window.get_clipboard().set(text)
            self.set_status("Copied — switch to your app and paste.")
            self.report("copied", "Copied by the Linux client; paste is user-controlled.")

    def report(self, status, message):
        if self.reported or not self.delivery_target:
            return
        self.reported = True
        client, generation_id = self.delivery_target
        background(lambda: client.delivery(generation_id, status, message), lambda _result, _error: None)

    def request_quit(self):
        if self.busy:
            self.cancel.set()
            self.stop_recording()
            self.set_status("Cancelling before quitting…")
            self.quitting = True
        else:
            self.quit()


def main():
    return Application().run(sys.argv)
