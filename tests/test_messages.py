import time
import unittest

import testlang  # noqa: F401
from twguard import messages
from twguard.state import State

from fakes import FakeTelegram


class MessagesTest(unittest.TestCase):
    def setUp(self):
        self.tg = FakeTelegram()
        self.st = State()

    def test_replace_removes_previous(self):
        first = messages.replace(self.tg, self.st, "status", "eins")
        second = messages.replace(self.tg, self.st, "status", "zwei")
        self.assertEqual(self.tg.deleted, [first["message_id"]])
        self.assertEqual(self.st.messages["status"]["message_id"], second["message_id"])

    def test_old_message_is_shortened_instead_of_deleted(self):
        self.st.messages["briefing"] = {"chat_id": -100, "message_id": 7, "at": time.time() - 50 * 3600}
        messages.replace(self.tg, self.st, "briefing", "neu")
        self.assertEqual(self.tg.deleted, [])
        self.assertEqual(self.tg.edited, [(7, messages.REPLACED, None)])

    def test_failed_delete_falls_back_to_edit(self):
        self.tg.fail_delete = True
        messages.replace(self.tg, self.st, "status", "eins")
        messages.replace(self.tg, self.st, "status", "zwei")
        self.assertEqual(self.tg.edited[0][1], messages.REPLACED)

    def test_claim_moves_message_between_topics(self):
        msg = messages.ref(self.tg.send("x"))
        messages.claim(self.st, "language", msg)
        messages.claim(self.st, "mode", msg)
        self.assertNotIn("language", self.st.messages)
        self.assertEqual(self.st.messages["mode"], msg)

    def test_ephemeral_deleted_after_ttl(self):
        sent = self.tg.send("kurz")
        messages.expire(self.st, sent, 60)
        self.assertEqual(messages.expired(self.st, time.time() + 30), [])
        due = messages.expired(self.st, time.time() + 61)
        self.assertEqual([m["message_id"] for m in due], [sent["message_id"]])
        self.assertEqual(self.st.ephemeral, [])


if __name__ == "__main__":
    unittest.main()
