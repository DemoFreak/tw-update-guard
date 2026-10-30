import time
import unittest

import testlang  # noqa: F401
from twguard.config import Escalation
from twguard.notify import fmt_age, in_quiet_hours, is_due, level
from twguard.state import Issue

H = 3600


def at(hour: int, minute: int = 0) -> float:
    """Zeitstempel heute zur angegebenen Ortszeit."""
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, hour, minute, 0, 0, 0, -1))


class LadderTest(unittest.TestCase):
    def setUp(self):
        self.esc = Escalation(quiet_hours="")

    def test_level(self):
        ladder = self.esc.ladder
        self.assertEqual(level(0, ladder), 0)
        self.assertEqual(level(47, ladder), 0)
        self.assertEqual(level(48, ladder), 1)
        self.assertEqual(level(500, ladder), 3)
        self.assertIsNone(level(10, self.esc.ladder_error))

    def test_first_notification_immediately(self):
        now = time.time()
        self.assertTrue(is_due(Issue("manual", since=now), now, self.esc))

    def test_interval_shrinks_with_age(self):
        now = time.time()
        young = Issue("manual", since=now - 10 * H, last_notified=now - 9 * H)
        self.assertFalse(is_due(young, now, self.esc))  # Stufe 0: alle 24 h
        old = Issue("manual", since=now - 130 * H, last_notified=now - 4 * H)
        self.assertTrue(is_due(old, now, self.esc))  # Stufe 2: alle 3 h

    def test_error_waits_a_day(self):
        now = time.time()
        self.assertFalse(is_due(Issue("error", since=now - 5 * H), now, self.esc))
        self.assertTrue(is_due(Issue("error", since=now - 25 * H), now, self.esc))

    def test_snooze(self):
        now = time.time()
        issue = Issue("manual", since=now - 100 * H, snoozed_until=now + H)
        self.assertFalse(is_due(issue, now, self.esc))
        self.assertTrue(is_due(issue, now + 2 * H, self.esc))

    def test_quiet_hours_over_midnight(self):
        spec = "22:00-07:00"
        self.assertTrue(in_quiet_hours(at(23), spec))
        self.assertTrue(in_quiet_hours(at(3), spec))
        self.assertFalse(in_quiet_hours(at(7), spec))
        self.assertFalse(in_quiet_hours(at(12), spec))
        self.assertTrue(in_quiet_hours(at(13), "12:00-14:00"))
        self.assertFalse(in_quiet_hours(at(3), ""))

    def test_quiet_hours_block_reminders(self):
        esc = Escalation(quiet_hours="22:00-07:00")
        self.assertFalse(is_due(Issue("manual", since=at(3) - H), at(3), esc))

    def test_fmt_age(self):
        self.assertEqual(fmt_age(5 * H), "5 Stunden")
        self.assertEqual(fmt_age(72 * H), "3 Tagen")


if __name__ == "__main__":
    unittest.main()
