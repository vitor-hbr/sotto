"""Device and desktop integration controls."""
from gi.repository import Gio, Gtk
from .audio import microphones
from .settings import Keyring
from .views import background, page


class Desktop:
    def __init__(self, app):
        self.app = app
        self.widget = page()
        self.status = Gtk.Label(label="Configure your Linux desktop integration.", wrap=True, xalign=0)
        self.widget.append(self.status)
        self.auto = Gtk.CheckButton(label="Insert automatically after shortcut dictation", active=app.config["auto_insert"])
        self.auto.connect("toggled", lambda button: self.save("auto_insert", button.get_active()))
        self.widget.append(self.auto)
        self.widget.append(Gtk.Label(label="Paste backend (restart Sotto after changing)", xalign=0))
        self.paste_backend = Gtk.ComboBoxText()
        self.paste_backend.append("portal", "Desktop portal — session permission required")
        self.paste_backend.append("gnome", "GNOME — Vicinae + ydotool, no Remote Desktop")
        self.paste_backend.set_active_id(app.config["paste_backend"])
        self.paste_backend.connect("changed", lambda widget: self.save("paste_backend", widget.get_active_id()))
        self.widget.append(self.paste_backend)
        self.widget.append(Gtk.Label(label="Insertion mode", xalign=0))
        self.delivery_mode = Gtk.ComboBoxText()
        for mode, label in (("guarded", "Guarded — original accessible text field"),
                            ("direct", "Direct paste — Ctrl+V"),
                            ("direct_terminal", "Direct terminal paste — Ctrl+Shift+V"),
                            ("direct_auto", "Direct paste — automatic app shortcut (GNOME)")):
            self.delivery_mode.append(mode, label)
        self.delivery_mode.set_active_id(app.config["delivery_mode"])
        self.delivery_mode.connect("changed", lambda widget: self.save("delivery_mode", widget.get_active_id()))
        self.widget.append(self.delivery_mode)
        self.widget.append(Gtk.Label(label="Automatic mode uses the Vicinae GNOME extension to recognize terminals. It sends Ctrl+Shift+V there and Ctrl+V elsewhere. Without that extension, it uses Ctrl+V; select terminal mode manually if needed.", wrap=True, xalign=0))
        self.notifications = Gtk.CheckButton(label="Show dictation notifications", active=app.config.get("notifications", True))
        self.notifications.connect("toggled", lambda button: self.save("notifications", button.get_active()))
        self.widget.append(self.notifications)
        schemas = Gio.SettingsSchemaSource.get_default()
        schema = schemas.lookup("org.gnome.desktop.interface", True) if schemas else None
        if schema and schema.has_key("toolkit-accessibility"):
            self.accessibility = Gio.Settings.new_full(schema, None, None)
            toggle = Gtk.CheckButton(label="Enable GNOME accessibility for text insertion",
                                     active=self.accessibility.get_boolean("toolkit-accessibility"))
            toggle.connect("toggled", self.accessibility_changed)
            self.widget.append(toggle)
        self.widget.append(Gtk.Label(label="Guarded mode checks the original field and caret. Direct paste uses the selected backend, not accessibility: it pastes into whatever is focused when transcription finishes. Keep your intended app focused. It replaces the clipboard and cannot confirm delivery. Recover missed text from History; no automatic retry is made.", wrap=True, xalign=0))
        self.shortcuts_status = Gtk.Label(label="Hold-to-talk is not connected.", wrap=True, xalign=0)
        self.widget.append(self.shortcuts_status)
        native_paste = app.paste is not None and not app.paste.requires_permission
        if native_paste and not app.shortcuts:
            self.shortcuts_status.set_text("Use a desktop shortcut bound to sotto-linux --toggle. No portal shortcut is needed.")
        else:
            self.button("Enable / configure hold-to-talk", self.shortcuts)
            self.button("Disable hold-to-talk", self.disable_shortcuts)
        message = ("GNOME paste requires the Vicinae extension and access to ydotoold. It replaces both clipboard and primary selection. No Remote Desktop session or permission popup is used. If unavailable, recover text from History; there is no portal fallback."
                   if native_paste else "Direct paste requires keyboard and clipboard permission for this session. Guarded accessible insertion works without it.")
        self.paste_status = Gtk.Label(label=message, wrap=True, xalign=0)
        self.widget.append(self.paste_status)
        if not native_paste:
            self.button("Enable portal paste", lambda _: app.paste.enable() if app.paste else self.paste_status.set_text("Desktop portals are unavailable."))
            self.button("Revoke this session's paste access", self.disable_paste)
        self.widget.append(Gtk.Label(label="Microphone", xalign=0))
        self.microphone = Gtk.ComboBoxText()
        self.widget.append(self.microphone)
        self.loading_mics = False
        self.microphone.connect("changed", self.microphone_changed)
        self.button("Refresh microphones", lambda _: self.refresh_mics())
        self.button("Test microphone (never inserts text)", lambda _: app.start_recording(mode="test"))
        self.widget.append(Gtk.Label(label="Use Stop recording on the Dictation tab to finish the test.", wrap=True, xalign=0))
        self.autostart = Gtk.CheckButton(label="Start Sotto when I log in", active=app.settings.autostart_path.exists())
        self.autostart.connect("toggled", self.startup_changed)
        self.widget.append(self.autostart)
        self.button("Save current server token in GNOME Keyring", self.save_token)
        self.button("Load this server's saved token", lambda _: app.load_token())
        self.button("Forget current server's saved token", self.forget_token)
        self.refresh_mics()

    def button(self, label, callback):
        button = Gtk.Button(label=label)
        button.connect("clicked", callback)
        self.widget.append(button)

    def accessibility_changed(self, button):
        if self.accessibility.set_boolean("toolkit-accessibility", button.get_active()):
            self.status.set_text("Accessibility setting updated. Restart destination applications if they do not expose text fields.")
        else:
            self.status.set_text("GNOME did not allow changing the accessibility setting.")

    def save(self, key, value):
        self.app.config[key] = value
        try:
            self.app.settings.save()
            if key == "paste_backend":
                self.status.set_text("Backend saved. Quit and reopen Sotto to apply it.")
            if key == "notifications" and not value:
                self.app.withdraw_notification("dictation")
        except OSError:
            self.status.set_text("Could not save device settings.")

    def shortcuts(self, _button):
        if self.app.shortcuts:
            self.save("hold_to_talk", True)
            self.app.shortcuts.enable()
        else:
            self.shortcuts_status.set_text("Desktop portals are unavailable.")

    def disable_shortcuts(self, _button):
        self.save("hold_to_talk", False)
        if self.app.shortcuts:
            self.app.shortcuts.disable()
        self.shortcuts_status.set_text("Hold-to-talk disabled. The --toggle command is still available.")

    def disable_paste(self, _button):
        if self.app.paste:
            self.app.paste.disable()
        self.paste_status.set_text("Portal paste disabled. Direct mode keeps transcripts for copying; guarded accessible insertion remains available.")

    def refresh_mics(self):
        self.loading_mics = True
        self.microphone.set_sensitive(False)
        def loaded(devices, error):
            self.microphone.remove_all()
            devices = devices or [("", "System default")]
            selected = self.app.config["microphone"]
            if selected and selected not in [item[0] for item in devices]:
                devices.append((selected, "Saved device (currently unavailable)"))
            for name, label in devices:
                self.microphone.append(name or "__default__", label)
            self.microphone.set_active_id(selected or "__default__")
            self.loading_mics = False
            self.microphone.set_sensitive(True)
            if error:
                self.status.set_text(error)
        background(microphones, loaded)

    def microphone_changed(self, widget):
        if not self.loading_mics:
            value = widget.get_active_id()
            self.save("microphone", "" if value == "__default__" else (value or ""))

    def startup_changed(self, widget):
        try:
            self.app.settings.autostart(widget.get_active())
            self.status.set_text("Login startup updated.")
        except OSError as exc:
            self.status.set_text(str(exc))

    def save_token(self, _button):
        try:
            client = self.app.client()
            Keyring.store(client.endpoint, client.token, lambda error: self.status.set_text(error or "Server token saved in GNOME Keyring."))
        except Exception:
            self.status.set_text("Check the server address before saving its token.")

    def forget_token(self, _button):
        try:
            client = self.app.client()
            Keyring.forget(client.endpoint, lambda error: self.status.set_text(error or "Saved token removed. The current session token remains until cleared or quit."))
        except Exception:
            self.status.set_text("Check the server address before removing its token.")
