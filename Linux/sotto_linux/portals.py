"""GNOME/Wayland integration through consent-based XDG desktop portals."""

import os
import select
import threading
import time
import uuid

from gi.repository import Gio, GLib

BUS = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
PREFIX = "org.freedesktop.portal."


class PortalError(RuntimeError):
    pass


class Portal:
    def __init__(self):
        # GTK uses portals too. A dedicated connection lets this unsandboxed
        # client register its desktop identity before any portal request.
        address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
        self.bus = Gio.DBusConnection.new_for_address_sync(
            address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None)
        self.bus.set_exit_on_close(False)
        self.subscriptions = []
        self.sessions = set()
        self.session_callbacks = {}
        self.pending = {}
        self.register_identity()
        subscription = self.bus.signal_subscribe("org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged",
            "/org/freedesktop/DBus", BUS, Gio.DBusSignalFlags.NONE, self.owner_changed)
        self.subscriptions.append(subscription)

    def register_identity(self):
        try:
            self.bus.call_sync(BUS, PATH, "org.freedesktop.host.portal.Registry", "Register",
                               GLib.Variant("(sa{sv})", ("io.github.vitor_hbr.Sotto", {})),
                               None, Gio.DBusCallFlags.NONE, 3000, None)
        except GLib.Error:
            pass  # Older portals identify host applications without Registry.

    def owner_changed(self, _bus, _sender, _path, _interface, _signal, parameters):
        _name, old, new = parameters.unpack()
        if old:
            callbacks = list(self.session_callbacks.values())
            self.session_callbacks.clear()
            self.sessions.clear()
            for callback in callbacks:
                callback()
        if new:
            self.register_identity()

    def subscribe(self, interface, signal, callback, path=PATH):
        subscription = self.bus.signal_subscribe(BUS, PREFIX + interface, signal, path,
                                                None, Gio.DBusSignalFlags.NONE,
                                                lambda *args: callback(args[5].unpack()))
        self.subscriptions.append(subscription)
        return subscription

    def call(self, interface, method, signature, args):
        return self.bus.call_sync(BUS, PATH, PREFIX + interface, method,
                                  GLib.Variant(signature, args), None,
                                  Gio.DBusCallFlags.NONE, 5000, None)

    def request(self, interface, method, signature, args, callback):
        """Subscribe before calling: a fast portal may respond before its method reply."""
        token = "sotto_" + uuid.uuid4().hex
        sender = self.bus.get_unique_name()[1:].replace(".", "_")
        path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        options = dict(args[-1], handle_token=GLib.Variant("s", token))
        args = (*args[:-1], options)
        state = {"done": False, "subscription": 0, "timer": 0}

        def finish(results=None, error=None):
            if state["done"]:
                return
            state["done"] = True
            self.bus.signal_unsubscribe(state["subscription"])
            if state["timer"]:
                GLib.source_remove(state["timer"])
            self.pending.pop(path, None)
            callback(results, error)

        def response(_bus, _sender, _path, _interface, _signal, parameters):
            code, results = parameters.unpack()
            finish(results if code == 0 else None,
                   None if code == 0 else PortalError("Desktop permission was declined or cancelled."))

        def expired():
            state["timer"] = 0
            self.close_request(path)
            finish(error=PortalError("Desktop permission request timed out. Try enabling it again."))
            return GLib.SOURCE_REMOVE

        state["subscription"] = self.bus.signal_subscribe(
            BUS, PREFIX + "Request", "Response", path, None, Gio.DBusSignalFlags.NONE, response)
        state["timer"] = GLib.timeout_add_seconds(120, expired)
        self.pending[path] = (interface, lambda: finish(error=PortalError("Desktop integration stopped.")))

        def returned(connection, result):
            try:
                actual_path = connection.call_finish(result).unpack()[0]
                if actual_path != path:
                    self.close_request(actual_path)
                    finish(error=PortalError("Desktop portal returned an incompatible request handle."))
            except GLib.Error:
                finish(error=PortalError(f"{interface} is unavailable. Install/update the GNOME desktop portal."))

        self.bus.call(BUS, PATH, PREFIX + interface, method, GLib.Variant(signature, args),
                      GLib.VariantType.new("(o)"), Gio.DBusCallFlags.NONE, 10000, None, returned)

    def close_request(self, path):
        self.bus.call(BUS, path, PREFIX + "Request", "Close", None, None,
                      Gio.DBusCallFlags.NONE, 1000, None, None)

    def watch_session(self, path, closed):
        self.sessions.add(path)
        self.session_callbacks[path] = closed
        def lost(_parameters):
            self.sessions.discard(path)
            self.session_callbacks.pop(path, None)
            closed()
        self.subscribe("Session", "Closed", lost, path)

    def close_session(self, path):
        if path:
            self.sessions.discard(path)
            self.session_callbacks.pop(path, None)
            self.bus.call(BUS, path, PREFIX + "Session", "Close", None, None,
                          Gio.DBusCallFlags.NONE, 1000, None, None)

    def close(self):
        for path, (_interface, finish) in list(self.pending.items()):
            self.close_request(path)
            finish()
        for path in list(self.sessions):
            self.close_session(path)
        for subscription in self.subscriptions:
            self.bus.signal_unsubscribe(subscription)
        self.subscriptions.clear()
        self.bus.close_sync(None)

    def cancel_requests(self, interface):
        for path, (owner, finish) in list(self.pending.items()):
            if owner == interface:
                self.close_request(path)
                finish()


