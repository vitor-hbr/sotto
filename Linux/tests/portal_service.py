"""Isolated real D-Bus portal fixture. Never runs outside the test session bus."""
import json
import os
import sys
import threading
from gi.repository import Gio, GLib

BUS = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
PREFIX = "org.freedesktop.portal."
bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
              GLib.Variant("(su)", (BUS, 0)), None, Gio.DBusCallFlags.NONE, 1000, None)

interfaces = {
    "Registry": {"Register": ["s", "a{sv}"]},
    "GlobalShortcuts": {"CreateSession": ["a{sv}"], "BindShortcuts": ["o", "a(sa{sv})", "s", "a{sv}"]},
    "RemoteDesktop": {"CreateSession": ["a{sv}"], "SelectDevices": ["o", "a{sv}"], "Start": ["o", "s", "a{sv}"],
                      "NotifyKeyboardKeysym": ["o", "a{sv}", "i", "u"]},
    "Clipboard": {"RequestClipboard": ["o", "a{sv}"], "SetSelection": ["o", "a{sv}"],
                  "SelectionWrite": ["o", "u"], "SelectionWriteDone": ["o", "u", "b"]},
}
methods_with_response = {"CreateSession", "BindShortcuts", "SelectDevices", "Start"}
nodes = []
for interface, methods in interfaces.items():
    name = "org.freedesktop.host.portal.Registry" if interface == "Registry" else PREFIX + interface
    xml = f'<node><interface name="{name}">'
    for method, arguments in methods.items():
        xml += f'<method name="{method}">' + ''.join(f'<arg type="{arg}" direction="in"/>' for arg in arguments)
        if method in methods_with_response:
            xml += '<arg type="o" direction="out"/>'
        elif method == "SelectionWrite":
            xml += '<arg type="h" direction="out"/>'
        xml += '</method>'
    xml += '</interface></node>'
    nodes.append(Gio.DBusNodeInfo.new_for_xml(xml))


def log(method, args):
    with open(sys.argv[1], "a") as stream:
        stream.write(json.dumps([method, args]) + "\n")


def signal(session, active):
    bus.emit_signal(None, PATH, PREFIX + "GlobalShortcuts", "Activated" if active else "Deactivated",
                    GLib.Variant("(osta{sv})", (session, "dictate", 100, {})))
    return GLib.SOURCE_REMOVE


def called(connection, sender, path, interface, method, parameters, invocation):
    args = parameters.unpack()
    log(method, args)
    if method == "SelectionWrite":
        read_fd, write_fd = os.pipe()
        descriptors = Gio.UnixFDList.new()
        index = descriptors.append(write_fd)
        invocation.return_value_with_unix_fd_list(GLib.Variant("(h)", (index,)), descriptors)
        os.close(write_fd)
        def read_transfer():
            with os.fdopen(read_fd, "rb") as stream:
                content = stream.read()
            log("Transferred", [content.decode("utf-8")])
        threading.Thread(target=read_transfer, daemon=True).start()
        return
    if method == "NotifyKeyboardKeysym" and args[2:] == (0x76, 1):
        bus.emit_signal(sender, PATH, PREFIX + "Clipboard", "SelectionTransfer",
                        GLib.Variant("(osu)", (args[0], "text/plain;charset=utf-8", 1)))
    if method not in methods_with_response:
        invocation.return_value(GLib.Variant("()", ()))
        return
    options = args[-1]
    request = "/org/freedesktop/portal/desktop/request/" + sender[1:].replace(".", "_") + "/" + options["handle_token"]
    results = {}
    if method == "CreateSession":
        session = "/org/freedesktop/portal/desktop/session/test/" + options["session_handle_token"]
        results["session_handle"] = GLib.Variant("s", session)
    if method == "BindShortcuts":
        results["shortcuts"] = GLib.Variant("a(sa{sv})", [("dictate", {"trigger_description": GLib.Variant("s", "Ctrl+Alt+Space")})])
        GLib.timeout_add(150, signal, args[0], True)
        GLib.timeout_add(200, signal, args[0], True)  # Repeat must not restart a take.
        GLib.timeout_add(250, signal, args[0], False)
    if method == "Start":
        results = {"devices": GLib.Variant("u", 1), "clipboard_enabled": GLib.Variant("b", True)}
    code = 1 if os.environ.get("SOTTO_TEST_DENY") else 0
    # Deliberately emit before the method reply to test the subscription race.
    bus.emit_signal(sender, request, PREFIX + "Request", "Response", GLib.Variant("(ua{sv})", (code, results)))
    invocation.return_value(GLib.Variant("(o)", (request,)))


for node in nodes:
    bus.register_object(PATH, node.interfaces[0], called, None, None)
print("READY", flush=True)
GLib.MainLoop().run()
