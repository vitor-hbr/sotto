import unittest
from types import SimpleNamespace
from unittest.mock import Mock
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


class DirectPasteTests(unittest.TestCase):
    def setUp(self):
        self.paste = SimpleNamespace(ready=True, paste=Mock(return_value=True))
        self.delivery = Delivery(None, self.paste)

    def test_standard_and_terminal_paste_without_tracker(self):
        for terminal in (False, True):
            with self.subTest(terminal=terminal):
                generation_id = str(terminal)
                status, message = self.delivery.direct_paste(
                    generation_id, "olá 🐧", "server", terminal=terminal)
                self.assertEqual(status, "unconfirmed")
                self.assertIn("transcript remains in History", message)
                args, kwargs = self.paste.paste.call_args
                self.assertEqual(args, ("olá 🐧",))
                self.assertIs(kwargs["terminal"], terminal)
                self.assertTrue(kwargs["guard"]())
                self.assertIn(("server", generation_id), self.delivery.attempted)

    def test_default_shortcut_is_not_terminal(self):
        self.delivery.direct_paste("1", "hello", "server")
        self.assertIs(self.paste.paste.call_args.kwargs["terminal"], False)

    def test_cancelled_before_paste(self):
        outcome = self.delivery.direct_paste("1", "hello", "server", cancelled=lambda: True)
        self.assertEqual(outcome[0], "none")
        self.paste.paste.assert_not_called()
        self.assertFalse(self.delivery.attempted)

    def test_cancellation_guard_prevents_dispatch(self):
        cancelled = False
        dispatch = Mock()

        def paste(text, *, terminal, guard):
            nonlocal cancelled
            self.assertIn(("server", "1"), self.delivery.attempted)
            cancelled = True
            if not guard():
                return False
            dispatch()
            return True

        self.paste.paste.side_effect = paste
        outcome = self.delivery.direct_paste("1", "hello", "server", cancelled=lambda: cancelled)
        self.assertEqual(outcome[0], "none")
        dispatch.assert_not_called()
        self.assertEqual(self.delivery.direct_paste("1", "hello", "server")[0], "unconfirmed")
        self.paste.paste.assert_called_once()

    def test_absent_or_revoked_permission_never_pastes(self):
        for paste in (None, self.paste):
            with self.subTest(paste=paste):
                self.paste.ready = False
                self.delivery.paste = paste
                self.assertEqual(self.delivery.direct_paste("1", "hello", "server")[0], "none")
                self.paste.paste.assert_not_called()
                self.assertFalse(self.delivery.attempted)

    def test_empty_text_never_pastes(self):
        self.assertEqual(self.delivery.direct_paste("1", "", "server")[0], "none")
        self.paste.paste.assert_not_called()
        self.assertFalse(self.delivery.attempted)

    def test_ambiguous_error_is_marked_before_paste_and_never_retried(self):
        def paste(*args, **kwargs):
            self.assertIn(("server", "1"), self.delivery.attempted)
            raise RuntimeError("Connection lost after shortcut dispatch")

        self.paste.paste.side_effect = paste
        for _ in range(2):
            self.assertEqual(self.delivery.direct_paste("1", "hello", "server")[0], "unconfirmed")
        self.paste.paste.assert_called_once()

    def test_success_is_not_repeated_but_endpoint_is_part_of_key(self):
        for endpoint in ("server", "server", "other server"):
            self.assertEqual(self.delivery.direct_paste("1", "hello", endpoint)[0], "unconfirmed")
        self.assertEqual(self.paste.paste.call_count, 2)

    def test_attempts_are_shared_with_guarded_insert_in_both_directions(self):
        self.delivery.direct_paste("1", "hello", "server")
        # A duplicate must stop before trying to use the absent tracker.
        self.assertEqual(self.delivery.insert("1", "hello", None, "server")[0], "unconfirmed")
        field = Field()
        anchor = Anchor(field, 7, 12, "before ", "after", 1)
        self.delivery.tracker = SimpleNamespace(matches=lambda value: value == anchor)
        self.assertEqual(self.delivery.insert("2", "hello", anchor, "server")[0], "inserted")
        self.delivery.tracker = None
        self.assertEqual(self.delivery.direct_paste("2", "hello", "server")[0], "unconfirmed")
        self.paste.paste.assert_called_once()
        self.assertEqual(len(field.calls), 1)
        self.assertIsNone(self.delivery.pending)

    def test_continuation_is_cleared_even_when_paste_is_skipped(self):
        for text, duplicate in (("hello", False), ("", False), ("hello", True)):
            with self.subTest(text=text, duplicate=duplicate):
                self.delivery.previous = ("prior", None, "server", float("inf"))
                self.delivery.pending = ("prior", "server")
                self.delivery.attempted.clear()
                if duplicate:
                    self.delivery.attempted.add(("server", "1"))
                self.delivery.direct_paste("1", text, "server")
                self.assertIsNone(self.delivery.previous)
                self.assertIsNone(self.delivery.pending)
                self.assertIsNone(self.delivery.continuation(None, "server"))


if __name__ == "__main__":
    unittest.main()
