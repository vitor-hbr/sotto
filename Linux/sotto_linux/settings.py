"""Device configuration, keyring credentials, and opt-in desktop autostart."""

import json
import os
from pathlib import Path
import tempfile
import uuid

import gi

gi.require_version("Secret", "1")
from gi.repository import Gio, Secret  # noqa: E402


class Settings:
    def __init__(self):
        self.root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        self.path = self.root / "sotto/client.json"
        self.data = {"endpoint": "http://localhost:8391", "device_id": str(uuid.uuid4()),
                     "microphone": "", "auto_insert": True, "hold_to_talk": False}
        self.error = None
        try:
            saved = json.loads(self.path.read_text())
            self.data.update(endpoint=str(saved["endpoint"]), device_id=str(uuid.UUID(saved["device_id"])))
            for key in ("microphone", "auto_insert", "hold_to_talk"):
                if key in saved and type(saved[key]) is type(self.data[key]):
                    self.data[key] = saved[key]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError):
            self.error = "Could not read saved settings. Check the server address before recording."

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(self.data, stream, indent=2)
                stream.write("\n")
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @property
    def autostart_path(self):
        return self.root / "autostart/io.github.vitor_hbr.Sotto.desktop"

    def autostart(self, enabled):
        if not enabled:
            self.autostart_path.unlink(missing_ok=True)
            return
        launcher = Path.home() / ".local/bin/sotto-linux"
        if not launcher.is_file() and not Path("/usr/bin/sotto-linux").is_file():
            raise OSError("Install Sotto before enabling login startup.")
        self.autostart_path.parent.mkdir(parents=True, exist_ok=True)
        command = ('Exec=sh -c "exec \\"\\$HOME/.local/bin/sotto-linux\\" --background"\n'
                   if launcher.is_file() else 'Exec=/usr/bin/sotto-linux --background\n')
        self.autostart_path.write_text('[Desktop Entry]\nType=Application\nName=Sotto\n' + command
                                       + 'X-GNOME-Autostart-enabled=true\n')


class Keyring:
    schema = Secret.Schema.new("io.github.vitor_hbr.Sotto", Secret.SchemaFlags.NONE,
                               {"endpoint": Secret.SchemaAttributeType.STRING})

    @classmethod
    def lookup(cls, endpoint, callback):
        def done(_source, result):
            try:
                callback(Secret.password_lookup_finish(result), None)
            except Exception:
                callback(None, "Keyring unavailable. Enter a token for this session.")
        Secret.password_lookup(cls.schema, {"endpoint": endpoint}, None, done)

    @classmethod
    def store(cls, endpoint, token, callback):
        def done(_source, result):
            try:
                callback(None if Secret.password_store_finish(result) else "Token could not be saved.")
            except Exception:
                callback("Keyring unavailable. The token remains in memory only.")
        Secret.password_store(cls.schema, {"endpoint": endpoint}, Secret.COLLECTION_DEFAULT,
                              "Sotto server token", token, None, done)

    @classmethod
    def forget(cls, endpoint, callback):
        def done(_source, result):
            try:
                Secret.password_clear_finish(result)
                callback(None)
            except Exception:
                callback("Could not remove the saved token from the keyring.")
        Secret.password_clear(cls.schema, {"endpoint": endpoint}, None, done)
