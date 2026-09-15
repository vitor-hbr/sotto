"""Structured dictionary editing with stable list and entry identities."""
import uuid
from gi.repository import Gtk


class Dictionary:
    def __init__(self):
        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.groups = []
        self.lists = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.widget.append(self.lists)
        add = Gtk.Button(label="Add dictionary list")
        add.connect("clicked", lambda _: self.add_list({"id": str(uuid.uuid4()), "name": "New list", "entries": []}))
        self.widget.append(add)

    def load(self, dictionary):
        for group in self.groups:
            self.lists.remove(group["widget"])
        self.groups.clear()
        for group in dictionary["lists"]:
            self.add_list(group)

    def add_list(self, data):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        header = Gtk.Box(spacing=6)
        name = Gtk.Entry(text=data["name"], placeholder_text="List name", hexpand=True)
        remove = Gtk.Button(label="Remove list")
        header.append(name)
        header.append(remove)
        box.append(header)
        entries = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(entries)
        group = {"widget": box, "name": name, "id": data["id"], "entries": [], "container": entries}
        def removed(_button):
            self.groups.remove(group)
            self.lists.remove(box)
        remove.connect("clicked", removed)
        add = Gtk.Button(label="Add word to this list")
        add.connect("clicked", lambda _: self.add_word(group, {"id": str(uuid.uuid4()), "term": "", "aliases": []}))
        box.append(add)
        self.groups.append(group)
        self.lists.append(box)
        for entry in data["entries"]:
            self.add_word(group, entry)

    def add_word(self, group, entry):
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_start=12)
        term = Gtk.Entry(text=entry["term"], placeholder_text="Preferred spelling")
        aliases = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        aliases.get_buffer().set_text("\n".join(entry.get("aliases", [])))
        row.append(term)
        row.append(Gtk.Label(label="Recognition aliases (one per line)", xalign=0))
        alias_scroll = Gtk.ScrolledWindow(min_content_height=50)
        alias_scroll.set_child(aliases)
        row.append(alias_scroll)
        footer = Gtk.Box(spacing=6)
        priority = Gtk.CheckButton(label="Priority recognition hint", active=entry.get("isPriority", False))
        remove = Gtk.Button(label="Remove word")
        footer.append(priority)
        footer.append(remove)
        row.append(footer)
        word = {"widget": row, "id": entry["id"], "term": term, "aliases": aliases, "priority": priority}
        def removed(_button):
            group["entries"].remove(word)
            group["container"].remove(row)
        remove.connect("clicked", removed)
        group["entries"].append(word)
        group["container"].append(row)

    def value(self):
        groups = []
        for group in self.groups:
            name = group["name"].get_text().strip()
            if not name:
                raise ValueError("Every dictionary list needs a name.")
            entries = []
            for word in group["entries"]:
                term = word["term"].get_text().strip()
                if not term:
                    raise ValueError("Every dictionary word needs a preferred spelling.")
                buffer = word["aliases"].get_buffer()
                aliases = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
                entries.append({"id": word["id"], "term": term,
                    "aliases": [alias.strip() for alias in aliases.splitlines() if alias.strip()],
                    "isPriority": word["priority"].get_active()})
            groups.append({"id": group["id"], "name": name, "entries": entries})
        return {"lists": groups}
