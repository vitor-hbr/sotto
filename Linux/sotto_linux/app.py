"""GTK 4 desktop application and single-instance shortcut entry point."""

import json
import os
from pathlib import Path
import socket
import sys
import threading
import uuid

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from .api import APIError, Client
from .audio import Microphone
from .session import record


class Application(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="io.github.vitor_hbr.Sotto",
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        for option in ("toggle", "stop", "cancel", "quit"):
            self.add_main_option(option, 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                                 {"toggle": "Start or stop recording without focusing the window",
                                  "stop": "Finish the current recording", "cancel": "Cancel the current recording",
                                  "quit": "Quit after cancelling any recording"}[option], None)
        self.window = None
        self.busy = False
        self.quitting = False
        self.stop = threading.Event()
        self.cancel = threading.Event()
        self.config_path = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "sotto/client.json"
        self.config = {"endpoint": "http://localhost:8391", "device_id": str(uuid.uuid4())}
        self.config_error = None
        try:
            saved = json.loads(self.config_path.read_text())
            self.config.update(endpoint=str(saved["endpoint"]), device_id=str(uuid.UUID(saved["device_id"])))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError):
            self.config_error = "Could not read saved settings. Check the server address before recording."

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.hold()  # Keep clipboard ownership and shortcut handling when hidden.

    def do_command_line(self, command_line):
        options = command_line.get_options_dict().end().unpack()
        self.ensure_window()
        if "quit" in options:
            self.request_quit()
        elif "cancel" in options:
            self.cancel.set()
            self.stop.set()
        elif "stop" in options:
            self.stop.set()
        elif "toggle" in options:
            self.toggle()
        else:
            self.window.present()
        return 0

    def do_activate(self):
        self.ensure_window()
        self.window.present()

    def ensure_window(self):
        if self.window is not None:
            return
        self.window = Gtk.ApplicationWindow(application=self, title="Sotto")
        self.window.set_default_size(520, 560)
        self.window.set_hide_on_close(True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + side)(24)
        self.window.set_child(box)
        title = Gtk.Label(label="Speak. Copy. Paste.", xalign=0)
        title.add_css_class("title-1")
        box.append(title)
        description = Gtk.Label(label="Dictation for your Linux desktop.\nUse the default microphone selected in GNOME Sound settings.",
                                xalign=0, wrap=True)
        box.append(description)
        box.append(Gtk.Label(label="Server address", xalign=0))
        self.endpoint = Gtk.Entry(text=self.config["endpoint"])
        box.append(self.endpoint)
        box.append(Gtk.Label(label="Server token (kept in memory for this session)", xalign=0))
        self.token = Gtk.PasswordEntry(show_peek_icon=True)
        token_file = os.environ.get("SOTTO_CLIENT_TOKEN_FILE")
        if token_file:
            try:
                self.token.set_text(Path(token_file).read_text().strip())
            except OSError:
                self.config_error = "Could not read SOTTO_CLIENT_TOKEN_FILE. Enter the token below."
        box.append(self.token)
        self.status = Gtk.Label(label=self.config_error or "Ready to connect", xalign=0, wrap=True)
        self.status.set_selectable(True)
        box.append(self.status)
        row = Gtk.Box(spacing=8)
        self.record_button = Gtk.Button(label="Start recording")
        self.record_button.add_css_class("suggested-action")
        self.record_button.connect("clicked", lambda _: self.toggle())
        row.append(self.record_button)
        self.cancel_button = Gtk.Button(label="Cancel", sensitive=False)
        self.cancel_button.connect("clicked", lambda _: (self.cancel.set(), self.stop.set()))
        row.append(self.cancel_button)
        box.append(row)
        self.transcript = Gtk.TextView(editable=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        scroller = Gtk.ScrolledWindow(vexpand=True, min_content_height=140)
        scroller.set_child(self.transcript)
        box.append(scroller)
        footer = Gtk.Box(spacing=8)
        self.copy_button = Gtk.Button(label="Copy transcript", sensitive=False)
        self.copy_button.connect("clicked", self.copy_transcript)
        footer.append(self.copy_button)
        quit_button = Gtk.Button(label="Quit Sotto")
        quit_button.connect("clicked", lambda _: self.request_quit())
        footer.append(quit_button)
        box.append(footer)
        box.append(Gtk.Label(label="Bind sotto-linux --toggle in GNOME Keyboard → Custom Shortcuts.\n"
                             "Paste with Ctrl+V (Ctrl+Shift+V in terminals). Closing this window keeps Sotto running.",
                             wrap=True, xalign=0))

    def set_status(self, message):
        self.status.set_text(message)
        if message.startswith(("Transcribing", "Proofreading")):
            self.record_button.set_sensitive(False)
            self.record_button.set_label("Processing…")
        return GLib.SOURCE_REMOVE

    def toggle(self):
        if self.busy:
            self.stop.set()
            self.record_button.set_sensitive(False)
            self.record_button.set_label("Finishing…")
            return
        try:
            client = Client(self.endpoint.get_text(), self.token.get_text())
            self.config["endpoint"] = client.endpoint
            self.config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.config_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.config, indent=2) + "\n")
            os.chmod(temporary, 0o600)
            temporary.replace(self.config_path)
        except (APIError, OSError) as exc:
            self.set_status(str(exc) if isinstance(exc, APIError) else "Cannot save settings in the configuration directory.")
            self.window.present()
            return
        self.stop.clear()
        self.cancel.clear()
        self.busy = True
        self.endpoint.set_sensitive(False)
        self.token.set_sensitive(False)
        self.record_button.set_label("Stop recording")
        self.cancel_button.set_sensitive(True)
        self.copy_button.set_sensitive(False)
        self.transcript.get_buffer().set_text("")
        device = {"id": self.config["device_id"], "name": socket.gethostname()[:128]}
        threading.Thread(target=self.run_recording, args=(client, device), daemon=True).start()

    def run_recording(self, client, device):
        try:
            result = record(client, device, lambda: Microphone(self.stop), self.stop, self.cancel,
                            lambda message: GLib.idle_add(self.set_status, message))
            GLib.idle_add(self.complete, client, result, None)
        except Exception as exc:
            message = str(exc) if isinstance(exc, APIError) else "Recording failed. Check the server and microphone dependencies."
            GLib.idle_add(self.complete, client, None, message)

    def complete(self, client, result, error):
        self.busy = False
        self.endpoint.set_sensitive(True)
        self.token.set_sensitive(True)
        self.record_button.set_sensitive(True)
        self.record_button.set_label("Start recording")
        self.cancel_button.set_sensitive(False)
        if self.quitting:
            self.quit()
            return GLib.SOURCE_REMOVE
        if self.cancel.is_set():
            self.set_status("Recording cancelled.")
        elif error:
            self.set_status(error)
            self.window.present()
        else:
            text = result["insertionText"]
            self.transcript.get_buffer().set_text(text)
            self.copy_button.set_sensitive(bool(text))
            # Wayland clipboard ownership is tied to input focus. Present the
            # result and require an explicit Copy click instead of falsely
            # reporting a background clipboard update as delivered.
            self.set_status("Transcript ready — click Copy transcript, then paste." if text else "No text to insert.")
            self.delivery_target = (client, result["id"])
            self.window.present()
        return GLib.SOURCE_REMOVE

    def copy_transcript(self, _button):
        buffer = self.transcript.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        if text:
            self.window.get_clipboard().set(text)
            self.set_status("Copied — switch to your app and paste.")
            client, generation_id = self.delivery_target
            threading.Thread(target=self.report_delivery, args=(client, generation_id), daemon=True).start()

    def report_delivery(self, client, generation_id):
        try:
            client.delivery(generation_id)
        except Exception:
            # Clipboard delivery has already happened; never retry or copy again.
            pass

    def request_quit(self):
        if self.busy:
            self.quitting = True
            self.cancel.set()
            self.stop.set()
            self.set_status("Cancelling before quitting…")
        else:
            self.quit()


def main():
    return Application().run(sys.argv)
