import time
import unittest

from twguard import messages
from twguard.config import Config
from twguard.notify import send_due
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
        messages.claim(self.st, "status", msg)
        messages.claim(self.st, "issue:manual", msg)
        self.assertNotIn("status", self.st.messages)
        self.assertEqual(self.st.messages["issue:manual"], msg)

    def test_ephemeral_deleted_after_ttl(self):
        sent = self.tg.send("kurz")
        messages.expire(self.st, sent, 60)
        messages.cleanup(self.tg, self.st, time.time() + 30)
        self.assertEqual(self.tg.deleted, [])
        messages.cleanup(self.tg, self.st, time.time() + 61)
        self.assertEqual(self.tg.deleted, [sent["message_id"]])
        self.assertEqual(self.st.ephemeral, [])


class ReminderTest(unittest.TestCase):
    def setUp(self):
        self.tg = FakeTelegram()
        self.st = State()
        self.cfg = Config(hostname="server1")
        self.cfg.escalation.quiet_hours = ""

    def test_reminder_replaces_previous_reminder(self):
        now = time.time()
        self.st.open_issue("reboot", "Kernel", now=now - 100 * 3600)
        send_due(self.cfg, self.st, self.tg, now)
        send_due(self.cfg, self.st, self.tg, now + 8 * 3600)
        self.assertEqual(len(self.tg.sent), 2)
        self.assertEqual(self.tg.deleted, [101])
        self.assertEqual(self.st.messages["issue:reboot"]["message_id"], 102)

    def test_resolved_issue_stays_as_done(self):
        self.st.open_issue("reboot", "Kernel")
        send_due(self.cfg, self.st, self.tg)
        self.st.resolve("reboot")
        send_due(self.cfg, self.st, self.tg)
        message_id, text, buttons = self.tg.edited[0]
        self.assertEqual(message_id, 101)
        self.assertIn("erledigt", text)
        self.assertIsNone(buttons)
        self.assertNotIn("issue:reboot", self.st.messages)


if __name__ == "__main__":
    unittest.main()
