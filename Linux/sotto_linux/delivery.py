"""Local-only accessibility anchors and at-most-once text delivery."""

from dataclasses import dataclass
import os
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, Gio, GLib  # noqa: E402


def read_text(field, start, end):
    # Accessible.get_text() is a deprecated interface getter with the same
    # name; PyGObject resolves that method before Text.get_text(start, end).
    if isinstance(field, Atspi.Accessible):
        return Atspi.Text.get_text(field, start, end)
    return field.get_text(start, end)


@dataclass(frozen=True)
class Anchor:
    target: object
    caret: int
    count: int
    before: str
    after: str
    epoch: int


class FocusTracker:
    def __init__(self):
        # libatspi can abort the process if its bus launcher is missing. Check
        # the service first so the application can retain manual dictation.
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync("org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress", None,
                      GLib.VariantType.new("(s)"), Gio.DBusCallFlags.NONE, 3000, None)
        Atspi.init()
        Atspi.set_timeout(150, 300)
        self.focused = None
        self.epoch = 0
        self.listener = Atspi.EventListener.new(self.event)
        self.listener.register("object:state-changed:focused")
        self.listener.register("object:text-caret-moved")
        self.listener.register("object:text-changed")
        self.listener.register("object:text-selection-changed")

    def event(self, event, _data=None):
        if event.type.startswith("object:state-changed:focused"):
            self.epoch += 1
            if event.detail1:
                self.focused = event.source
        elif event.source == self.focused:
            self.epoch += 1

    def snapshot(self):
        target = self.focused
        if target is None:
            return None
        try:
            state = target.get_state_set()
            if (not state.contains(Atspi.StateType.FOCUSED)
                    or state.contains(Atspi.StateType.DEFUNCT)
                    or target.get_process_id() == os.getpid()
                    or target.get_role() == Atspi.Role.PASSWORD_TEXT):
                return None
            text = target.get_text_iface()
            if text is None or text.get_n_selections():
                return None
            caret, count = text.get_caret_offset(), text.get_character_count()
            if caret < 0 or caret > count:
                return None
            return Anchor(target, caret, count, read_text(text, max(0, caret - 128), caret),
                          read_text(text, caret, min(count, caret + 128)), self.epoch)
        except (GLib.Error, RuntimeError):
            return None

    def matches(self, anchor):
        return anchor is not None and self.snapshot() == anchor

    def close(self):
        for event in ("object:state-changed:focused", "object:text-caret-moved",
                      "object:text-changed", "object:text-selection-changed"):
            self.listener.deregister(event)


class Delivery:
    def __init__(self, tracker, paste=None):
        self.tracker = tracker
        self.paste = paste
        self.previous = None
        self.pending = None
        self.attempted = set()

    def continuation(self, anchor, endpoint):
        if self.previous:
            generation_id, prior, server, timestamp = self.previous
            if server == endpoint and time.monotonic() - timestamp < 60 and anchor == prior:
                return generation_id
        return None

    def insert(self, generation_id, text, anchor, endpoint, cancelled=lambda: False):
        self.previous = None
        self.pending = None
        key = (endpoint, generation_id)
        if key in self.attempted:
            return "unconfirmed", "Delivery was already attempted. Check the destination."
        if not text:
            return "none", "No text to insert."
        if cancelled() or not self.tracker.matches(anchor):
            return "none", "Destination changed or is inaccessible. Your transcript is ready to copy."
        try:
            editable = anchor.target.get_editable_text_iface()
            if cancelled():
                return "none", "Delivery cancelled. Your transcript is ready to copy."
            terminal_paste = self.paste and self.paste.ready and anchor.target.get_role() == Atspi.Role.TERMINAL
            if editable is not None and not terminal_paste:
                # A single mutation against an exact object, never the current
                # global focus. Do not retry an ambiguous mutation via paste.
                self.attempted.add(key)
                accepted = editable.insert_text(anchor.caret, text, len(text.encode("utf-8")))
                field = anchor.target.get_text_iface()
                inserted = read_text(field, anchor.caret, anchor.caret + len(text))
                if accepted and inserted == text and field.get_character_count() == anchor.count + len(text):
                    try:
                        field.set_caret_offset(anchor.caret + len(text))
                    except GLib.Error:
                        pass  # Some GTK versions implement insertion but not caret mutation.
                    if field.get_caret_offset() == anchor.caret + len(text):
                        self.pending = (generation_id, endpoint, anchor.target, anchor.caret + len(text),
                                        anchor.count + len(text), (anchor.before + text)[-128:], anchor.after)
                    return "inserted", "Inserted into your text field."
                return "unconfirmed", "Insertion could not be verified. Check the destination before copying."
            if self.paste is not None and self.paste.ready:
                if cancelled() or not self.tracker.matches(anchor):
                    return "none", "Destination changed. Your transcript is ready to copy."
                self.attempted.add(key)
                sent = self.paste.paste(text, terminal=anchor.target.get_role() == Atspi.Role.TERMINAL,
                                        guard=lambda: not cancelled() and self.tracker.matches(anchor))
                if not sent:
                    return "none", "Destination changed before paste. Your transcript is ready to copy."
                return "unconfirmed", "Paste sent to the original destination."
            return "none", "This application requires portal paste. Enable it in Desktop settings, or copy the transcript."
        except Exception:
            return "unconfirmed", "Delivery could not be verified. Check the destination before copying."

    def remember(self, generation_id, endpoint):
        anchor = self.tracker.snapshot()
        if anchor and self.pending == (generation_id, endpoint, anchor.target, anchor.caret,
                                       anchor.count, anchor.before, anchor.after):
            self.previous = (generation_id, anchor, endpoint, time.monotonic())
