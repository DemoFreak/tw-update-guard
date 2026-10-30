import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import testlang  # noqa: F401
from twguard import ota, state
from twguard.bot import Bot, parse_duration
from twguard.config import Config
from twguard.i18n import current, set_language

from fakes import FakeTelegram

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
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.tmp.cleanup)

    def open_manual(self, approvable=True):
        with state.locked(self.bot.cfg.state_dir) as st:
            st.open_issue("manual", "• kritisches Paket", fingerprint="abc", approvable=approvable)

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