class Shortcuts:
    def __init__(self, portal, pressed, released, changed):
        self.portal = portal
        self.pressed = pressed
        self.released = released
        self.changed = changed
        self.session = None
        self.down = False
        self.enabling = False
        portal.subscribe("GlobalShortcuts", "Activated", self.activated)
        portal.subscribe("GlobalShortcuts", "Deactivated", self.deactivated)

    def enable(self):
        if self.enabling:
            return
        self.disable()
        self.enabling = True
        self.changed("Choose your hold-to-talk shortcut in the desktop dialog.")
        self.portal.request("GlobalShortcuts", "CreateSession", "(a{sv})",
                            ({"session_handle_token": GLib.Variant("s", "sotto_" + uuid.uuid4().hex)},), self.created)

    def created(self, result, error):
        if error:
            self.enabling = False
            self.changed(str(error))
            return
        self.session = result["session_handle"]
        session = self.session
        self.portal.watch_session(session, lambda: self.lost() if self.session == session else None)
        shortcuts = [("dictate", {"description": GLib.Variant("s", "Hold to dictate with Sotto"),
                                  "preferred_trigger": GLib.Variant("s", "CTRL+ALT+space")})]
        self.portal.request("GlobalShortcuts", "BindShortcuts", "(oa(sa{sv})sa{sv})",
                            (self.session, shortcuts, "", {}), self.bound)

    def bound(self, result, error):
        self.enabling = False
        if error or not any(item[0] == "dictate" for item in result.get("shortcuts", [])):
            self.disable()
            self.changed(str(error) if error else "No dictation shortcut was granted.")
            return
        description = next(item[1].get("trigger_description", "your chosen shortcut")
                           for item in result["shortcuts"] if item[0] == "dictate")
        self.changed("Hold-to-talk enabled: " + description)

    def activated(self, args):
        if args[0] == self.session and args[1] == "dictate" and not self.down:
            self.down = True
            self.pressed()

    def deactivated(self, args):
        if args[0] == self.session and args[1] == "dictate" and self.down:
            self.down = False
            self.released()

    def lost(self):
        self.session = None
        self.enabling = False
        self.portal.cancel_requests("GlobalShortcuts")
        if self.down:
            self.down = False
            self.released()
        self.changed("Hold-to-talk permission ended. Enable it again to reconnect.")

    def disable(self):
        self.portal.cancel_requests("GlobalShortcuts")
        self.portal.close_session(self.session)
        self.session = None
        if self.down:
            self.down = False
            self.released()


