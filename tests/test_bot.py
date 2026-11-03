import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import testlang  # noqa: F401
from twguard import bot as bot_module, check, ota, restarts, state, zypper
from twguard.bot import Bot, parse_duration
from twguard.config import Config
from twguard.i18n import current, set_language
from twguard.notify import send_due
from twguard.telegram import TelegramError, keyboard

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


class BotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Config(language="de", token="x", chat_id=-100, allowed_user_ids=[42], hostname="server1",
                     state_dir=Path(self.tmp.name))
        self.tg = FakeTelegram()
        self.bot = Bot(cfg, tg=self.tg, me=ME)
        self.start_check = mock.patch.object(Bot, "start_check").start()
        mock.patch.object(Bot, "_check_running", return_value=False).start()
        self.found = mock.patch.object(zypper, "services_needing_restart", return_value=[]).start()
        # Kein echtes zypper in den Tests (fehlt z. B. in der CI). None heißt „unbekannt“ und lässt einen
        # von Hand geöffneten Neustart-Punkt stehen; einzelne Tests ersetzen den Wert
        self.rebooting = mock.patch.object(zypper, "needs_rebooting", return_value=None).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.tmp.cleanup)

    def open_manual(self, approvable=True):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.open_issue("manual", "• kritisches Paket", fingerprint="abc", approvable=approvable)
            st.manual_shown = "abc"

    def test_status_broadcast_answered(self):
        self.bot.handle(message("/status"))
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("server1", self.tg.sent[0])

    def test_approve_needs_address(self):
        self.open_manual()
        self.bot.handle(message("/approve"))
        self.assertEqual(self.tg.sent, [])
        self.start_check.assert_not_called()

    def test_approve_by_reply(self):
        self.open_manual()
        self.bot.handle(message("/approve", reply_to=ME))
        self.start_check.assert_called_once()
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
        self.assertIn("72 Stunden", self.tg.sent[0])  # auf max_snooze_hours begrenzt
        self.assertIn("Nicht weiter aufschiebbar", self.tg.sent[2])

    def press(self, data, user=USER, chat=GROUP):
        self.bot.handle_callback({"id": "cb1", "from": user, "data": data,
                                  "message": {"message_id": 5, "chat": chat, "date": int(time.time()),
                                              "text": "Meldung", "entities": [], "reply_markup": {}}})

    def test_button_approve_with_matching_fingerprint(self):
        self.open_manual()
        self.press("approve:abc")
        self.start_check.assert_called_once()
        # Die Meldung selbst wird zur Wartemeldung, es kommt keine neue Nachricht
        self.assertEqual(self.tg.sent, [])
        self.assertEqual(self.tg.edited[0][0], 5)
        self.assertIn("Freigegeben", self.tg.edited[0][1])
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.approved_fingerprint, "abc")
            self.assertEqual(st.check_request["message_id"], 5)

    def test_button_approve_stale_fingerprint(self):
        self.open_manual()
        self.press("approve:old")
        self.start_check.assert_not_called()
        self.assertIn("älteren Stand", self.tg.answered[0])
        self.assertEqual(self.tg.edited, [])

    def test_button_approve_observed_routine(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.observed_fingerprint = "rtn"
        self.press("approve:rtn")
        self.start_check.assert_called_once()

    def test_button_unauthorized(self):
        self.open_manual()
        self.press("approve:abc", user={"id": 99, "is_bot": False})
        self.start_check.assert_not_called()
        self.assertEqual(self.tg.answered, ["Nicht berechtigt."])
        self.assertEqual(self.tg.sent, [])

    def test_button_details_toggles_in_place(self):
        self.press("details")
        self.assertEqual(self.tg.sent, [])
        message_id, text, buttons = self.tg.edited[0]
        self.assertEqual(message_id, 5)
        self.assertIn("Noch kein Probelauf", text)
        self.assertEqual(buttons, [[("◀️ Zurück", "back")]])
        self.press("back")
        self.assertEqual(self.tg.raw_edits, [(5, "Meldung")])

    def test_double_back_leaves_restored_message(self):
        self.press("details")
        self.press("back")
        edits = len(self.tg.edited)
        self.press("back")  # zweiter Druck aus der alten Ansicht
        self.assertEqual(len(self.tg.edited), edits)
        self.assertEqual(self.tg.raw_edits, [(5, "Meldung")])
        self.assertEqual(self.tg.answered[-1], "Nicht mehr verfügbar.")

    def set_plan(self, count=1):
        rows = [["to-upgrade", f"paket-{i:03}", "1.0-1", "1.0-2", "repo"] for i in range(count)]
        with state.locked(self.bot.cfg.state_dir) as st:
            st.last_plan = {"summary": f"{count} Pakete", "rows": rows, "problems": []}

    def test_details_switch_view_keeps_back(self):
        self.set_plan()
        self.press("details")
        _, text, buttons = self.tg.edited[-1]
        self.assertIn("paket-000\n", text)
        self.assertEqual(buttons, [[("🔢 Nach Version", "details:version:1"), ("📄 Als Datei", "file")],
                                   [("◀️ Zurück", "back")]])
        self.press("details:version:1")
        _, text, buttons = self.tg.edited[-1]
        self.assertIn("nur neu gebaut", text)
        self.assertEqual(buttons[0][0], ("🔤 Nach Name", "details:name:1"))
        # Umschalten darf den gemerkten Inhalt nicht überschreiben
        self.press("back")
        self.assertEqual(self.tg.raw_edits, [(5, "Meldung")])

    def test_details_pages(self):
        self.set_plan(300)
        self.press("details")
        nav = self.tg.edited[-1][2][1]
        self.assertEqual(nav[-1], ("➡️", "details:name:2"))
        self.assertEqual(len(nav), 2)
        self.press("details:name:2")
        nav = self.tg.edited[-1][2][1]
        self.assertEqual(nav[0], ("⬅️", "details:name:1"))
        # Der Plan kann inzwischen kürzer sein
        self.press("details:name:99")
        self.assertIn("paket-", self.tg.edited[-1][1])

    def test_details_command_has_no_back(self):
        self.set_plan()
        self.bot.handle(message("/details@" + self.bot.me["username"]))
        self.assertEqual(self.tg.buttons[-1], [[("🔢 Nach Version", "details:version:1"), ("📄 Als Datei", "file")]])

    def test_file_button_sends_document_and_replaces_previous(self):
        self.set_plan()
        self.press("file")
        name, content, caption = self.tg.documents[0]
        self.assertTrue(name.endswith(".html"))
        self.assertIn(b"paket-000", content)
        self.assertIn("1 Pakete", caption)
        with state.locked(self.bot.cfg.state_dir) as st:
            first = st.messages["file"]["message_id"]
        self.press("file")
        self.assertEqual(len(self.tg.documents), 2)
        self.assertIn(first, self.tg.deleted)

    def test_file_button_without_plan(self):
        self.press("file")
        self.assertEqual(self.tg.documents, [])
        self.assertEqual(self.tg.answered, ["Keine Paketliste vorhanden."])

    def test_status_has_buttons(self):
        self.bot.handle(message("/status"))
        self.assertIn(("🔄 Jetzt prüfen", "check"), self.tg.buttons[0][0])
        self.assertIn("Beobachtungsmodus", self.tg.sent[0])

    def open_reboot(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.open_issue("reboot", "Neustart nötig")

    def status_data(self, buttons):
        return [b[1] for row in buttons for b in row]

    def test_status_offers_buttons_of_open_items(self):
        self.open_reboot()
        self.bot.handle(message("/status"))
        data = self.status_data(self.tg.buttons[0])
        self.assertIn("reboot", data)
        self.assertNotIn("details", data)  # ohne Paketliste gibt es nichts zu zeigen
        self.assertEqual(data.count("check"), 1)

    def test_status_shows_details_only_with_packages(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.last_plan = {"summary": "1 Paket", "rows": [["to-upgrade", "bash", "5.3.2-1.1", "5.3.3-1.1", "repo"]],
                            "problems": []}
        self.assertIn("details", self.status_data(self.bot.status_view()[1]))

    def test_status_asks_zypper_for_pending_reboot(self):
        # Nicht erst beim nächsten Takt (alle 30 min): Der Status zeigt den Neustart gleich mit Knopf
        self.rebooting.return_value = True
        self.bot.handle(message("/status"))
        self.assertIn("reboot", self.status_data(self.tg.buttons[0]))
        with state.locked(self.bot.cfg.state_dir) as st:
            issue = st.issues["reboot"]
            # Gilt als erste Benachrichtigung, der Takt schickt danach keine zweite Meldung
            self.assertTrue(issue.last_notified)
            self.assertEqual(issue.notify_count, 1)
            send_due(self.bot.cfg, st, self.tg)
        self.assertEqual(len(self.tg.sent), 1)

    def test_status_closes_reboot_no_longer_needed(self):
        self.open_reboot()
        self.rebooting.return_value = False
        self.bot.handle(message("/status"))
        self.assertNotIn("reboot", self.status_data(self.tg.buttons[-1]))
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertNotIn("reboot", st.issues)

    def test_start_check_asks_zypper_for_pending_reboot(self):
        # Das Ergebnis eines Prüflaufs ohne Updates zeigt offene Punkte; der Neustart soll dabei sein
        self.rebooting.return_value = True
        with mock.patch.object(subprocess, "run") as run:
            REAL_START_CHECK(self.bot)
        run.assert_called_once()
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertIn("reboot", st.issues)
            self.assertFalse(st.issues["reboot"].last_notified)

    def status_message(self):
        self.bot.handle(message("/status"))
        with state.locked(self.bot.cfg.state_dir) as st:
            return st.messages["status"]["message_id"]

    def press_on(self, message_id, data, buttons):
        self.bot.handle_callback({"id": "cb1", "from": USER, "data": data,
                                  "message": {"message_id": message_id, "chat": GROUP, "date": int(time.time()),
                                              "text": "Status", "entities": [], "reply_markup": keyboard(buttons)}})

    def test_reboot_from_status_keeps_other_items(self):
        self.open_reboot()
        self.open_manual()
        status = self.status_message()
        with mock.patch.object(Bot, "reboot", return_value=""):
            self.press_on(status, "reboot", self.tg.buttons[0])
        self.assertIn("startet in 1 Minute neu", self.tg.sent[-1])
        (message_id, markup), = self.tg.markup_edits
        self.assertEqual(message_id, status)
        data = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
        self.assertNotIn("reboot", data)
        self.assertIn("approve:abc", data)

    def test_back_on_status_rebuilds_status(self):
        # Auch ohne gespeicherten Inhalt: Der Status wird neu aufgebaut, mit seinen Schaltflächen
        self.open_reboot()
        status = self.status_message()
        self.press_on(status, "back", [[("◀️ Zurück", "back")]])
        message_id, text, buttons = self.tg.edited[-1]
        self.assertEqual(message_id, status)
        self.assertIn("Neustart", text)
        self.assertIn("reboot", self.status_data(buttons))

    def test_update_offer_and_go(self):
        pending = ota.Pending("a" * 40, "b" * 40, ["bbbbbbb Neue Funktion"])
        with mock.patch.object(ota, "check_remote", return_value=pending), \
             mock.patch.object(ota, "start", return_value="") as start:
            self.bot.handle(message("/update"))  # ohne @botname: alle Server antworten
            self.assertIn("Neue Funktion", self.tg.sent[0])
            self.assertEqual(self.tg.buttons[0][0][0], ("⬆️ Einspielen", "update:" + "b" * 40))
            self.press("update:" + "b" * 40)
            start.assert_called_once_with("b" * 40)
        self.assertIn("Update auf", self.tg.edited[-1][1])
        # Die Frist bis zum Zurückrollen steht dabei, sonst wirkt der stumme Bot tot
        self.assertIn(f"{ota.START_TIMEOUT} s", self.tg.edited[-1][1])
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.update_request["commit"], "b" * 40)
            self.assertEqual(st.update_request["message_id"], 5)

    def test_update_refuses_changed_target(self):
        pending = ota.Pending("a" * 40, "c" * 40, ["ccccccc Noch neuer"])
        with mock.patch.object(ota, "check_remote", return_value=pending), \
             mock.patch.object(ota, "start") as start:
            self.press("update:" + "b" * 40)
            start.assert_not_called()
        self.assertIn("anderen Stand", self.tg.answered[0])

    def test_check_sends_waiting_message_and_registers_request(self):
        self.bot.handle(message("/check@server1_bot"))
        self.start_check.assert_called_once()
        self.assertEqual(self.tg.typed, [-100])
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("Prüfung läuft", self.tg.sent[0])
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.check_request["message_id"], 101)

    def test_approve_registers_request(self):
        self.open_manual()
        self.bot.handle(message("/approve", reply_to=ME))
        self.assertIn("Freigegeben", self.tg.sent[0])
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertTrue(st.check_request)

    def at(self, hour, minute=0):
        t = time.localtime()
        return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, hour, minute, 0, 0, 0, -1))

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
        self.bot.maybe_briefing(self.at(10, 0))
        self.assertIn("1 offener Punkt", self.tg.sent[0])
        self.assertFalse(self.tg.silent[0])

    def test_briefing_not_caught_up_too_late(self):
        self.bot.maybe_briefing(self.at(15, 0))
        self.assertEqual(self.tg.sent, [])

    def test_briefing_off(self):
        self.bot.cfg.briefing.at = ""
        self.bot.maybe_briefing(self.at(8, 0))
        self.assertEqual(self.tg.sent, [])

    def overdue_manual(self, now):
        """Freigabe offen seit 30 h, letzte Erinnerung vor 25 h: Die nächste ist fällig."""
        self.open_manual()
        with state.locked(self.bot.cfg.state_dir) as st:
            issue = st.issues["manual"]
            issue.since, issue.last_notified, issue.notify_count = now - 30 * 3600, now - 25 * 3600, 1
            st.messages["issue:manual"] = {"chat_id": -100, "message_id": 30, "at": now - 25 * 3600}

    def test_briefing_takes_due_reminder(self):
        now = self.at(8, 0)
        self.overdue_manual(now)
        self.bot.maybe_briefing(now)
        with state.locked(self.bot.cfg.state_dir) as st:
            send_due(self.bot.cfg, st, self.tg, now)
            issue = st.issues["manual"]
            self.assertEqual((issue.last_notified, issue.notify_count), (now, 2))
            self.assertNotIn("issue:manual", st.messages)
        self.assertEqual(len(self.tg.sent), 1)  # keine Erinnerung als zweite Nachricht
        self.assertIn("🔔", self.tg.sent[0])
        self.assertIn("kritisches Paket", self.tg.sent[0])
        self.assertFalse(self.tg.silent[0])
        callbacks = [b[1] for row in self.tg.buttons[0] for b in row]
        self.assertIn("approve:abc", callbacks)
        self.assertIn("status", callbacks)
        self.assertIn(30, self.tg.deleted)  # die vorige Erinnerung wird ersetzt

    def test_briefing_without_due_reminder_only_lists(self):
        now = self.at(8, 0)
        self.overdue_manual(now)
        with state.locked(self.bot.cfg.state_dir) as st:
            st.issues["manual"].last_notified = now - 3600
        self.bot.maybe_briefing(now)
        self.assertNotIn("🔔", self.tg.sent[0])
        self.assertNotIn("approve:abc", [b[1] for row in self.tg.buttons[0] for b in row])
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.issues["manual"].notify_count, 1)
            self.assertIn("issue:manual", st.messages)

    def test_reminder_inside_quiet_hours_stays_separate(self):
        # Briefing absichtlich in der Ruhezeit: Die Erinnerung kommt wie eingestellt danach
        self.bot.cfg.briefing.at = "07:00"
        now = self.at(7, 0)
        self.overdue_manual(now)
        self.bot.maybe_briefing(now)
        self.assertNotIn("🔔", self.tg.sent[0])
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.issues["manual"].notify_count, 1)

    def briefing_with(self, kind, now):
        """Briefing mit zwei fälligen Punkten: Freigabe und `kind`."""
        self.overdue_manual(now)
        if kind == "services":
            self.offer("postfix")
        with state.locked(self.bot.cfg.state_dir) as st:
            if kind != "services":
                st.open_issue(kind, "Fehler beim Einspielen")
            issue = st.issues[kind]
            issue.since, issue.last_notified, issue.notify_count = now - 30 * 3600, now - 25 * 3600, 1
        self.bot.maybe_briefing(now)
        self.assertIn("approve:abc", self.briefing_data())
        with state.locked(self.bot.cfg.state_dir) as st:
            return st.messages["briefing"]["message_id"]

    def briefing_data(self, markup=None):
        rows = markup["inline_keyboard"] if markup else keyboard(self.tg.buttons[0])["inline_keyboard"]
        return [b["callback_data"] for row in rows for b in row]

    def press_briefing(self, briefing, data):
        self.bot.handle_callback({"id": "cb1", "from": USER, "data": data,
                                  "message": {"message_id": briefing, "chat": GROUP, "date": int(time.time()),
                                              "text": "Briefing", "entities": [],
                                              "reply_markup": keyboard(self.tg.buttons[0])}})

    def assert_briefing_kept(self, briefing, gone, kept):
        """Die Aktion kam in eine eigene Nachricht; im Briefing fehlen nur die Schaltflächen ihres Punkts."""
        self.assertNotIn(briefing, [m for m, *_ in self.tg.edited])
        (message_id, markup), = self.tg.markup_edits
        self.assertEqual(message_id, briefing)
        data = self.briefing_data(markup)
        self.assertFalse([d for d in data if d.startswith(gone)])
        for d in [*kept, "status", "check"]:
            self.assertIn(d, data)
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.messages["briefing"]["message_id"], briefing)

    def test_approve_in_briefing_keeps_restart_buttons(self):
        briefing = self.briefing_with("services", self.at(8, 0))
        self.press_briefing(briefing, "approve:abc")
        self.start_check.assert_called_once()
        self.assertIn("Freigegeben", self.tg.sent[-1])
        self.assert_briefing_kept(briefing, "approve", ["restart:postfix"])
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.check_request["message_id"], self.tg.next_id)

    def assert_briefing_untouched(self, briefing):
        self.assertNotIn(briefing, [m for m, *_ in self.tg.edited])
        self.assertEqual(self.tg.markup_edits, [])
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.messages["briefing"]["message_id"], briefing)

    def test_approve_in_briefing_with_send_error_keeps_briefing(self):
        briefing = self.briefing_with("services", self.at(8, 0))
        self.tg.fail_send = True
        self.press_briefing(briefing, "approve:abc")
        self.start_check.assert_called_once()
        self.assert_briefing_untouched(briefing)
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertNotIn("message_id", st.check_request)
            self.assertIn("services", st.issues)
        # Das Ergebnis des Prüflaufs kommt neu, sobald Telegram wieder annimmt
        self.tg.fail_send = False
        self.assertTrue(check.finish(self.bot.cfg, self.tg, "Ergebnis"))
        self.assertEqual(self.tg.sent[-1], "Ergebnis")
        self.assert_briefing_untouched(briefing)

    def test_restart_in_briefing_with_send_error_reports_later(self):
        briefing = self.briefing_with("services", self.at(8, 0))
        self.tg.fail_send = True
        with mock.patch.object(restarts, "start", return_value={}):
            self.press_briefing(briefing, "restart:postfix")
        self.assert_briefing_untouched(briefing)
        self.tg.fail_send = False
        self.found.return_value = []
        with mock.patch.object(restarts, "pending", return_value=[]), \
                mock.patch.object(restarts, "states", return_value={"postfix": "active"}):
            self.bot.watch_restart()
        self.assertIn("postfix", self.tg.sent[-1])
        self.assert_briefing_untouched(briefing)
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.restart_reports, [])

    def test_restart_in_briefing_keeps_approval(self):
        briefing = self.briefing_with("services", self.at(8, 0))
        with mock.patch.object(restarts, "start", return_value={}) as start:
            self.press_briefing(briefing, "restart:postfix")
        start.assert_called_once_with(["postfix"])
        self.assertIn("starte neu: postfix", self.tg.sent[-1])
        self.assert_briefing_kept(briefing, "restart", ["approve:abc", "later:24:manual"])
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.restart_request["message_id"], self.tg.next_id)

    def test_later_in_briefing_moves_issue_to_own_message(self):
        briefing = self.briefing_with("services", self.at(8, 0))
        self.press_briefing(briefing, "later:24:services")
        # Die Dienste bleiben in der eigenen Nachricht bedienbar, die Freigabe im Briefing
        self.assertIn("Aufgeschoben bis", self.tg.sent[-1])
        self.assertIn("restart:postfix", [b[1] for row in self.tg.buttons[-1] for b in row])
        self.assert_briefing_kept(briefing, "later:24:services", ["approve:abc"])
        self.assertNotIn("restart:postfix", self.briefing_data(self.tg.markup_edits[0][1]))
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.messages["issue:services"]["message_id"], self.tg.next_id)

    def test_resolved_in_briefing_keeps_approval(self):
        briefing = self.briefing_with("apply-failed", self.at(8, 0))
        self.press_briefing(briefing, "resolved:apply-failed")
        self.assertIn("erledigt", self.tg.sent[-1])
        self.assert_briefing_kept(briefing, "resolved", ["approve:abc"])
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertNotIn("apply-failed", st.issues)

    def test_reboot_in_briefing_keeps_approval(self):
        briefing = self.briefing_with("reboot", self.at(8, 0))
        with mock.patch.object(Bot, "reboot", return_value=""):
            self.press_briefing(briefing, "reboot")
        self.assertIn("startet in 1 Minute neu", self.tg.sent[-1])
        self.assert_briefing_kept(briefing, "reboot", ["approve:abc"])
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.messages["reboot"]["message_id"], self.tg.next_id)

    def test_tick_sends_briefing_before_reminders(self):
        now = self.at(8, 0)
        self.overdue_manual(now)
        with mock.patch.object(bot_module.time, "time", return_value=now), \
                mock.patch.object(self.bot, "_rpmdb_mtime", return_value=0.0), \
                mock.patch.object(self.bot, "check_reboot"), mock.patch.object(self.bot, "deliver_restart_reports"):
            self.bot.tick()
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("☀️", self.tg.sent[0])
        self.assertIn("🔔", self.tg.sent[0])

    def test_command_deleted_after_answer(self):
        self.bot.handle(message("/status"))
        self.assertEqual(self.tg.deleted, [7])

    def test_missing_delete_right_is_tolerated(self):
        self.tg.fail_delete = True
        self.bot.handle(message("/status"))
        self.bot.handle(message("/status"))
        self.assertEqual(len(self.tg.sent), 2)
        self.assertTrue(self.bot.delete_warned)

    def test_status_replaces_previous_status(self):
        self.bot.handle(message("/status"))
        self.bot.handle(message("/status"))
        self.assertIn(101, self.tg.deleted)
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.messages["status"]["message_id"], 102)

    def test_keyboard_text_is_broadcast_status(self):
        self.bot.handle(message("📊 Status"))
        self.assertIn("server1", self.tg.sent[0])
        self.assertEqual(self.tg.deleted, [7])

    def test_keyboard_key_is_broadcast_even_as_reply_to_other_bot(self):
        # Telegram schickt Tastendrücke als Antwort auf die Tastatur-Nachricht eines Bots
        self.bot.handle(message("📊 Status", reply_to=OTHER_BOT))
        self.assertIn("server1", self.tg.sent[0])
        self.bot.handle(message("❓ Hilfe", reply_to=ME))
        self.assertEqual(len(self.tg.sent), 1)  # an alle, nicht gezielt: ohne help_full keine Hilfe

    def test_keyboard_check_all(self):
        self.bot.handle(message("🔄 Alle prüfen"))
        self.start_check.assert_called_once()
        self.assertIn("Prüfung läuft", self.tg.sent[0])

    def test_keyboard_update_offers_update(self):
        pending = ota.Pending("a" * 40, "b" * 40, ["bbbbbbb Neue Funktion"])
        with mock.patch.object(ota, "check_remote", return_value=pending):
            self.bot.handle(message("⬆️ Update", reply_to=OTHER_BOT))
        self.assertIn("Neue Funktion", self.tg.sent[0])

    def test_keyboard_version(self):
        self.bot.handle(message("🏷️ Version"))
        self.assertIn("server1", self.tg.sent[0])

    def test_keyboard_has_two_rows(self):
        from twguard.bot import keyboard_markup
        self.assertEqual([len(row) for row in keyboard_markup()["keyboard"]], [2, 3])

    def test_keyboard_command_sends_persistent_keyboard(self):
        self.bot.handle(message("/keyboard@server1_bot"))
        self.assertTrue(self.tg.markups[0]["is_persistent"])
        # Erneut einblenden ersetzt die vorige Tastatur-Nachricht
        self.bot.handle(message("/keyboard@server1_bot"))
        self.assertIn(101, self.tg.deleted)
        # Ausblenden löscht nur die Tastatur-Nachricht (damit verschwinden die Tasten), ohne neue Nachricht
        self.bot.handle(message("/keyboard@server1_bot off"))
        self.assertIn(102, self.tg.deleted)
        self.assertEqual(len(self.tg.sent), 2)
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertNotIn("keyboard", st.messages)
            self.assertEqual(st.ephemeral, [])  # die Tastatur-Nachricht wird nie automatisch gelöscht

    def test_help_broadcast_silent_without_help_full(self):
        self.bot.handle(message("/help"))
        self.assertEqual(self.tg.sent, [])
        self.assertEqual(self.tg.deleted, [7])  # der Befehl verschwindet trotzdem

    def test_help_addressed_or_help_full_has_list(self):
        self.bot.handle(message("/help@server1_bot"))
        self.assertIn("/approve", self.tg.sent[0])
        self.bot.cfg.help_full = True
        self.bot.handle(message("/help"))
        self.assertIn("/approve", self.tg.sent[1])

    def offer(self, *names):
        with state.locked(self.bot.cfg.state_dir) as st:
            restarts.note(st, list(names), self.bot.cfg.services.exclude)
        self.found.return_value = list(names)

    def test_tick_offers_restart_with_buttons(self):
        self.found.return_value = ["postfix", "sshd"]
        with mock.patch.object(zypper, "needs_rebooting", return_value=False):
            self.bot.check_reboot()
            self.bot.tick()
        data = [b[1] for row in self.tg.buttons[0] for b in row]
        self.assertEqual(data[:3], ["restart:postfix", "restart:sshd", f"restart:*:{restarts.digest(['postfix', 'sshd'])}"])
        self.assertIn("later:24:services", data)

    def test_no_scan_while_check_runs(self):
        self.bot._check_running.return_value = True
        self.bot.check_services()
        self.found.assert_not_called()

    def test_button_restarts_one_service(self):
        self.offer("postfix", "sshd")
        with mock.patch.object(restarts, "start", return_value={}) as start:
            self.press("restart:sshd")
        start.assert_called_once_with(["sshd"])
        self.assertIn("sshd", self.tg.edited[-1][1])
        self.assertIsNone(self.tg.edited[-1][2])
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.restart_request["services"], ["sshd"])
            self.assertNotIn("services", st.issues)

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
        self.offer("mariadb", "postfix", "sshd")  # erkannt, aber z. B. in der Ruhezeit noch nicht gemeldet
        with mock.patch.object(restarts, "start") as start:
            self.press(self.all_button("postfix", "sshd"))
        start.assert_not_called()
        # Stattdessen zeigt die Nachricht jetzt den aktuellen Stand mit aktuellem Knopf
        data = [b[1] for row in self.tg.edited[-1][2] for b in row]
        self.assertIn(self.all_button("mariadb", "postfix", "sshd"), data)

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
        self.found.return_value = None
        with mock.patch.object(restarts, "start") as start:
            self.press("restart:sshd")
        start.assert_not_called()
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertIn("services", st.issues)

    def test_no_restart_during_check_or_update(self):
        self.offer("sshd")
        self.bot._check_running.return_value = True
        with mock.patch.object(restarts, "start") as start:
            self.press("restart:sshd")
            self.bot._check_running.return_value = False
            with state.locked(self.bot.cfg.state_dir) as st:
                st.update_request = {"commit": "x"}
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

    def test_gone_service_is_not_restarted_rest_offered_here(self):
        self.offer("postfix", "sshd")
        self.found.return_value = ["postfix"]
        with mock.patch.object(restarts, "start") as start:
            self.press("restart:sshd")
        start.assert_not_called()
        self.assertEqual([b[1] for b in self.tg.edited[-1][2][0]], ["restart:postfix"])
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.restart_services, ["postfix"])
            self.assertEqual(st.messages["issue:services"]["message_id"], 5)

    def test_second_press_while_running_refused(self):
        self.offer("sshd")
        with state.locked(self.bot.cfg.state_dir) as st:
            st.restart_request = {"services": ["postfix"], "at": time.time(), "chat_id": -100, "message_id": 1}
        with mock.patch.object(restarts, "start") as start:
            self.press("restart:sshd")
        start.assert_not_called()

    def test_partly_rejected_restart_is_still_followed(self):
        self.offer("postfix", "sshd")
        with mock.patch.object(restarts, "start", return_value={"sshd": "Unit <x> masked"}):
            self.press(self.all_button("postfix", "sshd"))
        self.found.return_value = ["sshd"]
        with mock.patch.object(restarts, "pending", return_value=[]) as pending, \
                mock.patch.object(restarts, "states", return_value={"postfix": "active"}):
            self.bot.watch_restart()
        pending.assert_called_once_with(["postfix"])
        self.assertIn("neu gestartet: postfix", self.tg.sent[0])
        self.assertIn("Unit &lt;x&gt; masked", self.tg.sent[0])
        self.assertFalse(self.tg.silent[0])
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.restart_services, ["sshd"])  # wird wieder angeboten

    def running(self, *names):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.restart_request = {"chat_id": -100, "message_id": 55, "at": time.time(), "services": list(names)}

    def test_result_waits_for_jobs(self):
        self.running("sshd")
        with mock.patch.object(restarts, "pending", return_value=["sshd"]):
            self.bot.watch_restart()
        self.assertEqual(self.tg.edited, [])

    def test_result_success_edits_message(self):
        self.running("sshd")
        self.found.return_value = []
        with mock.patch.object(restarts, "pending", return_value=[]), \
                mock.patch.object(restarts, "states", return_value={"sshd": "active"}):
            self.bot.watch_restart()
        self.assertEqual(self.tg.edited[-1][0], 55)
        self.assertIn("✅", self.tg.edited[-1][1])
        self.assertEqual(self.tg.sent, [])
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.restart_request, {})

    def test_result_failure_is_new_message_with_sound(self):
        self.running("sshd")
        with mock.patch.object(restarts, "pending", return_value=[]), \
                mock.patch.object(restarts, "states", return_value={"sshd": "failed"}):
            self.bot.watch_restart()
        self.assertIn("sshd (failed)", self.tg.sent[0])
        self.assertFalse(self.tg.silent[0])
        self.assertIn(55, self.tg.deleted)

    def test_result_still_old_is_offered_again(self):
        self.running("sshd")
        self.found.return_value = ["sshd"]
        with mock.patch.object(restarts, "pending", return_value=[]), \
                mock.patch.object(restarts, "states", return_value={"sshd": "active"}):
            self.bot.watch_restart()
        self.assertIn("alte Dateien", self.tg.edited[-1][1])
        self.assertIn("restart:sshd", [b[1] for row in self.tg.buttons[0] for b in row])

    def test_conflict_details_have_no_list_buttons(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.last_plan = {"summary": "0 Pakete", "rows": [], "problems": ["Problem: 1: x"]}
        text, buttons = self.bot.details_view()
        self.assertIn("Problem: 1: x", text)
        self.assertEqual(buttons, [])
        self.assertEqual(self.bot.details_view(back=True)[1], [[("◀️ Zurück", "back")]])

    def test_unknown_scan_after_restart_waits_then_no_green(self):
        self.running("sshd")
        self.found.return_value = None
        with mock.patch.object(restarts, "pending", return_value=[]), \
                mock.patch.object(restarts, "states", return_value={"sshd": "active"}):
            self.bot.watch_restart()
            self.assertEqual(self.tg.sent + self.tg.edited, [])
            with state.locked(self.bot.cfg.state_dir) as st:
                st.restart_request["at"] -= 1000
            self.bot.watch_restart()
        self.assertNotIn("✅", self.tg.sent[0])
        self.assertIn("keine Auskunft", self.tg.sent[0])

    def test_result_survives_telegram_outage(self):
        self.running("sshd")
        self.found.return_value = []
        send, edit = self.tg.send, self.tg.edit
        error = TelegramError("Netz weg")
        self.tg.send = self.tg.edit = mock.Mock(side_effect=error)
        with mock.patch.object(restarts, "pending", return_value=[]) as pending, \
                mock.patch.object(restarts, "states", return_value={"sshd": "failed"}):
            self.bot.watch_restart()
            with state.locked(self.bot.cfg.state_dir) as st:
                self.assertIn("sshd (failed)", st.restart_reports[0]["text"])
            self.assertEqual(self.tg.deleted, [])  # die Wartemeldung bleibt, solange nichts gemeldet ist
            self.bot.deliver_restart_reports()
            self.tg.send, self.tg.edit = send, edit
            self.bot.deliver_restart_reports()
        self.assertIn("sshd (failed)", self.tg.sent[0])
        self.assertIn(55, self.tg.deleted)
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.restart_reports, [])

    def test_second_result_during_outage_keeps_first(self):
        self.found.return_value = []
        send, edit = self.tg.send, self.tg.edit
        self.tg.send = self.tg.edit = mock.Mock(side_effect=TelegramError("Netz weg"))
        with mock.patch.object(restarts, "pending", return_value=[]):
            self.running("sshd")
            with mock.patch.object(restarts, "states", return_value={"sshd": "failed"}):
                self.bot.watch_restart()
            self.running("postfix")
            with mock.patch.object(restarts, "states", return_value={"postfix": "active"}):
                self.bot.watch_restart()
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(len(st.restart_reports), 2)
        self.tg.send, self.tg.edit = send, edit
        self.bot.deliver_restart_reports()
        self.assertIn("sshd (failed)", self.tg.sent[0])
        self.assertIn("neu gestartet: postfix", self.tg.edited[-1][1])
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.restart_reports, [])

    def test_one_undeliverable_result_does_not_hold_back_others(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.restart_reports = [{"chat_id": -100, "at": time.time(), "text": t, "loud": True} for t in ("a", "b")]
        send = self.tg.send
        self.tg.send = mock.Mock(side_effect=[TelegramError("x"), {"message_id": 9, "chat": {"id": -100}}])
        self.bot.deliver_restart_reports()
        self.tg.send = send
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual([r["text"] for r in st.restart_reports], ["a"])

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
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("nicht abgeschlossen", self.tg.sent[0])
        self.assertNotIn("✅", self.tg.sent[0])
        self.assertNotIn(55, self.tg.deleted)  # die Wartemeldung bleibt für das Ergebnis
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertTrue(st.restart_request)
        # Später endet der Auftrag: Auch ein spätes Scheitern wird gemeldet
        with mock.patch.object(restarts, "pending", return_value=[]), \
                mock.patch.object(restarts, "states", return_value={"sshd": "failed"}):
            self.bot.watch_restart()
        self.assertIn("sshd (failed)", self.tg.sent[1])
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.restart_request, {})

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
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.restart_request, {})

    def test_unknown_jobs_within_limit_wait(self):
        self.running("sshd")
        with mock.patch.object(restarts, "pending", return_value=None):
            self.bot.watch_restart()
        self.assertEqual(self.tg.sent + self.tg.edited, [])

    def test_check_running_understands_oneshot(self):
        mock.patch.stopall()
        results = {"activating": True, "active": True, "inactive": False, "failed": False}
        for output, expected in results.items():
            done = subprocess.CompletedProcess([], 3, output + "\n", "")
            with mock.patch("twguard.bot.subprocess.run", return_value=done):
                self.assertEqual(Bot._check_running(self.bot), expected, output)

    def test_approve_refuses_plan_not_shown_and_shows_it(self):
        self.open_manual()
        with state.locked(self.bot.cfg.state_dir) as st:
            st.open_issue("manual", "• neuer Plan B", fingerprint="bbb", approvable=True)
        self.bot.handle(message("/approve", reply_to=ME))
        self.start_check.assert_not_called()
        self.assertIn("neuer Plan B", self.tg.sent[0])
        self.assertEqual(self.tg.buttons[0][0][0][1], "approve:bbb")
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.approved_fingerprint, "")
            self.assertEqual(st.manual_shown, "bbb")
        # Jetzt ist B gezeigt und gilt
        self.bot.handle(message("/approve", reply_to=ME))
        self.start_check.assert_called_once()
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.approved_fingerprint, "bbb")

    def test_reminder_counts_as_shown(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.open_issue("manual", "x", fingerprint="abc", approvable=True)
            self.bot.cfg.escalation.quiet_hours = ""
            send_due(self.bot.cfg, st, self.tg)
            self.assertEqual(st.manual_shown, "abc")

    def test_resolved_button_closes_failed_apply(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.open_issue("apply-failed", "Skript kaputt")
        self.press("resolved:apply-failed")
        self.assertIn("erledigt", self.tg.edited[-1][1])
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertNotIn("apply-failed", st.issues)

    def test_resolved_button_only_for_failed_apply(self):
        self.open_manual()
        self.press("resolved:manual")
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertIn("manual", st.issues)

    def test_same_message_id_in_other_chat_keeps_topic(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.messages["issue:reboot"] = {"chat_id": -100, "message_id": 5, "at": time.time()}
        self.press("dismiss", chat={"id": 42, "type": "private"})
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertIn("issue:reboot", st.messages)

    def test_collapsed_details_are_kept_per_chat(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.last_plan = {"summary": "0", "rows": [], "problems": []}
        self.press("details")
        self.press("details", chat={"id": 42, "type": "private"})
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(sorted(st.collapsed), ["-100:5", "42:5"])

    def test_help_expires(self):
        self.bot.handle(message("/help@server1_bot"))
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.ephemeral[0]["message_id"], 101)

    def test_button_later_snoozes_only_that_issue(self):
        self.open_manual()
        with state.locked(self.bot.cfg.state_dir) as st:
            st.open_issue("reboot", "Kernel")
        self.press("later:24:reboot")
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertTrue(st.issues["reboot"].snoozed_until)
            self.assertFalse(st.issues["manual"].snoozed_until)
        message_id, text, buttons = self.tg.edited[0]
        self.assertIn("Aufgeschoben bis", text)
        self.assertNotIn("later", str(buttons))

    def test_button_check_on_status_edits_it(self):
        self.bot.handle(message("/status"))
        status_id = self.tg.next_id
        self.bot.handle_callback({"id": "cb", "from": USER, "data": "check",
                                  "message": {"message_id": status_id, "chat": GROUP, "date": int(time.time())}})
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("Prüfung läuft", self.tg.edited[0][1])
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.check_request["message_id"], status_id)

    def test_button_reboot_turns_message_into_reboot_notice(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stderr="")):
            self.press("reboot")
            self.assertIn("startet in 1 Minute", self.tg.edited[0][1])
            self.bot.handle(message("/cancel@server1_bot"))
        self.assertIn("abgebrochen", self.tg.edited[1][1])
        self.assertEqual(self.tg.edited[1][0], 5)

    def test_cancel_offers_reboot_again_while_needed(self):
        self.open_reboot()
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stderr="")):
            self.press("reboot")
            self.press("cancel")
        message_id, text, buttons = self.tg.edited[-1]
        self.assertIn("abgebrochen", text)
        self.assertIn("reboot", self.status_data(buttons))
        self.assertIn("later:24:reboot", self.status_data(buttons))

    def test_cancel_without_open_reboot_has_no_buttons(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stderr="")):
            self.press("reboot")
            self.bot.handle(message("/cancel@server1_bot"))
        self.assertIn("abgebrochen", self.tg.edited[-1][1])
        self.assertFalse(self.tg.edited[-1][2])

    def test_language_switches_at_runtime_and_hints_at_config(self):
        self.addCleanup(set_language, "de")
        self.bot.handle(message("/language en"))
        self.assertEqual(current(), "en")
        self.assertIn("/etc/twguard/config.toml", self.tg.sent[-1])
        self.assertIn('language = "en"', self.tg.sent[-1])
        self.assertIn("until the next restart", self.tg.sent[-1])
        self.assertIn(("status", "Open issues (all servers)"), self.tg.commands)
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.language_override, "en")

    def test_language_back_to_config_clears_override(self):
        self.addCleanup(set_language, "de")
        self.bot.handle(message("/language en"))
        self.bot.handle(message("/language de"))
        self.assertIn("wie in der Konfiguration", self.tg.sent[-1])
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.language_override, "")

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
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.language_override, "")

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
        self.assertIn('mode = "auto"', self.tg.sent[-1])
        self.assertIn("bis zum nächsten Neustart des Bots", self.tg.sent[-1])
        self.assertIn("/check", self.tg.sent[-1])
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.mode_override, "auto")
        self.assertIn("Modus auto bis zum nächsten Neustart", self.bot.status_view()[0])
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
        self.assertIn("wie in der Konfiguration", self.tg.sent[-1])
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.mode_override, "")
        self.bot.handle(message("/mode@server1_bot auto"))
        restarted = Bot(Config(language="de", token="x", chat_id=-100, allowed_user_ids=[42], hostname="server1",
                               state_dir=self.bot.cfg.state_dir), self.tg, self.bot.me)
        restarted.startup()
        self.assertEqual(restarted.cfg.mode, "observe")
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.mode_override, "")

    def test_mode_rejects_unknown_and_shows_current(self):
        self.bot.handle(message("/mode@server1_bot fast"))
        self.assertIn("Format: /mode", self.tg.sent[-1])
        self.bot.handle(message("/mode@server1_bot"))
        self.assertIn("Modus observe, in der Konfiguration observe", self.tg.sent[-1])

    def test_status_shows_pending_instead_of_nothing_open(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.pending_text = "24 Pakete, Download 12 MB"
        text, _ = self.bot.status_view()
        self.assertIn("ausstehend: 24 Pakete", text)
        self.assertNotIn("nichts offen", text)

    def test_briefing_shows_pending_only_while_pending(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.last_plan_text = "24 Pakete, Download 12 MB"
        self.bot.maybe_briefing(self.at(8, 0))
        self.assertNotIn("ausstehend", self.tg.sent[-1])

    def test_rpmdb_change_rechecks_pending(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.pending_text = "24 Pakete"
            st.rpmdb_mtime = 1.0
        with mock.patch.object(self.bot, "_rpmdb_mtime", return_value=2.0), \
                mock.patch.object(self.bot, "check_reboot"), mock.patch.object(self.bot, "start_check") as start:
            self.bot.tick()
        start.assert_called_once()

    def test_briefing_replaces_previous_and_folds_reports(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.reports = [{"chat_id": -100, "message_id": 50, "at": time.time(), "packages": 12},
                          {"at": time.time(), "packages": 3}]
            st.messages["briefing"] = {"chat_id": -100, "message_id": 40, "at": time.time() - 86400}
        self.bot.maybe_briefing(self.at(8, 0))
        self.assertIn("2× eingespielt, 15 Pakete", self.tg.sent[0])
        self.assertEqual(sorted(self.tg.deleted), [40, 50])
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.reports, [])

    def test_supergroup_migration_from_service_message(self):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.messages["status"] = {"chat_id": -100, "message_id": 9, "at": time.time()}
        # Erste Nachricht in der neuen Supergruppe nennt die alte ID
        self.bot.handle({"message_id": 1, "date": int(time.time()), "migrate_from_chat_id": -100,
                         "chat": {"id": -1009, "type": "supergroup"}})
        self.assertEqual(self.bot.cfg.chat_id, -1009)
        self.assertIn("Supergruppe", self.tg.sent[0])
        with state.locked(self.bot.cfg.state_dir) as st:
            self.assertEqual(st.chat_migration["to"], -1009)
            self.assertNotIn("status", st.messages)  # alte Gruppe: nicht mehr bearbeitbar
        # Befehle aus der neuen Gruppe werden angenommen, der Hinweis kommt nur einmal
        self.bot.handle(message("/status", chat={"id": -1009, "type": "supergroup"}))
        self.bot.handle({"message_id": 2, "date": int(time.time()), "migrate_to_chat_id": -1009,
                         "chat": {"id": -100, "type": "group"}})
        self.assertEqual(len(self.tg.sent), 2)
        self.assertIn("server1", self.tg.sent[1])

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

    def test_parse_duration(self):
        self.assertEqual(parse_duration("12h"), 12)
        self.assertEqual(parse_duration("2d"), 48)
        self.assertEqual(parse_duration("1"), 24)
        self.assertIsNone(parse_duration("morgen"))


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
