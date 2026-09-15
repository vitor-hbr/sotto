"""Separate native GTK application used to verify real accessibility insertion."""
import gi
import signal
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

app = Gtk.Application(application_id="io.github.vitor_hbr.Sotto.TestEditor")
def activate(application):
    window = Gtk.ApplicationWindow(application=application, title="Sotto insertion test", default_width=400, default_height=200)
    view = Gtk.TextView()
    view.get_buffer().set_text("before after")
    view.get_buffer().place_cursor(view.get_buffer().get_iter_at_offset(7))
    window.set_child(view)
    window.present()
    view.grab_focus()
    def move_caret():
        view.get_buffer().place_cursor(view.get_buffer().get_start_iter())
        return GLib.SOURCE_CONTINUE
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, move_caret)
    print("READY", flush=True)
app.connect("activate", activate)
app.run([])