class Paste:
    """Optional clipboard + keyboard portal; no screen or pointer access requested."""
    def __init__(self, portal, changed):
        self.portal = portal
        self.changed = changed
        self.session = None
        self.ready = False
        self.enabling = False
        self.content = b""
        portal.subscribe("Clipboard", "SelectionTransfer", self.transfer)

    def enable(self):
        if self.enabling or self.ready:
            return
        self.enabling = True
        self.changed("Allow keyboard and clipboard access in the desktop dialog.")
        self.portal.request("RemoteDesktop", "CreateSession", "(a{sv})",
                            ({"session_handle_token": GLib.Variant("s", "sotto_" + uuid.uuid4().hex)},), self.created)

    def failed(self, error):
        self.disable()
        self.changed(str(error))

    def created(self, result, error):
        if error:
            self.failed(error)
            return
        self.session = result["session_handle"]
        session = self.session
        self.portal.watch_session(session, lambda: self.lost() if self.session == session else None)
        self.portal.request("RemoteDesktop", "SelectDevices", "(oa{sv})",
                            (self.session, {"types": GLib.Variant("u", 1)}), self.selected)

    def selected(self, result, error):
        if error:
            self.failed(error)
            return
        try:
            self.portal.call("Clipboard", "RequestClipboard", "(oa{sv})", (self.session, {}))
        except GLib.Error:
            self.failed(PortalError("This desktop does not provide the Clipboard portal. Accessible text insertion remains available."))
            return
        self.portal.request("RemoteDesktop", "Start", "(osa{sv})", (self.session, "", {}), self.started)

    def started(self, result, error):
        if error or not (result.get("devices", 0) & 1 and result.get("clipboard_enabled")):
            self.failed(error or PortalError("Keyboard and clipboard permissions are both required for portal paste."))
            return
        self.enabling = False
        self.ready = True
        self.changed("Portal paste enabled for this session.")

    def paste(self, text, terminal=False, guard=lambda: True):
        if not self.ready:
            raise PortalError("Enable portal paste in Desktop settings first.")
        self.content = text.encode("utf-8")
        self.portal.call("Clipboard", "SetSelection", "(oa{sv})",
                         (self.session, {"mime_types": GLib.Variant("as", ["text/plain;charset=utf-8", "text/plain"])}))
        if not guard():
            return False
        pressed = []
        try:
            for key in ([0xffe3, 0xffe1, 0x76] if terminal else [0xffe3, 0x76]):
                if not guard():
                    return False
                # Track before sending so even an ambiguous response gets a release.
                pressed.append(key)
                self.key(key, 1)
        finally:
            for key in reversed(pressed):
                try:
                    self.key(key, 0)
                except GLib.Error:
                    self.disable()
                    break
        return True

    def key(self, keysym, state):
        self.portal.call("RemoteDesktop", "NotifyKeyboardKeysym", "(oa{sv}iu)",
                         (self.session, {}, keysym, state))

    def transfer(self, args):
        session, mime, serial = args
        if session == self.session:
            content = self.content if mime in {"text/plain;charset=utf-8", "text/plain"} else None
            threading.Thread(target=self.write_transfer, args=(session, serial, content), daemon=True).start()

    def write_transfer(self, session, serial, content):
        success = False
        try:
            if content is not None:
                result, descriptors = self.portal.bus.call_with_unix_fd_list_sync(
                    BUS, PATH, PREFIX + "Clipboard", "SelectionWrite", GLib.Variant("(ou)", (session, serial)),
                    GLib.VariantType.new("(h)"), Gio.DBusCallFlags.NONE, 5000, None, None)
                fd = descriptors.get(result.unpack()[0])
                try:
                    os.set_blocking(fd, False)
                    remaining = memoryview(content)
                    deadline = time.monotonic() + 5
                    while remaining and time.monotonic() < deadline:
                        if select.select([], [fd], [], 0.1)[1]:
                            remaining = remaining[os.write(fd, remaining):]
                    success = not remaining
                finally:
                    os.close(fd)
        except (GLib.Error, OSError):
            pass
        finally:
            try:
                self.portal.call("Clipboard", "SelectionWriteDone", "(oub)", (session, serial, success))
            except GLib.Error:
                pass

    def lost(self):
        self.session = None
        self.ready = False
        self.enabling = False
        self.portal.cancel_requests("RemoteDesktop")
        self.content = b""
        self.changed("Portal paste permission ended. Enable it again to reconnect.")

    def disable(self):
        self.portal.cancel_requests("RemoteDesktop")
        self.portal.close_session(self.session)
        self.session = None
        self.ready = False
        self.enabling = False
        self.content = b""
