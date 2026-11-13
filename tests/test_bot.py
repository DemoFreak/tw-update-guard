import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import testlang  # noqa: F401
from twguard import bot as bot_module, busy, check, ota, panel, restarts, state, zypper
from twguard.bot import Bot, parse_duration
from twguard.config import Config
from twguard.i18n import current, set_language
from twguard.telegram import TelegramError

from fakes import FakeTelegram

# Vor dem Ersetzen in setUp gemerkt, für Tests des echten Starts
REAL_START_CHECK = Bot.start_check
ME = {"id": 1000, "username": "server1_bot", "is_bot": True}
OTHER_BOT = {"id": 2000, "username": "server2_bot", "is_bot": True}
USER = {"id": 42, "is_bot": False, "first_name": "Admin"}
GROUP = {"id": -100, "type": "supergroup"}


def message(text, chat=GROUP, user=USER, reply_to=None, age=0):
    msg = {"message_id": 7, "date": int(time.time() - age), "chat": chat, "from": user, "text": text}
    if reply_to:
        msg["reply_to_message"] = {"message_id": 3, "from": reply_to}
    return msg


def actions(buttons):
    return [b[1] for row in buttons or [] for b in row]


class BotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Config(language="de", token="x", chat_id=-100, allowed_user_ids=[42], hostname="server1",
                     state_dir=Path(self.tmp.name))
        # Sonst hängt das Ergebnis von der Uhrzeit des Testlaufs ab (Vorgabe 22–8 Uhr)
        cfg.escalation.quiet_hours = ""
        self.tg = FakeTelegram()
        self.bot = Bot(cfg, tg=self.tg, me=ME)
        self.start_check = mock.patch.object(Bot, "start_check").start()
        self.checking = mock.patch.object(busy, "check_running", return_value=False).start()
        # Kein echtes logind in den Tests; einzelne Tests planen einen Neustart über self.logind
        self.logind = mock.patch.object(busy, "shutdown", return_value=busy.Shutdown("none")).start()
        # Namen der Dienste aus `zypper ps`; Tests mit übrig gebliebenen Prozessen setzen self.lingering
        self.found, self.lingering = mock.Mock(return_value=[]), {}
        mock.patch.object(restarts, "survey", side_effect=lambda: None if (names := self.found()) is None else
                          restarts.Survey(list(names), dict(self.lingering))).start()
        # Neu gestartete Dienste gelten in den Tests als schon lange aktiv; einzelne Tests ersetzen den Wert
        self.young = mock.patch.object(restarts, "young", return_value=[]).start()
        # Kein echtes zypper in den Tests (fehlt z. B. in der CI). None heißt „unbekannt“ und lässt einen
        # von Hand geöffneten Neustart-Punkt stehen; einzelne Tests ersetzen den Wert
        self.rebooting = mock.patch.object(zypper, "needs_rebooting", return_value=None).start()
        self.holder = mock.patch.object(zypper, "lock_holder", return_value="").start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.tmp.cleanup)

    # --- Hilfen -----------------------------------------------------------

    def st(self) -> state.State:
        return state.load(self.bot.cfg.state_dir / "state.json")

    def panel_id(self) -> int:
        return self.st().messages[panel.TOPIC]["message_id"]

    def shown(self) -> tuple[str, list]:
        """Was die Zustandsnachricht jetzt zeigt (Text, Schaltflächen)."""
        return self.tg.current[self.panel_id()]

    def open_manual(self, approvable=True):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.open_issue("manual", "• kritisches Paket", fingerprint="abc", approvable=approvable)
            st.manual_shown = "abc"

    def open_reboot(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.open_issue("reboot", "Neustart nötig")

    def offer(self, *names):
        with state.locked(self.bot.cfg.state_dir) as st:
            restarts.note(st, restarts.Survey(list(names), {}), self.bot.cfg.services.exclude)
        self.found.return_value = list(names)

    def press(self, data, user=USER, chat=GROUP, message_id=None):
        """Schaltfläche drücken, ohne Angabe auf der Zustandsnachricht (die dafür erst entsteht)."""
        if message_id is None:
            self.bot.pump()
            message_id = self.panel_id()
        self.bot.handle_callback({"id": "cb1", "from": user, "data": data,
                                  "message": {"message_id": message_id, "chat": chat, "date": int(time.time()),
                                              "text": "Meldung", "entities": [], "reply_markup": {}}})

    def set_plan(self, count=1):
        rows = [["to-upgrade", f"paket-{i:03}", "1.0-1", "1.0-2", "repo"] for i in range(count)]
        with state.locked(self.bot.cfg.state_dir) as st:
            st.last_plan = {"summary": f"{count} Pakete", "rows": rows, "problems": []}

    def at(self, hour, minute=0):
        t = time.localtime()
        return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, hour, minute, 0, 0, 0, -1))

    # --- Befehle: wer antwortet -------------------------------------------

    def test_status_broadcast_answered(self):
        self.bot.handle(message("/status"))
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("server1", self.tg.sent[0])
        self.assertEqual(self.panel_id(), self.tg.next_id)

    def test_approve_needs_address(self):
        self.open_manual()
        self.bot.handle(message("/approve"))
        self.assertEqual(self.tg.sent, [])
        self.start_check.assert_not_called()

    def test_approve_by_reply(self):
        self.open_manual()
        self.bot.handle(message("/approve", reply_to=ME))
        self.start_check.assert_called_once_with(requested=True)
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.approved_fingerprint, "abc")

    def test_approve_by_mention(self):
        self.open_manual()
        self.bot.handle(message("/approve@server1_bot"))
        self.start_check.assert_called_once()

    def test_command_for_other_bot_ignored(self):
        self.open_manual()
        self.bot.handle(message("/approve@server2_bot"))
        self.bot.handle(message("/status", reply_to=OTHER_BOT))
        self.assertEqual(self.tg.sent, [])
        self.start_check.assert_not_called()

    def test_conflict_not_approvable(self):
        self.open_manual(approvable=False)
        self.bot.handle(message("/approve", reply_to=ME))
        self.start_check.assert_not_called()
        self.assertIn("SSH", self.tg.sent[0])

    def test_unauthorized_user_ignored(self):
        self.open_manual()
        self.bot.handle(message("/approve@server1_bot", user={"id": 99, "is_bot": False}))
        self.assertEqual(self.tg.sent, [])
        self.start_check.assert_not_called()

    def test_foreign_chat_ignored(self):
        self.bot.handle(message("/status", chat={"id": -555, "type": "group"}))
        self.assertEqual(self.tg.sent, [])

    def test_private_chat_counts_as_addressed(self):
        self.open_manual()
        self.bot.handle(message("/approve", chat={"id": 42, "type": "private"}))
        self.start_check.assert_called_once()

    def test_old_command_ignored(self):
        self.open_manual()
        self.bot.handle(message("/approve@server1_bot", age=3600))
        self.start_check.assert_not_called()

    def test_later_is_limited(self):
        self.open_manual()
        for _ in range(3):
            self.bot.handle(message("/later 5d", reply_to=ME))
        answers = [t for t in self.tg.sent if t.startswith(("Aufgeschoben", "Nicht weiter"))]
        self.assertIn("72 Stunden", answers[0])  # auf max_snooze_hours begrenzt
        self.assertIn("Nicht weiter aufschiebbar", answers[2])

    def test_command_deleted_after_answer(self):
        self.bot.handle(message("/status"))
        self.assertEqual(self.tg.deleted, [7])

    def test_missing_delete_right_is_tolerated(self):
        self.tg.fail_delete = True
        with self.assertLogs("twguard.bot", "WARNING"):
            self.bot.handle(message("/status"))
            self.bot.handle(message("/status"))
        self.assertTrue(self.bot.delete_warned)

    def test_help_expires(self):
        self.bot.handle(message("/help@server1_bot"))
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.ephemeral[0]["message_id"], 101)

    def test_help_broadcast_silent_without_help_full(self):
        self.bot.handle(message("/help"))
        self.assertEqual(self.tg.sent, [])

    def test_help_addressed_or_help_full_has_list(self):
        self.bot.handle(message("/help@server1_bot"))
        self.assertIn("/approve", self.tg.sent[0])
        self.bot.cfg.help_full = True
        self.bot.handle(message("/help"))
        self.assertIn("/approve", self.tg.sent[1])

    def test_parse_duration(self):
        self.assertEqual(parse_duration("12h"), 12)
        self.assertEqual(parse_duration("2d"), 48)
        self.assertEqual(parse_duration("1"), 24)
        self.assertIsNone(parse_duration("morgen"))

    # --- Die Zustandsnachricht ------------------------------------------

    def test_one_state_message_with_all_buttons(self):
        # Beobachtung auf einem Server: Neustart und Dienste offen; jede Übersicht muss beide Knöpfe zeigen
        self.open_reboot()
        self.offer("cron", "sshd")
        self.open_manual()
        self.bot.pump()
        text, buttons = self.shown()
        for title in ("Update braucht Freigabe", "Neustart ausstehend", "Dienste nutzen alte Dateien"):
            self.assertIn(title, text)
        data = actions(buttons)
        for action in ("approve:abc", "reboot", "restart:cron", "restart:sshd", "check", "later:24"):
            self.assertIn(action, data)
        self.assertEqual(data.count("check"), 1)
        self.assertIn("Stand ", text)

    def test_state_message_is_the_only_one_with_buttons(self):
        self.open_reboot()
        self.offer("postfix")
        self.bot.pump()
        with mock.patch.object(restarts, "start", return_value={}):
            self.press("restart:postfix")
        self.found.return_value = []
        with mock.patch.object(restarts, "pending", return_value=[]), \
                mock.patch.object(restarts, "states", return_value={"postfix": "active"}):
            self.bot.watch_restart()
        # Das Ereignis ohne Knöpfe, darunter wieder die Zustandsnachricht mit dem Neustart-Knopf
        with_buttons = [mid for mid, (_, b) in self.tg.current.items() if b]
        self.assertEqual(with_buttons, [self.panel_id()])
        self.assertIn("reboot", actions(self.shown()[1]))
        event = self.tg.sent[-2]
        self.assertIn("neu gestartet: postfix", event)
        self.assertIsNone(self.tg.buttons[-2])

    def test_unchanged_state_is_not_edited_again(self):
        self.bot.pump()
        self.bot.pump()
        self.assertEqual((len(self.tg.sent), self.tg.edited), (1, []))
        # Eine Änderung ohne fällige Erinnerung bearbeitet die Nachricht an ihrem Platz
        with state.locked(self.bot.cfg.state_dir) as st:
            st.pending_text = "3 Pakete"
        self.bot.pump()
        self.assertEqual(len(self.tg.sent), 1)
        self.assertEqual(self.tg.edited[0][0], self.panel_id())
        self.assertIn("Ausstehend: 3 Pakete", self.tg.edited[0][1])

    def test_edit_failure_sends_new_state_message(self):
        self.bot.pump()
        first = self.panel_id()
        self.open_reboot()
        self.tg.fail_edit = True
        self.bot.pump()
        self.assertNotEqual(self.panel_id(), first)
        self.assertIn(first, self.tg.deleted)

    def test_status_brings_state_message_down(self):
        self.bot.pump()
        first = self.panel_id()
        self.bot.handle(message("/status"))
        self.assertEqual(len(self.tg.sent), 2)
        self.assertIn(first, self.tg.deleted)
        self.assertTrue(self.tg.silent[-1])  # wer fragt, schaut hin

    def test_replaced_message_too_old_is_shortened(self):
        self.bot.pump()
        with state.locked(self.bot.cfg.state_dir) as st:
            st.messages[panel.TOPIC]["at"] = time.time() - 50 * 3600
            first = st.messages[panel.TOPIC]["message_id"]
        self.bot.pump(resend=True)
        self.assertNotIn(first, self.tg.deleted)
        self.assertEqual(self.tg.current[first], (self.tg.edited[-1][1], None))
        self.assertIn("veraltet", self.tg.current[first][0])

    def test_unremovable_message_is_retried(self):
        self.bot.pump()
        first = self.panel_id()
        self.tg.fail_delete = self.tg.fail_edit = True
        self.bot.pump(resend=True)
        self.assertEqual([m["message_id"] for m in self.st().stale_messages], [first])
        self.tg.fail_delete = self.tg.fail_edit = False
        self.bot.retry_stale()
        self.assertIn(first, self.tg.deleted)
        self.assertEqual(self.st().stale_messages, [])

    def test_too_many_requests_while_cleaning_up_spends_no_tries(self):
        # F24-Rest: 429 ist kein Löschfehler; unversuchte Nachrichten behalten ihren Zähler
        now = time.time()
        with state.locked(self.bot.cfg.state_dir) as st:
            st.stale_messages = [{"chat_id": -100, "message_id": 10, "at": now},
                                 {"chat_id": -100, "message_id": 11, "at": now}]
        for _ in range(4):
            self.tg.blocked_until = 0
            self.tg.limit["deleteMessage"] = 30
            self.bot.retry_stale()
        self.assertEqual([(m["message_id"], m.get("tries", 0)) for m in self.st().stale_messages], [(10, 0), (11, 0)])
        self.assertEqual(self.tg.calls, [])  # nichts hat Telegram erreicht
        self.tg.blocked_until = 0
        self.bot.retry_stale()
        self.assertEqual(self.tg.deleted, [10, 11])
        self.assertEqual(self.st().stale_messages, [])

    def test_unremovable_message_is_given_up_after_three_tries(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.stale_messages = [{"chat_id": -100, "message_id": 10, "at": time.time()}]
        self.tg.fail_delete = self.tg.fail_edit = True
        for tries in (1, 2):
            self.bot.retry_stale()
            self.assertEqual(self.st().stale_messages[0]["tries"], tries)
        self.bot.retry_stale()
        self.assertEqual(self.st().stale_messages, [])

    def test_send_failure_keeps_previous_state_message(self):
        self.bot.pump()
        first = self.panel_id()
        self.tg.fail_send = True
        self.assertFalse(self.bot.pump(resend=True))
        self.assertEqual(self.panel_id(), first)
        self.assertEqual(self.tg.deleted, [])

    def test_too_many_requests_pauses_sending(self):
        send = self.tg.send
        self.tg.send = mock.Mock(side_effect=TelegramError("sendMessage: Too Many Requests", retry_after=30))
        self.assertFalse(self.bot.pump())
        self.assertGreater(self.tg.blocked_until, time.time() + 25)
        self.tg.send = send
        self.assertFalse(self.bot.pump())  # noch in der Pause
        self.assertEqual(self.tg.sent, [])
        self.tg.blocked_until = 0
        self.assertTrue(self.bot.pump())

    def big_events(self, n=4, size=1400):
        with state.locked(self.bot.cfg.state_dir) as st:
            for i in range(n):
                panel.event(st, f"event-{i} " + "x" * size, loud=True)

    def test_large_outbox_sent_completely_in_several_messages(self):
        # F22: nichts geht durch Kürzen verloren
        self.big_events()
        self.assertTrue(self.bot.send_events(time.time()))
        joined = "\n".join(self.tg.sent)
        for i in range(4):
            self.assertIn(f"event-{i} ", joined)
        self.assertTrue(all(len(text) <= panel.TEXT_LIMIT for text in self.tg.sent))
        self.assertGreater(len(self.tg.sent), 1)
        self.assertEqual(self.st().outbox, [])

    def test_send_failure_keeps_undelivered_events(self):
        self.big_events()
        send, calls = self.tg.send, []

        def second_fails(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise TelegramError("sendMessage: Bad Gateway")
            return send(*args, **kwargs)

        self.tg.send = second_fails
        self.bot.send_events(time.time())
        delivered = "\n".join(self.tg.sent)
        pending = [e["text"] for e in self.st().outbox]
        for i in range(4):
            # Jedes Ereignis ist entweder angekommen oder steht noch aus, nie beides oder keins
            self.assertNotEqual(f"event-{i} " in delivered, any(t.startswith(f"event-{i} ") for t in pending))
        self.assertTrue(pending)

    def test_oversized_event_is_shortened_as_plain_text(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            panel.event(st, "<b>Fehler</b>\n<pre>" + "a &lt; b\n" * 1000 + "</pre>", loud=True)
        text = self.st().outbox[0]["text"]
        self.assertLessEqual(len(text), panel.TEXT_LIMIT)
        self.assertNotIn("<pre>", text)
        self.assertIn("a &lt; b", text)

    def test_press_during_outage_counts_on_the_message_it_was_made_on(self):
        # VM-Test: Während der Störung „Jetzt prüfen“ gedrückt; nach der Störung sendete der Bot zuerst eine
        # neue Zustandsnachricht und wies den Druck danach als veraltet ab
        self.bot.pump()
        pressed_on = self.panel_id()
        self.open_reboot()  # neuer Punkt während der Störung: verlangt eine neue Nachricht mit Ton
        with mock.patch.object(self.bot.tg, "get_updates", create=True, side_effect=TelegramError("timed out")), \
                mock.patch("time.sleep"):
            self.assertEqual(self.bot.fetch(), [])
        sent = len(self.tg.sent)
        self.assertFalse(self.bot.pump())
        self.assertEqual(len(self.tg.sent), sent)  # nichts gesendet, solange nichts gelesen ist
        update = {"update_id": 5, "callback_query": {
            "id": "cb1", "from": USER, "data": "check",
            "message": {"message_id": pressed_on, "chat": GROUP, "date": int(time.time()), "text": "Meldung",
                        "entities": [], "reply_markup": {}}}}
        with mock.patch.object(self.bot.tg, "get_updates", create=True, return_value=[update]):
            self.bot.handle_batch(self.bot.fetch())
        self.start_check.assert_called_once()
        self.assertNotIn("veraltet", " ".join(self.tg.answered))

    def test_several_presses_and_events_after_outage(self):
        # Mehrere Drücke aus der Störung in einem Abruf, dazu ein wartendes Ereignis: alle Drücke gelten
        self.bot.pump()
        pressed_on = self.panel_id()
        self.open_reboot()
        with state.locked(self.bot.cfg.state_dir) as st:
            panel.event(st, "✅ <b>server1</b>: Ereignis aus der Störung")
        with mock.patch.object(self.bot.tg, "get_updates", create=True, side_effect=TelegramError("timed out")), \
                mock.patch("time.sleep"):
            self.bot.fetch()
        sent = len(self.tg.sent)
        self.assertFalse(self.bot.pump())
        self.assertEqual(len(self.tg.sent), sent)

        def press(update_id, data):
            return {"update_id": update_id, "callback_query": {
                "id": f"cb{update_id}", "from": USER, "data": data,
                "message": {"message_id": pressed_on, "chat": GROUP, "date": int(time.time()), "text": "Meldung",
                            "entities": [], "reply_markup": {}}}}
        with mock.patch.object(self.bot.tg, "get_updates", create=True,
                               return_value=[press(5, "check"), press(6, "later:24")]):
            self.bot.handle_batch(self.bot.fetch())
        self.start_check.assert_called_once()
        self.assertNotIn("veraltet", " ".join(self.tg.answered))
        self.assertGreater(self.st().issues["reboot"].snoozed_until, time.time())
        self.bot.pump()
        self.assertIn("Ereignis aus der Störung", " ".join(self.tg.sent))
        self.assertEqual(self.outbox(), [])

    def test_press_on_message_replaced_before_fetch_stays_outdated(self):
        # Nur die beim Abruf registrierte Nachricht gilt, nicht irgendeine frühere
        self.bot.pump()
        old = self.panel_id()
        self.bot.pump(resend=True)
        self.assertNotEqual(self.panel_id(), old)
        update = {"update_id": 7, "callback_query": {
            "id": "cb7", "from": USER, "data": "check",
            "message": {"message_id": old, "chat": GROUP, "date": int(time.time()), "text": "Meldung",
                        "entities": [], "reply_markup": {}}}}
        self.bot.handle_batch([update])
        self.start_check.assert_not_called()
        self.assertIn("veraltet", self.tg.answered[-1])

    def test_only_fetching_broken_sends_again_after_a_while(self):
        # z. B. 409: ein zweiter Bot mit demselben Token holt die Updates ab, Senden geht weiter
        self.bot.pump()
        self.open_reboot()
        self.bot.fetch_failed_since = time.time() - bot_module.UNREAD_MAX - 1
        sent = len(self.tg.sent)
        self.assertTrue(self.bot.pump())
        self.assertGreater(len(self.tg.sent), sent)

    def test_too_many_requests_on_event_stops_the_cycle(self):
        # F24: nach 429 beim Ereignis kein weiterer Aufruf im selben Durchgang, das Ereignis bleibt
        with state.locked(self.bot.cfg.state_dir) as st:
            panel.event(st, "Ereignis")
        self.tg.limit["sendMessage"] = 30
        self.assertFalse(self.bot.pump())
        self.assertEqual(self.tg.calls, [])
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(len(st.outbox), 1)

    def test_too_many_requests_on_edit_does_not_send_instead(self):
        self.bot.pump()
        with state.locked(self.bot.cfg.state_dir) as st:
            st.zypp_holder = "zypper (PID 4711)"  # ändert die Nachricht, ohne Erinnerung: wird bearbeitet
        calls = len(self.tg.calls)
        self.tg.limit["editMessageText"] = 30
        self.assertFalse(self.bot.pump())
        self.assertEqual(len(self.tg.calls), calls)

    def test_nothing_open(self):
        self.bot.pump()
        text, buttons = self.shown()
        self.assertTrue(text.startswith("✅ <b>server1</b>: nichts offen"))
        self.assertEqual(buttons, [[("🔄 Jetzt prüfen", "check")]])
        self.assertIn("Beobachtungsmodus", text)

    def test_auto_mode_is_shown(self):
        self.bot.cfg.mode = "auto"
        self.bot.pump()
        text = self.shown()[0]
        self.assertIn("Automatikmodus", text)
        self.assertNotIn("Beobachtungsmodus", text)

    def test_details_button_only_with_packages(self):
        self.bot.pump()
        self.assertNotIn("details", actions(self.shown()[1]))
        self.set_plan()
        self.bot.pump()
        self.assertIn("details", actions(self.shown()[1]))

    def test_status_asks_zypper_for_pending_reboot(self):
        # Nicht erst beim nächsten Takt (alle 30 min): Der Status zeigt den Neustart gleich mit Knopf
        self.rebooting.return_value = True
        self.bot.handle(message("/status"))
        self.assertIn("reboot", actions(self.shown()[1]))

    def test_status_closes_reboot_no_longer_needed(self):
        self.open_reboot()
        self.rebooting.return_value = False
        self.bot.handle(message("/status"))
        self.assertNotIn("reboot", actions(self.shown()[1]))
        self.assertNotIn("reboot", self.st().issues)

    def test_start_check_asks_zypper_and_marks_running(self):
        self.rebooting.return_value = True
        with mock.patch.object(subprocess, "run") as run:
            REAL_START_CHECK(self.bot, requested=True)
        run.assert_called_once()
        st = self.st()
        self.assertIn("reboot", st.issues)
        self.assertEqual(st.activity["check"]["phase"], "queued")
        self.assertTrue(st.check_request)
        self.bot.pump()
        self.assertIn("Prüfung startet", self.shown()[0])

    def test_start_check_keeps_running_check(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.activity["check"] = {**state.holder(), "phase": "apply", "detail": "3 Pakete"}
        with mock.patch.object(subprocess, "run"):
            REAL_START_CHECK(self.bot)
        self.assertEqual(self.st().activity["check"]["phase"], "apply")

    def test_check_that_never_started_is_not_shown_forever(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.activity["check"] = {"phase": "queued", "detail": "", "at": time.time() - bot_module.QUEUED_MAX - 1}
            st.activity["other"] = {}
        self.bot.pump()
        self.assertNotIn("check", self.st().activity)
        with state.locked(self.bot.cfg.state_dir) as st:
            # Ein abgestürzter Prüflauf (Prozess weg) ebenso
            st.activity["check"] = {"pid": 2**22 + 1, "boot": state.boot_id(), "at": time.time(), "phase": "apply"}
        self.bot.pump()
        self.assertNotIn("check", self.st().activity)

    def test_running_phase_and_lock_holder_shown(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.activity["check"] = {**state.holder(), "phase": "wait-zypp",
                                    "detail": "purge-kernels.service (zypper, PID 707)"}
            st.zypp_holder = "purge-kernels.service (zypper, PID 707)"
        self.bot.pump()
        text = self.shown()[0]
        self.assertIn("Prüfung wartet auf die Paketverwaltung: purge-kernels.service", text)
        self.assertNotIn("🔒", text)  # der Besitzer steht schon in der Prüfung

    def test_status_names_lock_holder(self):
        self.holder.return_value = "purge-kernels.service (zypper, PID 707)"
        self.bot.handle(message("/status"))
        self.assertIn("🔒 Paketverwaltung belegt: purge-kernels.service (zypper, PID 707)", self.shown()[0])

    def test_no_check_button_while_check_runs(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.activity["check"] = {**state.holder(), "phase": "refresh"}
        self.bot.pump()
        self.assertNotIn("check", [b[1] for row in self.shown()[1] for b in row])
        # Ein Druck auf einen noch sichtbaren Knopf stößt nichts an; der Bot fragt selbst nach
        # Ein Knopf von kurz vorher, /check und „Alle prüfen“ bewirken nichts und melden nichts
        self.checking.return_value = True
        sent = len(self.tg.sent)
        with mock.patch.object(self.bot, "start_check") as start:
            self.press("check")
            self.bot.handle(message("/check"))
            self.bot.handle(message("🔄 Alle prüfen"))
        start.assert_not_called()
        self.assertEqual(self.tg.answered[-1], "")
        self.assertEqual(len(self.tg.sent), sent)
        with state.locked(self.bot.cfg.state_dir) as st:
            st.activity = {}
        self.checking.return_value = False
        self.bot.pump()
        self.assertIn("check", [b[1] for row in self.shown()[1] for b in row])

    def test_own_check_is_not_shown_as_lock_holder(self):
        # Der eigene Prüflauf hält die Sperre beim Auffrischen; das ist seine Phase, keine fremde Belegung
        # Ihre Sperre gilt trotzdem: Den Neustart weist der Bot dann ab, also bietet er ihn auch nicht an (F26)
        self.holder.return_value = "twguard-check.service (Zypp-main, PID 75494)"
        with state.locked(self.bot.cfg.state_dir) as st:
            st.activity["check"] = {**state.holder(), "phase": "refresh"}
            st.open_issue("reboot", "zypper needs-rebooting meldet einen ausstehenden Neustart.")
        self.bot.observe_system()
        self.bot.pump()
        text, buttons = self.shown()
        self.assertIn("Prüfung läuft: Repos auffrischen", text)
        self.assertNotIn("🔒", text)
        self.assertNotIn("reboot", [b[1] for row in buttons for b in row])
        self.assertIn("Paketverwaltung belegt", self.bot.refused("reboot"))
        # Nach dem Ende des Prüflaufs: kein Hinweis, und der Knopf ist sofort wieder da, auch wenn die Sperre
        # erst bei der nächsten Abfrage als frei beobachtet wird
        with state.locked(self.bot.cfg.state_dir) as st:
            st.activity = {}
        self.bot.pump()
        text, buttons = self.shown()
        self.assertNotIn("🔒", text)
        self.assertIn("reboot", [b[1] for row in buttons for b in row])

    def test_status_shows_pending_instead_of_nothing_open(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.pending_text = "24 Pakete, Download 12 MB"
            st.observed_fingerprint = "rtn"
        self.bot.pump()
        text, buttons = self.shown()
        self.assertIn("Ausstehend: 24 Pakete", text)
        self.assertNotIn("nichts offen", text)
        self.assertEqual(buttons[0][0], ("▶️ Diesmal einspielen", "approve:rtn"))

    # --- Ereignisse -------------------------------------------------------

    def event(self, text, loud=False, at=None):
        with state.locked(self.bot.cfg.state_dir) as st:
            panel.event(st, text, loud=loud, now=at)

    def test_events_come_together_and_state_message_follows(self):
        self.bot.pump()
        first = self.panel_id()
        self.event("🔄 a")
        self.event("✅ b")
        self.bot.pump()
        self.assertEqual(self.tg.sent[1], "🔄 a\n✅ b")
        self.assertIsNone(self.tg.buttons[1])
        self.assertTrue(self.tg.silent[1])
        # Die Zustandsnachricht steht danach wieder unten, still
        self.assertNotEqual(self.panel_id(), first)
        self.assertTrue(self.tg.silent[2])
        self.assertEqual(self.st().outbox, [])

    def test_events_wait_for_running_action(self):
        self.event("✅ eingespielt")
        with state.locked(self.bot.cfg.state_dir) as st:
            st.restart_request = {"at": time.time(), "services": ["postfix"]}
        self.bot.pump()
        self.assertFalse([t for t in self.tg.sent if "eingespielt" in t])
        # Höchstens EVENT_HOLD
        with state.locked(self.bot.cfg.state_dir) as st:
            st.outbox[0]["at"] -= bot_module.EVENT_HOLD + 1
        self.bot.pump()
        self.assertIn("✅ eingespielt", self.tg.sent)

    def test_loud_event_goes_at_once(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.restart_request = {"at": time.time(), "services": ["postfix"]}
        self.event("🔴 kaputt", loud=True)
        self.bot.pump()
        self.assertEqual(self.tg.sent[0], "🔴 kaputt")
        self.assertFalse(self.tg.silent[0])

    def test_event_survives_telegram_outage(self):
        self.event("✅ x")
        self.tg.fail_send = True
        self.bot.pump()
        self.assertEqual(len(self.st().outbox), 1)
        self.tg.fail_send = False
        self.bot.pump()
        self.assertIn("✅ x", self.tg.sent)
        self.assertEqual(self.st().outbox, [])

    def test_event_from_old_version_retires_its_waiting_message(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.outbox = [{"chat_id": -100, "message_id": 55, "at": time.time(), "text": "✅ alt", "loud": False}]
        self.bot.pump()
        self.assertIn("✅ alt", self.tg.sent)
        self.assertIn(55, self.tg.deleted)

    # --- Erinnerungen und Briefing --------------------------------------

    def test_due_reminder_resends_with_sound_and_counts_after_delivery(self):
        self.bot.pump()
        first = self.panel_id()
        with state.locked(self.bot.cfg.state_dir) as st:
            st.open_issue("reboot", "Kernel", now=time.time() - 100 * 3600)
        self.tg.fail_send = True
        self.bot.pump()
        self.assertEqual(self.st().issues["reboot"].notify_count, 0)  # nicht angekommen, nicht erinnert
        self.tg.fail_send = False
        self.bot.pump()
        self.assertFalse(self.tg.silent[-1])
        self.assertIn("🔔 Erinnerung: Neustart ausstehend", self.tg.sent[-1])
        self.assertIn(first, self.tg.deleted)
        issue = self.st().issues["reboot"]
        self.assertEqual(issue.notify_count, 1)
        # Bis zur nächsten Stufe nicht wieder; danach nur bearbeiten
        self.bot.pump()
        self.assertEqual(len(self.tg.sent), 2)

    def test_bringing_down_does_not_count_as_reminder(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.open_issue("reboot", "Kernel")
            st.issues["reboot"].last_notified = time.time()
            st.issues["reboot"].notify_count = 1
        self.bot.handle(message("/status"))
        self.assertEqual(self.st().issues["reboot"].notify_count, 1)
        self.assertNotIn("🔔", self.tg.sent[-1])

    def test_reminder_counts_as_shown(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.open_issue("manual", "x", fingerprint="abc", approvable=True)
        self.bot.pump()
        self.assertEqual(self.st().manual_shown, "abc")

    def swap_plan_during(self, method):
        """F21: Während `method` läuft, ersetzt ein Prüflauf Plan A (abc) durch B."""
        original = getattr(self.tg, method)

        def swapping(*args, **kwargs):
            with state.locked(self.bot.cfg.state_dir) as st:
                st.open_issue("manual", "NEVER SHOWN PLAN B", fingerprint="unseen-B", approvable=True)
            setattr(self.tg, method, original)
            return original(*args, **kwargs)

        setattr(self.tg, method, swapping)

    def test_plan_changed_while_sending_reminder_is_not_shown(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.open_issue("manual", "Plan A", fingerprint="abc", approvable=True, now=time.time() - 100 * 3600)
        self.swap_plan_during("send")
        self.bot.pump()
        self.assertIn("Plan A", self.tg.sent[0])
        st = self.st()
        self.assertEqual(st.manual_shown, "abc")  # gezeigt war A, nicht B
        self.assertEqual(st.issues["manual"].notify_count, 0)  # B gilt nicht als erinnert
        # /approve ohne Fingerabdruck gibt B nicht frei, solange B nicht zu sehen war
        self.assertIn("geändert", self.bot.approve())
        self.assertEqual(self.st().approved_fingerprint, "")
        # Der nächste Durchgang zeigt B und erinnert daran
        self.bot.pump()
        self.assertIn("NEVER SHOWN PLAN B", self.tg.sent[-1])
        self.assertFalse(self.tg.silent[-1])
        self.assertEqual(self.st().manual_shown, "unseen-B")
        self.assertEqual(self.st().issues["manual"].notify_count, 1)

    def test_plan_changed_while_editing_is_not_shown(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.open_issue("manual", "Plan A", fingerprint="abc", approvable=True)
            st.issues["manual"].last_notified = time.time()
        self.bot.pump()
        with state.locked(self.bot.cfg.state_dir) as st:
            st.zypp_holder = "zypper (PID 4711)"  # nur bearbeiten, keine Erinnerung
        self.swap_plan_during("edit")
        self.bot.pump()
        self.assertIn("Plan A", self.tg.edited[-1][1])
        self.assertEqual(self.st().manual_shown, "abc")
        self.assertIn("geändert", self.bot.approve())

    def test_recreated_issue_of_same_kind_is_not_reminded(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.open_issue("reboot", "alt", now=time.time() - 100 * 3600)
        original = self.tg.send

        def recreate(*args, **kwargs):
            with state.locked(self.bot.cfg.state_dir) as st:
                st.resolve("reboot")
                st.open_issue("reboot", "neu")
            self.tg.send = original
            return original(*args, **kwargs)

        self.tg.send = recreate
        self.bot.pump()
        self.assertEqual(self.st().issues["reboot"].notify_count, 0)

    def test_no_reminder_in_quiet_hours(self):
        self.bot.cfg.escalation.quiet_hours = "00:00-23:59"
        self.open_reboot()
        self.bot.pump()
        self.assertTrue(self.tg.silent[0])
        self.assertNotIn("🔔", self.tg.sent[0])
        self.assertEqual(self.st().issues["reboot"].notify_count, 0)

    def test_briefing_once_per_day(self):
        self.bot.maybe_briefing(self.at(7, 59))
        self.assertEqual(self.tg.sent, [])
        self.bot.maybe_briefing(self.at(8, 0))
        self.bot.maybe_briefing(self.at(9, 0))
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("alles in Ordnung", self.tg.sent[0])
        self.assertTrue(self.tg.silent[0])

    def test_briefing_with_issue_not_silent(self):
        self.open_manual()
        with state.locked(self.bot.cfg.state_dir) as st:
            st.issues["manual"].last_notified = self.at(9, 0)
        self.bot.maybe_briefing(self.at(10, 0))
        self.assertIn("☀️ <b>server1</b>: 1 offener Punkt", self.tg.sent[-1])
        self.assertFalse(self.tg.silent[-1])

    def test_briefing_not_caught_up_too_late(self):
        self.bot.maybe_briefing(self.at(15, 0))
        self.assertEqual(self.tg.sent, [])

    def test_briefing_off(self):
        self.bot.cfg.briefing.at = ""
        self.bot.maybe_briefing(self.at(8, 0))
        self.assertEqual(self.tg.sent, [])

    def test_briefing_takes_due_reminder(self):
        now = self.at(8, 0)
        self.open_manual()
        with state.locked(self.bot.cfg.state_dir) as st:
            issue = st.issues["manual"]
            issue.since, issue.last_notified, issue.notify_count = now - 30 * 3600, now - 25 * 3600, 1
        self.bot.maybe_briefing(now)
        self.bot.pump(now=now)
        self.assertEqual(len(self.tg.sent), 1)  # keine Erinnerung als zweite Nachricht
        self.assertIn("🔔", self.tg.sent[0])
        self.assertIn("approve:abc", actions(self.tg.buttons[0]))
        issue = self.st().issues["manual"]
        self.assertEqual((issue.last_notified, issue.notify_count), (now, 2))

    def test_briefing_replaces_previous_state_message(self):
        self.bot.pump()
        first = self.panel_id()
        self.bot.maybe_briefing(self.at(8, 0))
        self.assertIn(first, self.tg.deleted)
        self.assertEqual(self.st().last_briefing_day, time.strftime("%Y-%m-%d", time.localtime(self.at(8, 0))))

    def test_briefing_not_counted_when_not_delivered(self):
        self.tg.fail_send = True
        self.bot.maybe_briefing(self.at(8, 0))
        self.assertEqual(self.st().last_briefing_day, "")

    def test_briefing_names_kernel_and_last_check(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.last_check_ok = self.at(3, 0)
        self.bot.maybe_briefing(self.at(8, 0))
        self.assertIn("Letzte Prüfung", self.tg.sent[0])
        self.assertIn(f"Kernel {os.uname().release} seit", self.tg.sent[0])

    def test_reboot_planned_on_console_does_not_remind(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.open_issue("reboot", "Neustart nötig", now=time.time() - 3 * 86400)
            busy.note_shutdown(st, busy.Shutdown("planned", "reboot", time.time() + 50))
        self.bot.pump()
        self.assertNotIn("🔔", self.tg.sent[0])
        # sudo shutdown -c: logind hat nichts mehr geplant; der Takt gleicht ab und erinnert wieder
        with mock.patch.object(self.bot, "_rpmdb_mtime", return_value=0.0), \
                mock.patch.object(self.bot, "check_reboot"), mock.patch.object(self.bot, "maybe_briefing"):
            self.bot.tick()
        self.bot.pump()
        self.assertIn("🔔 Erinnerung: Neustart ausstehend", self.tg.sent[-1])

    # --- Schaltflächen ----------------------------------------------------

    def test_button_on_older_message_does_nothing_and_shows_current(self):
        # Codex: Aktionen nur aus der aktuell registrierten Zustandsnachricht
        self.open_manual()
        self.bot.pump()
        current_id = self.panel_id()
        self.press("approve:abc", message_id=33)
        self.start_check.assert_not_called()
        self.assertIn("veraltet", self.tg.answered[0])
        self.assertIn(33, self.tg.deleted)
        self.assertNotEqual(self.panel_id(), current_id)  # der aktuelle Stand steht jetzt unten
        self.assertEqual(self.st().approved_fingerprint, "")

    def test_same_message_id_in_other_chat_is_not_the_state_message(self):
        self.open_reboot()
        with mock.patch("subprocess.run") as run:
            self.press("reboot", chat={"id": 42, "type": "private"}, message_id=None)
        run.assert_not_called()
        self.assertIn("veraltet", self.tg.answered[0])

    def test_button_approve_with_matching_fingerprint(self):
        self.open_manual()
        self.press("approve:abc")
        self.press("confirm:abc", message_id=self.panel_id())
        self.start_check.assert_called_once_with(requested=True)
        self.assertEqual(self.tg.answered, ["Bitte bestätigen", "Freigegeben"])
        self.assertEqual(self.st().approved_fingerprint, "abc")
        self.assertEqual(self.st().panel["view"], "")

    # --- Freigabe in zwei Schritten (ein Tipp allein spielt nie etwas ein) ---

    def test_single_press_on_approve_only_asks(self):
        self.open_manual()
        self.press("approve:abc")
        self.start_check.assert_not_called()
        self.assertEqual(self.st().approved_fingerprint, "")
        text, buttons = self.shown()
        self.assertIn("Dieses Update jetzt einspielen?", text)
        # Ein zweiter Tipp links oben (wo „Freigeben“ stand) bricht ab; „Ja“ steht rechts daneben
        self.assertEqual(buttons, [[("✖️ Abbrechen", "back"), ("✅ Ja, einspielen", "confirm:abc")]])

    def test_double_tap_on_same_spot_cancels(self):
        self.open_manual()
        self.press("approve:abc")
        first = self.shown()[1][0][0][1]
        self.press(first, message_id=self.panel_id())
        self.start_check.assert_not_called()
        self.assertEqual(self.st().approved_fingerprint, "")
        self.assertIn("approve:abc", actions(self.shown()[1]))

    def test_confirm_expires(self):
        self.open_manual()
        self.press("approve:abc")
        with state.locked(self.bot.cfg.state_dir) as st:
            st.panel["view_at"] = time.time() - 61
        self.press("confirm:abc", message_id=self.panel_id())
        self.start_check.assert_not_called()
        self.assertIn("abgelaufen", self.tg.answered[-1])
        self.assertEqual(self.st().approved_fingerprint, "")

    def test_question_disappears_after_its_time(self):
        self.open_manual()
        self.press("approve:abc")
        with state.locked(self.bot.cfg.state_dir) as st:
            st.panel["view_at"] = time.time() - 61
        self.bot.pump()
        self.assertIn("approve:abc", actions(self.shown()[1]))
        self.assertNotIn("confirm:abc", actions(self.shown()[1]))

    def test_confirm_without_question_is_refused(self):
        # Ein „Ja“ ohne stehende Rückfrage (z. B. alter Callback) gibt nichts frei
        self.open_manual()
        self.press("confirm:abc")
        self.start_check.assert_not_called()
        self.assertEqual(self.st().approved_fingerprint, "")

    def test_plan_change_drops_question(self):
        self.open_manual()
        self.press("approve:abc")
        with state.locked(self.bot.cfg.state_dir) as st:
            st.open_issue("manual", "• anderes Paket", fingerprint="neu", approvable=True)
        self.bot.pump()
        self.assertNotIn("confirm:abc", actions(self.shown()[1]))
        self.press("confirm:abc", message_id=self.panel_id())
        self.start_check.assert_not_called()
        self.assertEqual(self.st().approved_fingerprint, "")

    def test_approval_is_logged_and_reported(self):
        self.open_manual()
        self.press("approve:abc")
        with self.assertLogs("twguard.bot", "INFO") as logs:
            self.press("confirm:abc", message_id=self.panel_id())
        self.assertTrue(any("Freigabe von" in line and "abc" in line for line in logs.output))
        self.assertTrue(any("freigegeben von" in text for text in self.tg.sent))

    def test_text_approve_needs_no_question_but_is_reported(self):
        self.open_manual()
        with self.assertLogs("twguard.bot", "INFO") as logs:
            self.bot.handle(message("/approve@server1_bot"))
        self.start_check.assert_called_once()
        self.assertTrue(any("/approve" in line for line in logs.output))

    def test_button_approve_stale_fingerprint(self):
        self.open_manual()
        self.press("approve:old")
        self.start_check.assert_not_called()
        self.assertIn("älteren Stand", self.tg.answered[0])

    def test_button_approve_observed_routine(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.observed_fingerprint = "rtn"
            st.pending_text = "3 Pakete, Download 1 MB"
        self.press("approve:rtn")
        self.assertIn("Diese Updates diesmal einspielen?", self.shown()[0])
        self.press("confirm:rtn", message_id=self.panel_id())
        self.start_check.assert_called_once()

    def test_button_approve_observed_routine_no_longer_pending(self):
        # Der Fingerabdruck bleibt nach dem Einspielen stehen; ein alter Knopf darf nichts mehr starten
        with state.locked(self.bot.cfg.state_dir) as st:
            st.observed_fingerprint = "rtn"
        self.press("approve:rtn")
        self.start_check.assert_not_called()
        self.assertIn("kein Update", self.tg.answered[0])

    def test_manual_shows_plan_summary_from_last_plan(self):
        # Auch ein von einer älteren Version angelegter Punkt (Text nur mit Gründen) nennt den Umfang
        self.open_manual()
        with state.locked(self.bot.cfg.state_dir) as st:
            st.last_plan = {"summary": "120 Pakete, Download 619 MB", "rows": [], "problems": []}
        self.bot.pump()
        text, buttons = self.shown()
        self.assertIn("120 Pakete, Download 619 MB\n• kritisches Paket", text)
        self.assertEqual(buttons[0][0], ("✅ Freigeben", "approve:abc"))

    def test_observed_and_manual_show_summary_alike(self):
        plan = {"summary": "7 Pakete, Download 3 MB", "rows": [], "problems": []}
        with state.locked(self.bot.cfg.state_dir) as st:
            st.last_plan, st.pending_text, st.observed_fingerprint = plan, "7 Pakete, Download 3 MB", "rtn"
        self.bot.pump()
        observed = self.shown()[0]
        with state.locked(self.bot.cfg.state_dir) as st:
            st.pending_text = ""
        self.open_manual()
        self.bot.pump()
        manual = self.shown()[0]
        self.assertIn("Ausstehend: 7 Pakete, Download 3 MB", observed)
        self.assertIn("7 Pakete, Download 3 MB", manual)
        self.assertNotIn("Ausstehend", manual)

    def test_button_unauthorized(self):
        self.open_manual()
        self.press("approve:abc", user={"id": 99, "is_bot": False})
        self.start_check.assert_not_called()
        self.assertEqual(self.tg.answered, ["Nicht berechtigt."])

    def test_details_in_state_message_and_back(self):
        self.open_reboot()
        self.set_plan()
        self.press("details")
        text, buttons = self.shown()
        self.assertIn("paket-000\n", text)
        # Was offen ist, bleibt über der Paketliste sichtbar
        self.assertIn("📋 <b>server1</b> · Neustart ausstehend", text)
        self.assertEqual(buttons, [[("🔢 Nach Version", "details:version:1"), ("📄 Als Datei", "file")],
                                   [("◀️ Zurück", "back")]])
        self.press("details:version:1")
        text, buttons = self.shown()
        self.assertIn("nur neu gebaut", text)
        self.assertEqual(buttons[0][0], ("🔤 Nach Name", "details:name:1"))
        self.press("back")
        self.assertIn("reboot", actions(self.shown()[1]))
        # Ein doppeltes „Zurück“ ändert nichts
        edits = len(self.tg.edited)
        self.press("back")
        self.assertEqual(len(self.tg.edited), edits)

    def test_details_stay_until_something_changes(self):
        self.set_plan()
        self.press("details")
        self.bot.pump()
        self.assertIn("paket-000", self.shown()[0])
        # Ein neuer Punkt führt zur Übersicht zurück, statt hinter der Paketliste zu verschwinden
        self.open_reboot()
        self.bot.pump()
        self.assertIn("reboot", actions(self.shown()[1]))
        self.assertNotIn("paket-000", self.shown()[0])

    def test_details_pages(self):
        self.set_plan(300)
        self.press("details")
        nav = self.shown()[1][1]
        self.assertEqual(nav[-1], ("➡️", "details:name:2"))
        self.assertEqual(len(nav), 2)
        self.press("details:name:2")
        self.assertEqual(self.shown()[1][1][0], ("⬅️", "details:name:1"))
        # Der Plan kann inzwischen kürzer sein
        self.press("details:name:99")
        self.assertIn("paket-", self.shown()[0])

    def test_conflict_details_have_no_list_buttons(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.last_plan = {"summary": "0 Pakete", "rows": [], "problems": ["Problem: 1: x"]}
        self.press("details")
        text, buttons = self.shown()
        self.assertIn("Problem: 1: x", text)
        self.assertEqual(buttons, [[("◀️ Zurück", "back")]])

    def test_details_command_brings_list_down(self):
        self.set_plan()
        self.bot.handle(message("/details@" + self.bot.me["username"]))
        self.assertIn("paket-000", self.tg.sent[-1])
        self.assertEqual(self.panel_id(), self.tg.next_id)

    def test_file_button_sends_document_and_replaces_previous(self):
        self.set_plan()
        self.press("file")
        name, content, caption = self.tg.documents[0]
        self.assertTrue(name.endswith(".html"))
        self.assertIn(b"paket-000", content)
        self.assertIn("1 Pakete", caption)
        first = self.st().messages["file"]["message_id"]
        self.press("file", message_id=99)  # auch von einer älteren Nachricht
        self.assertEqual(len(self.tg.documents), 2)
        self.assertIn(first, self.tg.deleted)

    def test_file_button_without_plan(self):
        self.press("file")
        self.assertEqual(self.tg.documents, [])
        self.assertEqual(self.tg.answered, ["Keine Paketliste vorhanden."])

    def test_button_later_snoozes_and_shows_it(self):
        self.open_manual()
        self.open_reboot()
        self.press("later:24")
        st = self.st()
        self.assertTrue(st.issues["reboot"].snoozed_until)
        self.assertTrue(st.issues["manual"].snoozed_until)
        self.assertIn("aufgeschoben bis", self.shown()[0])

    def test_old_later_button_snoozes_only_that_issue(self):
        self.open_manual()
        self.open_reboot()
        self.press("later:24:reboot")
        st = self.st()
        self.assertTrue(st.issues["reboot"].snoozed_until)
        self.assertFalse(st.issues["manual"].snoozed_until)

    def test_button_check_starts_and_shows_it(self):
        self.press("check")
        self.start_check.assert_called_once_with(requested=True)
        self.assertEqual(self.tg.answered, ["Prüfung läuft …"])

    def test_status_button_from_older_message(self):
        self.bot.pump()
        first = self.panel_id()
        self.press("status", message_id=12)
        self.assertNotEqual(self.panel_id(), first)
        self.assertEqual(self.tg.answered, [""])

    def test_resolved_button_closes_failed_apply(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.open_issue("apply-failed", "Skript kaputt")
        self.bot.pump()
        self.assertIn("resolved:apply-failed", actions(self.shown()[1]))
        self.press("resolved:apply-failed")
        self.assertNotIn("apply-failed", self.st().issues)
        self.assertIn("als behoben quittiert", self.tg.sent[-2])

    def test_resolved_button_only_for_failed_apply(self):
        self.open_manual()
        self.press("resolved:manual")
        self.assertIn("manual", self.st().issues)

    def test_dismiss_on_old_message_removes_it(self):
        self.press("dismiss", message_id=60)
        self.assertIn(60, self.tg.deleted)

    # --- Tastatur ---------------------------------------------------------

    def test_keyboard_text_is_broadcast_status(self):
        self.bot.handle(message("📊 Status"))
        self.assertEqual(len(self.tg.sent), 1)

    def test_keyboard_key_is_broadcast_even_as_reply_to_other_bot(self):
        msg = message("📊 Status", reply_to=OTHER_BOT)
        self.bot.handle(msg)
        self.assertEqual(len(self.tg.sent), 1)

    def test_keyboard_check_all(self):
        self.bot.handle(message("🔄 Alle prüfen"))
        self.start_check.assert_called_once()

    def test_keyboard_update_offers_update(self):
        with mock.patch.object(ota, "check_remote", return_value=ota.Pending("a" * 40, "a" * 40, [])):
            self.bot.handle(message("⬆️ Update"))
        self.assertIn("aktuell", self.tg.sent[0])

    def test_keyboard_version(self):
        self.bot.handle(message("🏷️ Version"))
        self.assertIn("twguard", self.tg.sent[0])

    def test_keyboard_has_two_rows(self):
        self.assertEqual([len(row) for row in bot_module.keyboard_markup()["keyboard"]], [2, 3])

    def test_keyboard_command_sends_persistent_keyboard(self):
        self.bot.handle(message("/keyboard@server1_bot"))
        self.assertTrue(self.tg.markups[0]["is_persistent"])
        first = self.tg.next_id
        self.bot.handle(message("/keyboard@server1_bot"))
        self.assertIn(first, self.tg.deleted)
        self.bot.handle(message("/keyboard@server1_bot off"))
        self.assertIn(self.tg.next_id, self.tg.deleted)

    # --- Update von twguard ----------------------------------------------

    def test_update_offer_and_go(self):
        pending = ota.Pending("a" * 40, "b" * 40, ["bbbbbbb Neue Funktion"], "9.9.9")
        with mock.patch.object(ota, "check_remote", return_value=pending), \
             mock.patch.object(ota, "start", return_value="") as start:
            self.bot.handle(message("/update"))  # ohne @botname: alle Server antworten
            text, buttons = self.shown()
            self.assertIn("Neue Funktion", text)
            # Version statt langer Hashes, für Ziel und laufenden Stand
            self.assertIn("twguard v9.9.9 (bbbbbbbb) verfügbar", text)
            self.assertIn(f"läuft mit {ota.running()}", text)
            self.assertIn(("⬆️ Einspielen", "update:" + "b" * 40), buttons[0])
            self.press("update:" + "b" * 40)
            start.assert_called_once_with("b" * 40)
        text, buttons = self.shown()
        self.assertIn("Update von twguard auf v9.9.9 (bbbbbbbb) läuft", text)
        # Die Frist gilt erst nach Tests und Installation, nicht ab dem Knopfdruck
        self.assertIn(f"danach wartet twguard bis zu {ota.START_TIMEOUT} s auf den neuen Bot", text)
        self.assertNotIn("update:", str(buttons))
        st = self.st()
        self.assertEqual(st.update_request["commit"], "b" * 40)
        self.assertEqual(st.update_offer, {})

    def test_update_refuses_changed_target(self):
        pending = ota.Pending("a" * 40, "c" * 40, ["ccccccc Noch neuer"])
        with state.locked(self.bot.cfg.state_dir) as st:
            st.update_offer = {"target": "b" * 40, "label": "b", "running": "a", "commits": [], "at": time.time()}
        with mock.patch.object(ota, "check_remote", return_value=pending), \
             mock.patch.object(ota, "start") as start:
            self.press("update:" + "b" * 40)
            start.assert_not_called()
        self.assertIn("anderen Stand", self.tg.answered[0])

    def test_update_offer_dismissed_and_expires(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.update_offer = {"target": "b" * 40, "label": "b", "running": "a", "commits": [], "at": time.time()}
        self.press("dismiss")
        self.assertEqual(self.st().update_offer, {})
        with state.locked(self.bot.cfg.state_dir) as st:
            st.update_offer = {"target": "b" * 40, "label": "b", "running": "a", "commits": [],
                               "at": time.time() - bot_module.OFFER_MAX - 1}
        self.bot.pump()
        self.assertEqual(self.st().update_offer, {})

    # --- Prüfen und Freigeben ------------------------------------------

    def test_check_command_starts_and_brings_state_down(self):
        self.bot.handle(message("/check@server1_bot"))
        self.start_check.assert_called_once_with(requested=True)
        self.assertEqual(self.tg.typed, [-100])
        self.assertEqual(len(self.tg.sent), 1)
        self.assertEqual(self.panel_id(), self.tg.next_id)

    def test_approve_refuses_plan_not_shown_and_shows_it(self):
        self.open_manual()
        with state.locked(self.bot.cfg.state_dir) as st:
            st.open_issue("manual", "• neuer Plan B", fingerprint="bbb", approvable=True)
            st.issues["manual"].last_notified = time.time()
        self.bot.handle(message("/approve", reply_to=ME))
        self.start_check.assert_not_called()
        self.assertIn("neuer Plan B", self.shown()[0])
        self.assertEqual(self.shown()[1][0][0][1], "approve:bbb")
        st = self.st()
        self.assertEqual(st.approved_fingerprint, "")
        self.assertEqual(st.manual_shown, "bbb")
        # Jetzt ist B gezeigt und gilt
        self.bot.handle(message("/approve", reply_to=ME))
        self.start_check.assert_called_once()
        self.assertEqual(self.st().approved_fingerprint, "bbb")

    def test_manual_shown_only_after_delivery(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.open_issue("manual", "x", fingerprint="abc", approvable=True)
        self.tg.fail_send = True
        self.bot.pump()
        self.assertEqual(self.st().manual_shown, "")

    # --- Dienst-Neustart --------------------------------------------------

    def test_tick_offers_restart_with_buttons(self):
        self.found.return_value = ["postfix", "sshd"]
        with mock.patch.object(zypper, "needs_rebooting", return_value=False):
            self.bot.check_reboot()
            self.bot.tick()
        self.bot.pump()
        data = actions(self.shown()[1])
        self.assertEqual(data[:3], ["restart:postfix", "restart:sshd",
                                    f"restart:*:{restarts.digest(['postfix', 'sshd'])}"])

    # Was an der Konsole geschah, zeigen „Jetzt prüfen“ und „Status“ sofort (nicht erst nach 30 min)
    WAYS = {
        "Knopf Jetzt prüfen": lambda self: self.press("check"),
        "/check": lambda self: self.bot.handle(message("/check@server1_bot")),
        "Knopf Status": lambda self: self.press("status"),
        "/status": lambda self: self.bot.handle(message("/status")),
    }

    def test_ways_take_over_services_restarted_on_console(self):
        for name, way in self.WAYS.items():
            with self.subTest(name):
                self.offer("postfix")
                self.found.return_value = []
                way(self)
                self.assertNotIn("services", self.st().issues)

    def test_ways_show_new_services_at_once(self):
        for name, way in self.WAYS.items():
            with self.subTest(name):
                self.offer("postfix")
                self.found.return_value = ["postfix", "sshd"]
                way(self)
                self.assertEqual(self.st().restart_services, ["postfix", "sshd"])

    def test_ways_keep_services_when_unknown(self):
        for name, way in self.WAYS.items():
            with self.subTest(name):
                self.offer("postfix")
                self.found.return_value = None
                way(self)
                self.assertEqual(self.st().restart_services, ["postfix"])

    def test_ways_keep_services_during_restart(self):
        for name, way in self.WAYS.items():
            with self.subTest(name):
                self.offer("postfix")
                with state.locked(self.bot.cfg.state_dir) as st:
                    st.restart_request = {"at": time.time(), "services": ["postfix"]}
                self.found.return_value = []
                way(self)
                self.assertIn("services", self.st().issues)
                with state.locked(self.bot.cfg.state_dir) as st:
                    st.restart_request = {}

    def test_check_while_running_does_not_query(self):
        # Prüft schon (noop): weder Dienste noch Neustart abfragen; während des Prüflaufs setzt der Abgleich aus
        self.offer("postfix")
        self.checking.return_value = True
        self.found.reset_mock()
        for way in (self.WAYS["Knopf Jetzt prüfen"], self.WAYS["/check"]):
            way(self)
        self.found.assert_not_called()
        self.rebooting.assert_not_called()
        self.start_check.assert_not_called()

    def test_check_queries_services_before_check_and_reboot_only_there(self):
        order = []
        self.found.side_effect = lambda: order.append("services") or []
        self.start_check.side_effect = lambda *a, **k: order.append("check")
        self.press("check")
        self.assertEqual(order, ["services", "check"])
        self.rebooting.assert_not_called()  # den Neustart fragt start_check selbst ab

    def test_status_during_check_queries_reboot_not_services(self):
        self.offer("postfix")
        self.checking.return_value = True
        self.found.reset_mock()
        self.bot.handle(message("/status"))
        self.found.assert_not_called()
        self.rebooting.assert_called_once()

    def test_details_queries_only_reboot(self):
        self.offer("postfix")
        self.found.reset_mock()
        self.bot.handle(message("/details@server1_bot"))
        self.found.assert_not_called()
        self.rebooting.assert_called_once()
        self.assertEqual(self.st().restart_services, ["postfix"])

    def test_no_scan_while_check_runs(self):
        self.checking.return_value = True
        self.bot.check_services()
        self.found.assert_not_called()

    def test_button_restarts_one_service(self):
        self.offer("postfix", "sshd")
        with mock.patch.object(restarts, "start", return_value={}) as start:
            self.press("restart:sshd")
        start.assert_called_once_with(["sshd"])
        self.assertIn("Dienste werden neu gestartet: sshd", self.shown()[0])
        st = self.st()
        self.assertEqual(st.restart_request["services"], ["sshd"])
        self.assertNotIn("services", st.issues)

    def test_reboot_button_hidden_while_services_restart_then_back(self):
        # Beobachtung auf einem Server: Nach dem Dienst-Neustart war der Neustart-Knopf verschwunden.
        # Während des Neustarts würde ein Druck abgewiesen, also kein Knopf; danach kommt er wieder.
        self.open_reboot()
        self.offer("postfix")
        with mock.patch.object(restarts, "start", return_value={}):
            self.press("restart:postfix")
        text, buttons = self.shown()
        self.assertNotIn("reboot", actions(buttons))
        self.assertIn("Dienste werden neu gestartet", text)
        self.assertIn("Neustart ausstehend", text)  # der Punkt selbst bleibt sichtbar
        # Ein Druck auf einen noch stehenden Knopf sagt weiterhin, warum es gerade nicht geht
        with mock.patch("subprocess.run") as run:
            self.press("reboot")
        run.assert_not_called()
        self.assertIn("Dienste werden gerade neu gestartet", self.tg.answered[-1])
        self.found.return_value = []
        with mock.patch.object(restarts, "pending", return_value=[]), \
                mock.patch.object(restarts, "states", return_value={"postfix": "active"}):
            self.bot.watch_restart()
        self.assertIn("reboot", actions(self.shown()[1]))

    def test_buttons_hidden_by_the_same_table_as_refusals(self):
        # Eine Regel für alle: was busy.BLOCKERS abweisen würde, bietet die Zustandsnachricht nicht an
        self.open_reboot()
        self.offer("postfix")
        with state.locked(self.bot.cfg.state_dir) as st:
            st.zypp_holder = "purge-kernels.service (zypper, PID 707)"
        self.bot.pump()
        shown = actions(self.shown()[1])
        self.assertNotIn("reboot", shown)
        self.assertFalse([a for a in shown if a.startswith("restart:")])
        self.assertIn("check", shown)  # Prüfen wartet selbst auf die Paketverwaltung
        with state.locked(self.bot.cfg.state_dir) as st:
            st.zypp_holder = ""
            st.shutdown = {"state": "planned", "mode": "reboot", "at": time.time() + 60}
        self.bot.pump()
        shown = actions(self.shown()[1])
        self.assertIn("cancel", shown)
        self.assertNotIn("check", shown)
        self.assertFalse([a for a in shown if a.startswith("restart:")])
        self.assertIn("later:24", shown)
        with state.locked(self.bot.cfg.state_dir) as st:
            st.shutdown = {}
        self.bot.pump()
        shown = actions(self.shown()[1])
        self.assertIn("reboot", shown)
        self.assertIn("restart:postfix", shown)

    def test_approve_hidden_and_refused_while_installing(self):
        # Fund vom 2026-11-13: „Approve“ stand noch da, während der freigegebene Plan eingespielt wurde
        self.open_manual()
        with state.locked(self.bot.cfg.state_dir) as st:
            st.applying = state.holder()
            st.activity = {"check": {"phase": "apply", "detail": "1 Paket", "at": time.time()}}
        self.bot.pump()
        shown = actions(self.shown()[1])
        self.assertNotIn("approve:abc", shown)
        self.assertIn("details", shown)
        self.press("approve:abc", message_id=self.panel_id())
        self.assertNotIn("Bitte bestätigen", self.tg.answered)
        self.assertNotIn("confirm:abc", actions(self.shown()[1]))
        # „1 Tag später“ ist während des Einspielens ebenfalls weg; ein alter Druck bekommt den Grund
        self.assertNotIn("later:24", shown)
        self.press("later:24", message_id=self.panel_id())
        self.assertEqual(self.st().issues["manual"].snoozed_until, 0.0)
        self.assertIn("spielt gerade ein", self.tg.answered[-1])

    def test_later_hidden_while_checking(self):
        # Fund vom 2026-11-13: während des Prüflaufs nach „Ja“ stand „1 Tag später“ noch da
        self.open_manual()
        with state.locked(self.bot.cfg.state_dir) as st:
            st.activity = {"check": {"phase": "refresh", "at": time.time()}}
        self.checking.return_value = True
        self.bot.pump()
        shown = actions(self.shown()[1])
        self.assertNotIn("later:24", shown)
        self.assertNotIn("approve:abc", shown)
        self.assertIn("details", shown)
        self.press("later:24", message_id=self.panel_id())
        self.assertEqual(self.st().issues["manual"].snoozed_until, 0.0)
        self.assertIn("Prüf", self.tg.answered[-1])

    def test_restart_refused_while_check_applies(self):
        self.offer("postfix")
        with state.locked(self.bot.cfg.state_dir) as st:
            st.applying = state.holder()
        with mock.patch.object(restarts, "start") as start:
            self.press("restart:postfix")
        start.assert_not_called()
        self.assertIn("twguard spielt gerade ein", self.tg.answered[-1])
        self.assertEqual(self.st().restart_request, {})

    def test_restart_refused_while_other_process_applies(self):
        # Reservierung eines anderen lebenden Prozesses (direkter CLI-Prüflauf, kein systemd-Dienst)
        self.offer("postfix")
        with state.locked(self.bot.cfg.state_dir) as st:
            st.applying = {**state.holder(), "pid": os.getppid()}
        with mock.patch.object(restarts, "start") as start:
            self.press("restart:postfix")
        start.assert_not_called()
        answer = self.bot.cmd_mode(["observe"], addressed=True)
        self.assertIn("läuft noch zu Ende", answer.text)

    def test_check_reserving_after_precheck_wins(self):
        # Der Timer startet nach der Vorprüfung; während zypper ps läuft, reserviert er das Einspielen
        self.offer("postfix")

        def check_reserves():
            with state.locked(self.bot.cfg.state_dir) as st:
                st.applying = state.holder()
            return ["postfix"]
        self.bot.pump()
        self.found.side_effect = check_reserves
        with mock.patch.object(restarts, "start") as start:
            self.press("restart:postfix")
        start.assert_not_called()
        st = self.st()
        self.assertEqual(st.restart_request, {})
        self.assertIn("services", st.issues)  # der Punkt bleibt offen

    def test_stale_reservation_does_not_block_restart(self):
        self.offer("postfix")
        with state.locked(self.bot.cfg.state_dir) as st:
            st.applying = {**state.holder(), "boot": "vor-dem-reboot"}
        with mock.patch.object(restarts, "start", return_value={}) as start:
            self.press("restart:postfix")
        start.assert_called_once_with(["postfix"])

    def test_restart_reserved_and_shown_before_it_starts(self):
        # Ist der Bot selbst dabei, wird er gleich beendet: vorher muss alles gespeichert und gezeigt sein
        self.offer("twguard-bot")
        seen = []

        def start(names):
            seen.append((self.st().restart_request.get("services"), self.shown()[0]))
            return {}
        with mock.patch.object(restarts, "start", side_effect=start):
            self.press("restart:twguard-bot")
        self.assertEqual(seen[0][0], ["twguard-bot"])
        self.assertIn("Dienste werden neu gestartet: twguard-bot", seen[0][1])

    def test_bot_restart_keeps_restart_reservation(self):
        self.offer("twguard-bot")
        with mock.patch.object(restarts, "start", return_value={}):
            self.press("restart:twguard-bot")
        with mock.patch.object(Bot, "_set_menu"):
            Bot(self.bot.cfg, tg=self.tg, me=ME).startup()
        self.assertEqual(self.st().restart_request["services"], ["twguard-bot"])

    def all_button(self, *names):
        return f"restart:*:{restarts.digest(list(names))}"

    def test_all_covers_only_what_was_offered(self):
        self.offer("postfix", "sshd")
        self.found.return_value = ["postfix", "sshd", "unseen"]
        with mock.patch.object(restarts, "start", return_value={}) as start:
            self.press(self.all_button("postfix", "sshd"))
        start.assert_called_once_with(["postfix", "sshd"])

    def test_old_all_button_does_not_cover_later_service(self):
        self.offer("postfix", "sshd")
        self.bot.pump()
        self.offer("mariadb", "postfix", "sshd")  # erkannt, aber noch nicht gezeigt
        with mock.patch.object(restarts, "start") as start:
            self.press(self.all_button("postfix", "sshd"))
        start.assert_not_called()
        # Stattdessen zeigt die Zustandsnachricht jetzt den aktuellen Stand mit aktuellem Knopf
        self.assertIn(self.all_button("mariadb", "postfix", "sshd"), actions(self.shown()[1]))

    def test_used_all_button_cannot_fire_again(self):
        self.offer("sshd", "postfix")
        with state.locked(self.bot.cfg.state_dir) as st:
            names = list(st.restart_services)
            st.restart_services = []
        with mock.patch.object(restarts, "start") as start:
            self.press(self.all_button(*names))
        start.assert_not_called()

    def test_unknown_scan_restarts_nothing(self):
        self.offer("sshd")
        self.bot.pump()
        self.found.return_value = None
        with mock.patch.object(restarts, "start") as start:
            self.press("restart:sshd")
        start.assert_not_called()
        self.assertIn("services", self.st().issues)

    def test_no_restart_during_check_or_update(self):
        self.offer("sshd")
        self.checking.return_value = True
        with mock.patch.object(restarts, "start") as start:
            self.press("restart:sshd")
            self.checking.return_value = False
            with state.locked(self.bot.cfg.state_dir) as st:
                st.update_request = {"commit": "x", "at": time.time()}
            self.press("restart:sshd")
        start.assert_not_called()

    def test_bot_itself_is_restarted_last_after_saving_errors(self):
        self.offer("postfix", "twguard-bot", "zzz")
        order = []

        def start(names):
            with state.locked(self.bot.cfg.state_dir) as st:
                order.append((names, dict(st.restart_request.get("errors", {}))))
            return {"zzz": "maskiert"} if "zzz" in names else {}

        with mock.patch.object(restarts, "start", side_effect=start):
            self.press(self.all_button("postfix", "twguard-bot", "zzz"))
        self.assertEqual(order, [(["postfix", "zzz"], {}), (["twguard-bot"], {"zzz": "maskiert"})])

    def test_button_cannot_restart_foreign_service(self):
        self.offer("sshd")
        self.found.return_value = ["sshd", "mariadb"]
        with mock.patch.object(restarts, "start") as start:
            self.press("restart:mariadb")
        start.assert_not_called()

    def test_gone_service_is_not_restarted_rest_offered(self):
        self.offer("postfix", "sshd")
        self.bot.pump()
        self.found.return_value = ["postfix"]
        with mock.patch.object(restarts, "start") as start:
            self.press("restart:sshd")
        start.assert_not_called()
        self.assertIn("geändert", self.tg.answered[-1])
        self.assertEqual(self.st().restart_services, ["postfix"])
        self.assertIn("restart:postfix", actions(self.shown()[1]))
        self.assertNotIn("restart:sshd", actions(self.shown()[1]))

    def test_second_press_while_running_refused(self):
        self.offer("sshd")
        with state.locked(self.bot.cfg.state_dir) as st:
            st.restart_request = {"services": ["postfix"], "at": time.time()}
        with mock.patch.object(restarts, "start") as start:
            self.press("restart:sshd")
        start.assert_not_called()

    def running(self, *names):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.restart_request = {"at": time.time(), "services": list(names)}

    def outbox(self):
        return [(e["text"], e["loud"]) for e in self.st().outbox]

    def test_partly_rejected_restart_is_still_followed(self):
        self.offer("postfix", "sshd")
        with mock.patch.object(restarts, "start", return_value={"sshd": "Unit <x> masked"}):
            self.press(self.all_button("postfix", "sshd"))
        self.found.return_value = ["sshd"]
        with mock.patch.object(restarts, "pending", return_value=[]) as pending, \
                mock.patch.object(restarts, "states", return_value={"postfix": "active"}):
            self.bot.watch_restart()
        pending.assert_called_once_with(["postfix"])
        event = next(t for t in self.tg.sent if "neu gestartet: postfix" in t)
        self.assertIn("Unit &lt;x&gt; masked", event)
        self.assertFalse(self.tg.silent[self.tg.sent.index(event)])
        self.assertEqual(self.st().restart_services, ["sshd"])  # wird wieder angeboten

    def test_restart_with_left_over_processes_is_done_and_named(self):
        # KillMode=process: Der Dienst ist erneuert, eine Sitzung hält weiter alte Dateien
        self.offer("cron", "sshd")
        with mock.patch.object(restarts, "start", return_value={}):
            self.press(self.all_button("cron", "sshd"))
        self.found.return_value, self.lingering = ["cron", "sshd"], {"sshd": [zypper.Proc(6341, "alice", "screen", "sshd")]}
        with mock.patch.object(restarts, "pending", return_value=[]), \
                mock.patch.object(restarts, "states", return_value={"cron": "active", "sshd": "active"}):
            self.bot.watch_restart()
        result = next(t for t in self.tg.sent if "cron" in t and not t.startswith("⚠️ <b>server1</b>: 1"))
        self.assertIn("nutzt weiter alte Dateien: cron", result)
        self.assertIn("Dienst sshd neu gestartet. Prozess screen (PID 6341, alice) nutzt bis zu seinem Ende "
                      "weiter gelöschte Dateien.", result)
        self.assertNotIn("✅", result)
        st = self.st()
        self.assertEqual(st.restart_services, ["cron"])
        self.assertEqual(list(st.lingering), ["sshd"])
        self.assertIn("Prozess screen (PID 6341, alice)", self.shown()[0])
        self.assertNotIn("restart:sshd", actions(self.shown()[1]))

    def test_result_waits_for_jobs(self):
        self.running("sshd")
        with mock.patch.object(restarts, "pending", return_value=["sshd"]):
            self.bot.watch_restart()
        self.assertEqual(self.outbox(), [])
        self.assertTrue(self.st().restart_request)

    def test_result_success_is_quiet_event(self):
        self.running("sshd")
        self.found.return_value = []
        with mock.patch.object(restarts, "pending", return_value=[]), \
                mock.patch.object(restarts, "states", return_value={"sshd": "active"}):
            self.bot.watch_restart()
        self.assertEqual(self.tg.sent[0], "✅ <b>server1</b>: neu gestartet: sshd")
        self.assertTrue(self.tg.silent[0])
        self.assertEqual(self.st().restart_request, {})

    def test_result_failure_comes_with_sound(self):
        self.running("sshd")
        with mock.patch.object(restarts, "pending", return_value=[]), \
                mock.patch.object(restarts, "states", return_value={"sshd": "failed"}):
            self.bot.watch_restart()
        self.assertIn("sshd (failed)", self.tg.sent[0])
        self.assertFalse(self.tg.silent[0])

    def test_just_started_service_is_not_yet_success(self):
        # Type=simple: Auftrag erledigt, das Programm scheitert erst Millisekunden später (203/EXEC)
        self.running("sshd")
        self.found.return_value = []
        self.young.return_value = ["sshd"]
        with mock.patch.object(restarts, "pending", return_value=[]), \
                mock.patch.object(restarts, "states", return_value={"sshd": "active"}):
            self.bot.watch_restart()
        self.assertEqual(self.outbox(), [])
        self.assertEqual(self.tg.sent, [])
        self.assertTrue(self.st().restart_request)
        self.young.return_value = []
        with mock.patch.object(restarts, "pending", return_value=[]), \
                mock.patch.object(restarts, "states", return_value={"sshd": "failed"}):
            self.bot.watch_restart()
        self.assertIn("sshd (failed)", self.tg.sent[0])
        self.assertFalse(self.tg.silent[0])

    def test_late_job_end_still_waits_for_young_service(self):
        # F25 (Codex): Auftrag endet erst nach RESTART_TIMEOUT; auch dann kein sofortiger Erfolg
        self.running("sshd")
        with state.locked(self.bot.cfg.state_dir) as st:
            st.restart_request["at"] -= bot_module.RESTART_TIMEOUT + 1
            st.restart_request["overdue"] = True
        self.found.return_value = []
        self.young.return_value = ["sshd"]
        with mock.patch.object(restarts, "pending", return_value=[]), \
                mock.patch.object(restarts, "states", return_value={"sshd": "active"}):
            self.bot.watch_restart()
        self.assertEqual(self.outbox(), [])
        self.assertEqual(self.tg.sent, [])
        self.young.return_value = []
        with mock.patch.object(restarts, "pending", return_value=[]), \
                mock.patch.object(restarts, "states", return_value={"sshd": "failed"}):
            self.bot.watch_restart()
        self.assertIn("sshd (failed)", self.tg.sent[0])
        self.assertFalse(self.tg.silent[0])

    def test_late_job_end_stable_service_is_success(self):
        self.running("sshd")
        with state.locked(self.bot.cfg.state_dir) as st:
            st.restart_request["at"] -= bot_module.RESTART_TIMEOUT + 1
        self.found.return_value = []
        self.young.return_value = ["sshd"]
        with mock.patch.object(restarts, "pending", return_value=[]), \
                mock.patch.object(restarts, "states", return_value={"sshd": "active"}):
            self.bot.watch_restart()
            self.assertEqual(self.tg.sent, [])
            self.young.return_value = []
            self.bot.watch_restart()
        self.assertEqual(self.tg.sent[0], "✅ <b>server1</b>: neu gestartet: sshd")

    def test_service_in_restart_loop_does_not_hold_result_forever(self):
        # Ein Dienst in einer Neustartschleife ist bei jeder Abfrage jung: Nach drei Mindestlaufzeiten endet das
        # Warten, aber er gilt nicht als neu gestartet; der stabile Dienst desselben Auftrags schon
        self.running("postfix", "sshd")
        self.found.return_value = []
        self.young.return_value = ["sshd"]
        with mock.patch.object(restarts, "pending", return_value=[]), \
                mock.patch.object(restarts, "states", return_value={"postfix": "active", "sshd": "active"}):
            self.bot.watch_restart()
            self.assertEqual(self.tg.sent, [])
            with state.locked(self.bot.cfg.state_dir) as st:
                st.restart_request["settling_since"] -= bot_module.SETTLE_ROUNDS * restarts.SETTLE_SECONDS + 1
            self.bot.watch_restart()
        self.assertEqual(self.tg.sent[0].splitlines(), [
            "✅ <b>server1</b>: neu gestartet: postfix",
            "⚠️ <b>server1</b>: aktiv, aber nicht nachweislich 10 s am Stück (Neustartschleife oder Startzeit "
            "unbekannt), Ergebnis nicht bestätigt: sshd"])
        self.assertFalse(self.tg.silent[0])
        self.assertEqual(self.st().restart_request, {})

    def test_unknown_start_time_waits_then_reports(self):
        # Ohne Startzeit ist die Mindestlaufzeit nicht nachgewiesen: an der Grenze melden, aber nicht als Erfolg
        self.running("sshd")
        self.found.return_value = []
        self.young.return_value = None
        with mock.patch.object(restarts, "pending", return_value=[]), \
                mock.patch.object(restarts, "states", return_value={"sshd": "active"}):
            self.bot.watch_restart()
            self.assertEqual(self.tg.sent, [])
            with state.locked(self.bot.cfg.state_dir) as st:
                st.restart_request["at"] -= 1000
            self.bot.watch_restart()
        self.assertIn("Ergebnis nicht bestätigt: sshd", self.tg.sent[0])
        self.assertNotIn("✅", self.tg.sent[0])
        self.assertFalse(self.tg.silent[0])

    def test_late_job_end_with_unknown_start_time_is_not_success(self):
        self.running("sshd")
        with state.locked(self.bot.cfg.state_dir) as st:
            st.restart_request["at"] -= bot_module.RESTART_TIMEOUT + 1
        self.found.return_value = []
        self.young.return_value = None
        with mock.patch.object(restarts, "pending", return_value=[]), \
                mock.patch.object(restarts, "states", return_value={"sshd": "active"}):
            self.bot.watch_restart()
        self.assertNotIn("✅", self.tg.sent[0])
        self.assertIn("Ergebnis nicht bestätigt: sshd", self.tg.sent[0])
        self.assertFalse(self.tg.silent[0])

    def test_result_still_old_is_offered_again(self):
        self.running("sshd")
        self.found.return_value = ["sshd"]
        with mock.patch.object(restarts, "pending", return_value=[]), \
                mock.patch.object(restarts, "states", return_value={"sshd": "active"}):
            self.bot.watch_restart()
        self.assertIn("alte Dateien", self.tg.sent[0])
        self.assertIn("restart:sshd", actions(self.shown()[1]))

    def test_unknown_scan_after_restart_waits_then_no_green(self):
        self.running("sshd")
        self.found.return_value = None
        with mock.patch.object(restarts, "pending", return_value=[]), \
                mock.patch.object(restarts, "states", return_value={"sshd": "active"}):
            self.bot.watch_restart()
            self.assertEqual(self.outbox(), [])
            with state.locked(self.bot.cfg.state_dir) as st:
                st.restart_request["at"] -= 1000
            self.bot.watch_restart()
        self.assertNotIn("✅", self.tg.sent[0])
        self.assertIn("keine Auskunft", self.tg.sent[0])

    def test_result_survives_telegram_outage(self):
        self.running("sshd")
        self.found.return_value = []
        self.tg.fail_send = True
        with mock.patch.object(restarts, "pending", return_value=[]), \
                mock.patch.object(restarts, "states", return_value={"sshd": "failed"}):
            self.bot.watch_restart()
        self.assertIn("sshd (failed)", self.outbox()[0][0])
        self.tg.fail_send = False
        self.bot.pump()
        self.assertIn("sshd (failed)", self.tg.sent[0])
        self.assertEqual(self.outbox(), [])

    def test_second_result_during_outage_keeps_first(self):
        self.found.return_value = []
        self.tg.fail_send = True
        with mock.patch.object(restarts, "pending", return_value=[]):
            self.running("sshd")
            with mock.patch.object(restarts, "states", return_value={"sshd": "failed"}):
                self.bot.watch_restart()
            self.running("postfix")
            with mock.patch.object(restarts, "states", return_value={"postfix": "active"}):
                self.bot.watch_restart()
        self.assertEqual(len(self.outbox()), 2)
        self.tg.fail_send = False
        self.bot.pump()
        self.assertIn("sshd (failed)", self.tg.sent[0])
        self.assertIn("neu gestartet: postfix", self.tg.sent[0])
        self.assertEqual(self.outbox(), [])

    def overdue(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.restart_request["at"] -= 1000

    def test_job_past_limit_warns_once_and_keeps_watching(self):
        self.running("sshd")
        self.overdue()
        self.found.return_value = []
        with mock.patch.object(restarts, "pending", return_value=["sshd"]), \
                mock.patch.object(restarts, "states", return_value={"sshd": "active"}):
            self.bot.watch_restart()
            self.bot.watch_restart()
        warnings = [t for t in self.tg.sent if "nicht abgeschlossen" in t]
        self.assertEqual(len(warnings), 1)
        self.assertNotIn("✅", warnings[0])
        self.assertTrue(self.st().restart_request)
        self.assertIn("dauert ungewöhnlich lange", self.shown()[0])
        # Später endet der Auftrag: Auch ein spätes Scheitern wird gemeldet
        with mock.patch.object(restarts, "pending", return_value=[]), \
                mock.patch.object(restarts, "states", return_value={"sshd": "failed"}):
            self.bot.watch_restart()
        self.assertTrue(any("sshd (failed)" in t for t in self.tg.sent))
        self.assertEqual(self.st().restart_request, {})

    def test_unknown_jobs_past_limit_are_not_green(self):
        self.running("sshd")
        self.overdue()
        self.found.return_value = []
        with mock.patch.object(restarts, "pending", return_value=None), \
                mock.patch.object(restarts, "states", return_value={"sshd": "active"}):
            self.bot.watch_restart()
        self.assertNotIn("✅", self.tg.sent[0])
        self.assertIn("Ergebnis des Neustarts unbekannt", self.tg.sent[0])
        self.assertFalse(self.tg.silent[0])
        self.assertEqual(self.st().restart_request, {})

    def test_unknown_jobs_within_limit_wait(self):
        self.running("sshd")
        with mock.patch.object(restarts, "pending", return_value=None):
            self.bot.watch_restart()
        self.assertEqual(self.outbox(), [])

    # --- Server-Neustart ----------------------------------------------------

    def plan_reboot(self):
        # So meldet logind einen mit shutdown -r +1 geplanten Neustart
        self.logind.return_value = busy.Shutdown("planned", "reboot", time.time() + 50)
        with state.locked(self.bot.cfg.state_dir) as st:
            busy.note_shutdown(st, self.logind.return_value)

    def test_reboot_button_plans_and_offers_cancel(self):
        self.open_reboot()

        def shutdown(cmd, **kwargs):
            self.logind.return_value = busy.Shutdown("planned", "reboot", time.time() + 60)
            return mock.Mock(returncode=0, stderr="")
        with mock.patch("subprocess.run", side_effect=shutdown) as run:
            self.press("reboot")
        self.assertEqual(run.call_args.args[0][:3], ["shutdown", "-r", "+1"])
        self.assertEqual(self.tg.answered, ["Neustart in 1 Minute"])
        self.assertIn("Neustart per Telegram ausgelöst (Neustart geplant um", self.tg.sent[-2])
        text, buttons = self.shown()
        self.assertIn("🔁 Neustart geplant um", text)
        self.assertNotIn("Neustart ausstehend", text)
        self.assertIn("cancel", actions(buttons))
        self.assertNotIn("reboot", actions(buttons))
        self.assertTrue(self.st().reboot_requested_at)

    def test_cancel_reoffers_reboot_while_needed(self):
        self.open_reboot()
        self.plan_reboot()
        with state.locked(self.bot.cfg.state_dir) as st:
            st.reboot_requested_at = time.time()
        self.bot.pump()
        self.logind.return_value = busy.Shutdown("none")
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stderr="")) as run:
            self.press("cancel")
        self.assertEqual(run.call_args.args[0], ["shutdown", "-c"])
        self.assertIn("Neustart abgebrochen", self.tg.sent[-2])
        actions_now = actions(self.shown()[1])
        self.assertIn("reboot", actions_now)
        self.assertNotIn("cancel", actions_now)
        st = self.st()
        self.assertFalse(st.reboot_requested_at)
        self.assertEqual(st.shutdown, {})

    def test_cancel_that_did_not_work_says_so(self):
        self.plan_reboot()
        with state.locked(self.bot.cfg.state_dir) as st:
            st.reboot_requested_at = time.time()
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stderr="")):
            self.press("cancel")
        self.assertIn("Abbrechen fehlgeschlagen", self.tg.answered[-1])
        self.assertIn("cancel", actions(self.shown()[1]))
        self.assertTrue(self.st().reboot_requested_at)

    def test_cancel_command_without_plan_says_so(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=1, stderr="nichts geplant")):
            self.bot.handle(message("/cancel@server1_bot"))
        self.assertIn("Abbrechen fehlgeschlagen: nichts geplant", self.tg.sent[-1])

    def test_reboot_refused_while_package_management_locked(self):
        # purge-kernels nach dem Boot oder ein zypper per SSH: Ein Neustart würde es mittendrin abbrechen
        self.open_reboot()
        self.holder.return_value = "purge-kernels.service (zypper, PID 707)"
        with mock.patch("subprocess.run") as run:
            self.press("reboot")
        run.assert_not_called()
        self.assertIn("purge-kernels.service", self.tg.answered[-1])
        self.assertFalse(self.st().reboot_requested_at)

    def test_reboot_refused_while_twguard_applies(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.applying = state.holder()  # dieser Prozess lebt
        with mock.patch("subprocess.run") as run:
            self.bot.handle(message("/reboot@server1_bot"))
        run.assert_not_called()
        self.assertIn("twguard spielt gerade ein", self.tg.sent[-1])

    def test_nothing_new_starts_while_reboot_planned(self):
        # Prüflauf, Freigabe, Update und Dienst-Neustart brächen mit dem Neustart ab, ihr Ergebnis käme nie an
        self.open_manual()
        self.offer("postfix")
        self.plan_reboot()
        self.bot.handle(message("/check@server1_bot"))
        self.assertIn("Neustart geplant um", self.tg.sent[-1])
        self.press("check")
        self.press("approve:abc")
        self.bot.handle(message("/update@server1_bot"))
        with mock.patch.object(restarts, "start") as start:
            self.press("restart:postfix")
        start.assert_not_called()
        self.start_check.assert_not_called()
        self.assertEqual(sum("Neustart geplant um" in a for a in self.tg.answered), 3)
        self.assertIn("Neustart geplant um", self.tg.sent[-1])
        st = self.st()
        self.assertEqual(st.approved_fingerprint, "")
        self.assertEqual(st.update_request, {})

    def test_reboot_planned_on_console_is_recognised(self):
        # Nicht nur selbst ausgelöste: logind kennt auch shutdown -r +5 auf der Konsole
        self.logind.return_value = busy.Shutdown("planned", "reboot", time.time() + 300)
        self.bot.handle(message("/check@server1_bot"))
        self.start_check.assert_not_called()
        self.assertIn("Neustart geplant um", self.tg.sent[-1])
        self.bot.tick()
        self.bot.pump()
        self.assertIn("cancel", actions(self.shown()[1]))

    def test_second_reboot_does_not_postpone_the_first(self):
        self.plan_reboot()
        with mock.patch("subprocess.run") as run:
            self.bot.handle(message("/reboot@server1_bot"))
        run.assert_not_called()
        self.assertIn("Neustart geplant um", self.tg.sent[-1])

    def test_reboot_planning_excludes_apply_in_between(self):
        # F17: Zwischen Prüfung und shutdown darf kein Prüflauf reservieren
        seen = []

        def shutdown(cmd, **kwargs):
            cfg = Config(language="de", token="x", chat_id=-100, hostname="server1", mode="auto",
                         state_dir=self.bot.cfg.state_dir)
            seen.append(check._reserve(cfg, "auto", approved=False))
            return mock.Mock(returncode=0, stderr="")
        with mock.patch("subprocess.run", side_effect=shutdown):
            self.assertEqual(self.bot.reboot(), "")
        self.assertEqual(seen, [("busy", {})])
        st = self.st()
        self.assertEqual(st.applying, {})
        self.assertEqual(st.reboot_planning, {})
        self.assertTrue(st.reboot_requested_at)

    def test_failed_reboot_releases_only_its_planning(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=1, stderr="kaputt")):
            self.assertIn("Neustart fehlgeschlagen: kaputt", self.bot.reboot())
        st = self.st()
        self.assertEqual(st.reboot_planning, {})
        self.assertFalse(st.reboot_requested_at)
        self.assertEqual(st.outbox, [])

    def test_unknown_logind_refuses_consequential_actions_only(self):
        self.offer("postfix")
        self.logind.return_value = busy.Shutdown("unknown")
        with mock.patch.object(restarts, "start") as start:
            self.press("restart:postfix")
        start.assert_not_called()
        self.assertIn("logind gibt keine Auskunft", self.tg.answered[-1])
        with mock.patch("subprocess.run") as run:
            self.bot.handle(message("/reboot@server1_bot"))
        run.assert_not_called()
        # Prüfen ändert nichts am System; das Einspielen fragt logind selbst noch einmal
        self.bot.handle(message("/check@server1_bot"))
        self.start_check.assert_called_once()

    # --- Nach dem Boot -----------------------------------------------------

    def boot(self, **fields):
        """Bot startet nach einem Boot: eine andere Boot-ID als beim letzten Start."""
        with state.locked(self.bot.cfg.state_dir) as st:
            st.boot_id = "vorher"
            for name, value in fields.items():
                setattr(st, name, value)
        with mock.patch.object(self.bot, "_set_menu"):
            self.bot.startup()

    def test_boot_closes_reboot_and_services_while_zypper_locked(self):
        # Nach dem Boot sperrte purge-kernels zypper minutenlang; der Status zeigte weiter „Neustart nötig“
        self.offer("postfix")
        self.open_reboot()
        self.rebooting.return_value = self.found.return_value = None
        self.boot()
        st = self.st()
        self.assertEqual(st.issues, {})
        self.assertEqual(st.restart_services, [])
        self.assertEqual(st.unverified, ["reboot", "services"])
        self.assertEqual(st.boot_id, state.boot_id())
        # Ohne Auskunft von zypper keine Entwarnung
        text, buttons = self.shown()
        self.assertTrue(text.startswith("❔"), text)
        self.assertIn("noch nicht abgefragt: Neustartbedarf, Dienste mit alten Dateien", text)
        self.assertNotIn("reboot", actions(buttons))

    def test_unverified_clears_once_zypper_answers(self):
        self.rebooting.return_value = self.found.return_value = None
        self.boot()
        self.rebooting.return_value, self.found.return_value = False, []
        self.bot.check_reboot()
        self.bot.pump()
        self.assertTrue(self.shown()[0].startswith("✅"), self.shown()[0])
        self.assertEqual(self.st().unverified, [])

    def test_lock_freed_after_boot_asks_zypper_at_once(self):
        self.holder.return_value = "purge-kernels.service (zypper, PID 707)"
        self.boot(reboot_requested_at=time.time() - 300)
        self.assertIn("🔒 Paketverwaltung belegt: purge-kernels.service", self.shown()[0])
        self.holder.return_value = ""
        with mock.patch.object(self.bot, "check_reboot") as check_reboot, \
                mock.patch.object(self.bot, "_rpmdb_mtime", return_value=0.0):
            self.bot.last_reboot_check = time.time()
            self.bot.tick()
        check_reboot.assert_called_once()
        self.bot.pump()
        self.assertNotIn("🔒", self.shown()[0])

    def test_bot_restart_without_boot_keeps_items(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.boot_id = state.boot_id()
            st.open_issue("reboot", "Neustart nötig")
        with mock.patch.object(self.bot, "_set_menu"):
            self.bot.startup()
        st = self.st()
        self.assertIn("reboot", st.issues)
        self.assertEqual(st.unverified, [])

    def test_first_start_after_upgrade_is_no_boot(self):
        # Bis 0.9.4 stand keine Boot-ID im Zustand; ein Update ist kein Neustart des Servers
        self.open_reboot()
        with mock.patch.object(self.bot, "_set_menu"):
            self.bot.startup()
        st = self.st()
        self.assertIn("reboot", st.issues)
        self.assertEqual(st.boot_id, state.boot_id())

    def test_service_restart_interrupted_by_boot_is_reported(self):
        request = {"at": time.time() - 60, "services": ["postfix"]}
        self.boot(restart_request=request)
        event = next(t for t in self.tg.sent if "unterbrochen" in t)
        self.assertIn("postfix", event)
        self.assertFalse(self.tg.silent[self.tg.sent.index(event)])
        st = self.st()
        self.assertEqual(st.restart_request, {})
        self.assertEqual(st.outbox, [])

    def test_back_after_boot(self):
        self.boot(reboot_requested_at=time.time() - 300)
        self.assertIn("ist wieder da", self.tg.sent[0])
        self.assertIsNone(self.tg.buttons[0])
        self.assertFalse(self.st().reboot_requested_at)

    def test_back_after_boot_survives_telegram_outage(self):
        self.tg.fail_send = True
        self.boot(reboot_requested_at=time.time() - 300)
        st = self.st()
        self.assertEqual(len(st.outbox), 1)
        self.assertFalse(st.reboot_requested_at)
        self.tg.fail_send = False
        self.bot.pump()
        self.assertIn("ist wieder da", self.tg.sent[0])
        self.assertEqual(self.st().outbox, [])

    # --- Übergang von 0.9 -------------------------------------------------

    def test_old_topics_removed_once_state_message_stands(self):
        now = time.time()
        with state.locked(self.bot.cfg.state_dir) as st:
            st.messages = {"status": {"chat_id": -100, "message_id": 40, "at": now},
                           "briefing": {"chat_id": -100, "message_id": 41, "at": now},
                           "issue:reboot": {"chat_id": -100, "message_id": 42, "at": now - 50 * 3600},
                           "keyboard": {"chat_id": -100, "message_id": 43, "at": now}}
            st.restart_request = {"chat_id": -100, "message_id": 44, "at": now, "services": ["postfix"]}
            st.update_request = {"commit": "abc1234" + "0" * 33, "chat_id": -100, "message_id": 45, "at": now}
        self.tg.fail_send = True
        with mock.patch.object(self.bot, "_set_menu"), mock.patch.object(ota, "COMMIT", "abc1234"):
            self.bot.startup()
        st_after_start = self.st()
        self.assertEqual(self.tg.deleted, [])  # erst die neue Nachricht, dann abräumen
        self.assertIn("status", self.st().messages)
        self.tg.fail_send = False
        self.bot.pump()
        self.bot.migrate_legacy()
        self.assertEqual(sorted(self.tg.deleted), [40, 41, 44, 45])
        self.assertIn("veraltet", self.tg.current[42][0])  # zu alt zum Löschen
        # Das Update ist beim Start abgeschlossen; seine Wartemeldung geht trotzdem, das Ergebnis kommt als Ereignis
        self.assertEqual(st_after_start.update_request, {})
        self.assertIn("twguard läuft jetzt mit", st_after_start.outbox[0]["text"])
        st = self.st()
        self.assertEqual(sorted(st.messages), ["keyboard", panel.TOPIC])
        # Der laufende Auftrag bleibt, nur seine Wartemeldung ging
        self.assertEqual(st.restart_request, {"at": now, "services": ["postfix"]})
        self.bot.migrate_legacy()
        self.assertEqual(sorted(self.tg.deleted), [40, 41, 44, 45])

    def test_old_state_fields_are_dropped(self):
        path = self.bot.cfg.state_dir / "state.json"
        path.write_text('{"reports": [{"at": 1, "packages": 3}], "collapsed": {"x": {}}, "maintenance": {"a": 1},'
                        ' "restart_reports": [{"text": "x", "loud": true}]}')
        st = state.load(path)
        self.assertEqual(st.outbox, [{"text": "x", "loud": True}])
        self.assertFalse(hasattr(st, "reports"))

    # --- Sprache und Modus -------------------------------------------------

    def test_language_switches_at_runtime_and_hints_at_config(self):
        self.addCleanup(set_language, "de")
        self.bot.handle(message("/language en"))
        self.assertEqual(current(), "en")
        answer = next(t for t in self.tg.sent if "config.toml" in t)
        self.assertIn('language = "en"', answer)
        self.assertIn("until the next restart", answer)
        self.assertIn(("status", "Open issues (all servers)"), self.tg.commands)
        self.assertEqual(self.st().language_override, "en")
        self.assertIn("nothing open", self.shown()[0])

    def test_language_back_to_config_clears_override(self):
        self.addCleanup(set_language, "de")
        self.bot.handle(message("/language en"))
        self.bot.handle(message("/language de"))
        self.assertTrue(any("wie in der Konfiguration" in t for t in self.tg.sent))
        self.assertEqual(self.st().language_override, "")

    def test_language_rejects_unknown_and_shows_current(self):
        self.bot.handle(message("/language fr"))
        self.assertIn("Format: /language", self.tg.sent[-1])
        self.assertEqual(current(), "de")
        self.bot.handle(message("/language"))
        self.assertIn("Sprache de, in der Konfiguration de", self.tg.sent[-1])

    def test_restart_returns_to_configured_language(self):
        self.addCleanup(set_language, "de")
        self.bot.handle(message("/language en"))
        restarted = Bot(self.bot.cfg, self.tg, self.bot.me)
        self.assertEqual(current(), "de")
        restarted.startup()
        self.assertEqual(self.st().language_override, "")

    def test_language_renews_keyboard(self):
        self.addCleanup(set_language, "de")
        self.bot.handle(message("/keyboard@server1_bot"))
        old = self.tg.next_id
        self.bot.handle(message("/language en"))
        self.assertIn(old, self.tg.deleted)
        self.assertTrue(any("Keyboard for all servers" in text for text in self.tg.sent))

    def test_mode_switches_at_runtime_and_hints_at_config(self):
        self.bot.handle(message("/mode@server1_bot auto"))
        self.assertEqual(self.bot.cfg.mode, "auto")
        answer = next(t for t in self.tg.sent if "mode = " in t)
        self.assertIn('mode = "auto"', answer)
        self.assertIn("bis zum nächsten Neustart des Bots", answer)
        self.assertIn("/check", answer)
        self.assertEqual(self.st().mode_override, "auto")
        self.assertIn("Modus <code>auto</code> bis zum nächsten Neustart", self.shown()[0])
        self.assertIn("automatisch (bis zum Neustart)", self.bot.version_line())

    def test_mode_only_when_addressed(self):
        self.bot.handle(message("/mode auto"))
        self.assertEqual(self.bot.cfg.mode, "observe")
        self.assertEqual(self.tg.sent, [])  # nur der Bot mit help_full gibt den Hinweis
        self.bot.cfg.help_full = True
        self.bot.handle(message("/mode auto"))
        self.assertEqual(self.bot.cfg.mode, "observe")
        self.assertIn("nur gezielt", self.tg.sent[-1])
        self.bot.handle(message("/mode auto", reply_to=ME))
        self.assertEqual(self.bot.cfg.mode, "auto")

    def test_mode_back_to_config_and_restart_clear_override(self):
        self.bot.handle(message("/mode@server1_bot auto"))
        self.bot.handle(message("/mode@server1_bot observe"))
        self.assertTrue(any("wie in der Konfiguration" in t for t in self.tg.sent))
        self.assertEqual(self.st().mode_override, "")
        self.bot.handle(message("/mode@server1_bot auto"))
        restarted = Bot(Config(language="de", token="x", chat_id=-100, allowed_user_ids=[42], hostname="server1",
                               state_dir=self.bot.cfg.state_dir), self.tg, self.bot.me)
        restarted.startup()
        self.assertEqual(restarted.cfg.mode, "observe")
        self.assertEqual(self.st().mode_override, "")

    def test_mode_rejects_unknown_and_shows_current(self):
        self.bot.handle(message("/mode@server1_bot fast"))
        self.assertIn("Format: /mode", self.tg.sent[-1])
        self.bot.handle(message("/mode@server1_bot"))
        self.assertIn("Modus <code>observe</code>, in der Konfiguration <code>observe</code>", self.tg.sent[-1])

    def test_rpmdb_change_rechecks_pending(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.pending_text = "24 Pakete"
            st.rpmdb_mtime = 1.0
        with mock.patch.object(self.bot, "_rpmdb_mtime", return_value=2.0), \
                mock.patch.object(self.bot, "check_reboot"), mock.patch.object(self.bot, "start_check") as start:
            self.bot.tick()
        start.assert_called_once()

    def test_rpmdb_mtime_also_from_ndb(self):
        # Frische Tumbleweed-Installationen haben nur Packages.db (ndb), kein rpmdb.sqlite
        with tempfile.TemporaryDirectory() as tmp:
            ndb = Path(tmp, "Packages.db")
            ndb.write_text("")
            os.utime(ndb, (5.0, 5.0))
            with mock.patch("twguard.bot.RPMDB_PATHS", (Path(tmp, "rpmdb.sqlite"), ndb)):
                self.assertEqual(Bot._rpmdb_mtime(), 5.0)
        self.assertIn(Path("/usr/lib/sysimage/rpm/Packages.db"), bot_module.RPMDB_PATHS)

    # --- Umzug zur Supergruppe ---------------------------------------------

    def test_supergroup_migration_from_service_message(self):
        self.bot.pump()
        # Erste Nachricht in der neuen Supergruppe nennt die alte ID
        self.bot.handle({"message_id": 1, "date": int(time.time()), "migrate_from_chat_id": -100,
                         "chat": {"id": -1009, "type": "supergroup"}})
        self.assertEqual(self.bot.cfg.chat_id, -1009)
        self.assertIn("Supergruppe", self.tg.sent[-1])
        st = self.st()
        self.assertEqual(st.chat_migration["to"], -1009)
        self.assertNotIn(panel.TOPIC, st.messages)  # alte Gruppe: nicht mehr bearbeitbar
        # Befehle aus der neuen Gruppe werden angenommen, der Hinweis kommt nur einmal
        self.bot.handle(message("/status", chat={"id": -1009, "type": "supergroup"}))
        self.bot.handle({"message_id": 2, "date": int(time.time()), "migrate_to_chat_id": -1009,
                         "chat": {"id": -100, "type": "group"}})
        self.assertEqual(sum("Supergruppe" in t for t in self.tg.sent), 1)
        self.assertIn("server1", self.tg.sent[-1])

    def test_migration_noticed_while_sending(self):
        self.tg.migration = (-100, -1009)
        self.bot.sync_migration()
        self.assertEqual(self.bot.cfg.chat_id, -1009)
        self.assertIsNone(self.tg.migration)

    def test_migration_for_other_chat_ignored(self):
        self.bot.handle({"message_id": 1, "date": int(time.time()), "migrate_from_chat_id": -555,
                         "chat": {"id": -1009, "type": "supergroup"}})
        self.assertEqual(self.bot.cfg.chat_id, -100)
        self.assertEqual(self.tg.sent, [])

    def test_unknown_supergroup_gets_hint_in_log(self):
        with self.assertLogs("twguard.bot", "WARNING") as logs:
            self.bot.handle(message("/status", chat={"id": -1009, "type": "supergroup"}))
        self.assertTrue(any("chat_id = -1009" in line for line in logs.output))


class StateTest(unittest.TestCase):
    def test_roundtrip_keeps_since(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            with state.locked(d) as st:
                st.open_issue("manual", "a", now=100.0, fingerprint="x")
            with state.locked(d) as st:
                issue, new = st.open_issue("manual", "b", now=200.0, fingerprint="y")
                self.assertFalse(new)
            with state.locked(d) as st:
                self.assertEqual(st.issues["manual"].since, 100.0)
                self.assertEqual(st.issues["manual"].fingerprint, "y")


if __name__ == "__main__":
    unittest.main()
