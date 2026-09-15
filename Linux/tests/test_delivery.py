import unittest
from types import SimpleNamespace
from sotto_linux.delivery import Anchor, Delivery, Atspi


class Field:
    def __init__(self, value="before after", caret=7):
        self.value, self.caret, self.calls = value, caret, []
        self.ambiguous = False

    def get_editable_text_iface(self):
        return self

    def get_role(self):
        return Atspi.Role.TEXT

    def insert_text(self, offset, text, length):
        self.calls.append((offset, text, length))
        self.value = self.value[:offset] + text + self.value[offset:]
        if self.ambiguous:
            raise RuntimeError("Connection lost after mutation")
        return True

    def get_text_iface(self):
        return self

    def get_text(self, start, end):
        return self.value[start:end]

    def get_character_count(self):
        return len(self.value)

    def set_caret_offset(self, value):
        self.caret = value

    def get_caret_offset(self):
        return self.caret


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.field = Field()
        self.anchor = Anchor(self.field, 7, 12, "before ", "after", 1)
        self.current = self.anchor
        self.tracker = SimpleNamespace(matches=lambda anchor: anchor == self.current,
                                       snapshot=lambda: self.current)
        self.delivery = Delivery(self.tracker)

    def test_unicode_insert_is_verified_and_not_repeated(self):
        outcome = self.delivery.insert("1", "olá 🐧 ", self.anchor, "server")
        self.assertEqual(outcome[0], "inserted")
        self.assertEqual(self.field.value, "before olá 🐧 after")
        self.assertEqual(self.field.calls[0][2], len("olá 🐧 ".encode()))
        self.assertEqual(self.delivery.insert("1", "olá 🐧 ", self.anchor, "server")[0], "unconfirmed")
        self.assertEqual(len(self.field.calls), 1)

    def test_caret_or_focus_change_prevents_mutation(self):
        self.current = None
        self.assertEqual(self.delivery.insert("1", "hello", self.anchor, "server")[0], "none")
        self.assertFalse(self.field.calls)

    def test_cancel_prevents_mutation(self):
        self.assertEqual(self.delivery.insert("1", "hello", self.anchor, "server", lambda: True)[0], "none")
        self.assertFalse(self.field.calls)

    def test_ambiguous_write_never_retries_as_paste(self):
        self.field.ambiguous = True
        self.delivery.paste = SimpleNamespace(ready=True, paste=lambda *_: self.fail("Must not paste after ambiguous insertion"))
        self.assertEqual(self.delivery.insert("1", "hello", self.anchor, "server")[0], "unconfirmed")
        self.assertEqual(len(self.field.calls), 1)

    def test_continuation_requires_the_verified_destination(self):
        self.delivery.insert("1", "hi ", self.anchor, "server")
        self.current = Anchor(self.field, 10, 15, "before hi ", "after", 3)
        self.delivery.remember("1", "server")
        self.assertEqual(self.delivery.continuation(self.current, "server"), "1")
        self.assertIsNone(self.delivery.continuation(self.current, "other server"))

    def test_focus_change_before_remember_does_not_attach_continuation(self):
        self.delivery.insert("1", "hi ", self.anchor, "server")
        self.current = Anchor(Field(), 10, 15, "before hi ", "after", 3)
        self.delivery.remember("1", "server")
        self.assertIsNone(self.delivery.continuation(self.current, "server"))


if __name__ == "__main__":
    unittest.main()
