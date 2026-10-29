import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from twguard import check, state
from twguard.config import Config
from twguard.zypper import Plan, Solvable

from fakes import FakeTelegram


def routine_plan() -> Plan:
    p = Plan(sections={"to-upgrade": [Solvable("bash", "5.3.3-1.1", "x86_64", edition_old="5.3.2-1.1")]},
             packages_to_change=1, raw="<stream/>")
    return p


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tg = FakeTelegram()
        for target, kwargs in (
            ("refresh", {"return_value": []}),
            ("dry_run", {"side_effect": lambda args: routine_plan()}),
            ("apply", {"side_effect": lambda args: (0, "", routine_plan())}),
            ("needs_rebooting", {"return_value": False}),
            ("services_needing_restart", {"return_value": []}),
        ):
            setattr(self, target, mock.patch.object(check.zypper, target, **kwargs).start())
        mock.patch.object(check, "Telegram", return_value=self.tg).start()
        self.addCleanup(mock.patch.stopall)

    def cfg(self, mode):
        return Config(token="x", chat_id=-100, hostname="server1", mode=mode, state_dir=Path(self.tmp.name))

    def test_observe_does_not_apply(self):
        cfg = self.cfg("observe")
        check.run(cfg)
        self.apply.assert_not_called()
        self.assertIn("Beobachtungsmodus", self.tg.sent[0])
        # gleicher Stand am nächsten Tag: keine erneute Meldung
        check.run(cfg)
        self.assertEqual(len(self.tg.sent), 1)

    def test_observe_applies_after_approval(self):
        cfg = self.cfg("observe")
        check.run(cfg)
        fingerprint = routine_plan().fingerprint()
        self.assertEqual(self.tg.buttons[0][0][0], ("▶️ Diesmal einspielen", f"approve:{fingerprint}"))
        with state.locked(cfg.state_dir) as st:
            st.approved_fingerprint = fingerprint
        check.run(cfg)
        self.apply.assert_called_once()

    def request(self, cfg, age=0):
        with state.locked(cfg.state_dir) as st:
            st.check_request = {"chat_id": -100, "message_id": 55, "at": time.time() - age}

    def test_requested_check_edits_waiting_message(self):
        cfg = self.cfg("observe")
        self.request(cfg)
        check.run(cfg)
        self.assertEqual(self.tg.sent, [])  # keine zweite Meldung neben dem Ergebnis
        message_id, text, buttons = self.tg.edited[0]
        self.assertEqual(message_id, 55)
        self.assertIn("Beobachtungsmodus", text)
        self.assertTrue(buttons)
        with state.locked(cfg.state_dir) as st:
            self.assertEqual(st.check_request, {})

    def test_requested_check_reports_nothing_to_do(self):
        cfg = self.cfg("auto")
        self.dry_run.side_effect = lambda args: Plan(raw="<stream/>")
        self.request(cfg)
        check.run(cfg)
        self.assertIn("keine Updates", self.tg.edited[0][1])

    def test_requested_check_reports_manual_issue_in_quiet_hours(self):
        cfg = self.cfg("auto")
        cfg.escalation.quiet_hours = "00:00-23:59"
        self.dry_run.side_effect = lambda args: Plan(
            sections={"to-upgrade": [Solvable("grub2", "2.12-60.1", "x86_64", edition_old="2.12-59.1")]},
            packages_to_change=1, raw="<stream/>")
        self.request(cfg)
        check.run(cfg)
        self.apply.assert_not_called()
        _, text, buttons = self.tg.edited[0]
        self.assertIn("Freigabe", text)
        self.assertTrue(buttons[0][0][1].startswith("approve:"))
        with state.locked(cfg.state_dir) as st:
            self.assertEqual(st.issues["manual"].notify_count, 1)

    def test_manual_result_becomes_issue_message(self):
        cfg = self.cfg("auto")
        self.dry_run.side_effect = lambda args: Plan(
            sections={"to-upgrade": [Solvable("grub2", "2.12-60.1", "x86_64", edition_old="2.12-59.1")]},
            packages_to_change=1, raw="<stream/>")
        with state.locked(cfg.state_dir) as st:
            st.messages["status"] = {"chat_id": -100, "message_id": 55, "at": time.time()}
            st.messages["issue:manual"] = {"chat_id": -100, "message_id": 30, "at": time.time()}
        self.request(cfg)
        check.run(cfg)
        with state.locked(cfg.state_dir) as st:
            self.assertEqual(st.messages["issue:manual"]["message_id"], 55)
            self.assertNotIn("status", st.messages)
        self.assertIn(30, self.tg.deleted)  # alte Meldung zum selben Punkt entfernt

    def test_routine_report_kept_for_briefing(self):
        cfg = self.cfg("auto")
        check.run(cfg)
        with state.locked(cfg.state_dir) as st:
            self.assertEqual(len(st.reports), 1)
            self.assertEqual(st.reports[0]["packages"], 1)
            self.assertTrue(st.reports[0]["message_id"])
        self.assertTrue(self.tg.silent[0])

    def test_deviating_plan_reported_with_sound(self):
        cfg = self.cfg("auto")
        cfg.report_routine = False
        other = Plan(sections={"to-upgrade": [Solvable("bash", "5.3.4-1.1", "x86_64", edition_old="5.3.2-1.1")]},
                     packages_to_change=1)
        self.apply.side_effect = lambda args: (0, "", other)
        check.run(cfg)
        self.assertIn("nicht genau der geprüfte Plan", self.tg.sent[0])
        self.assertIn("+ to-upgrade: bash 5.3.2-1.1 → 5.3.4-1.1 (x86_64)", self.tg.sent[0])
        self.assertIn("− to-upgrade: bash 5.3.2-1.1 → 5.3.3-1.1 (x86_64)", self.tg.sent[0])
        self.assertFalse(self.tg.silent[0])

    def test_arch_only_deviation_reported(self):
        cfg = self.cfg("auto")
        cfg.report_routine = False
        other = Plan(sections={"to-upgrade": [Solvable("bash", "5.3.3-1.1", "x86_64", edition_old="5.3.2-1.1",
                                                       arch_old="i586")]}, packages_to_change=1)
        self.apply.side_effect = lambda args: (0, "", other)
        check.run(cfg)
        self.assertIn("+ to-upgrade: bash 5.3.2-1.1 → 5.3.3-1.1 (i586 → x86_64)", self.tg.sent[0])
        self.assertFalse(self.tg.silent[0])

    def test_unreadable_applied_plan_warns(self):
        cfg = self.cfg("auto")
        cfg.report_routine = False
        self.apply.side_effect = lambda args: (0, "", Plan())
        check.run(cfg)
        self.assertIn("nicht lesen", self.tg.sent[0])
        self.assertFalse(self.tg.silent[0])

    def test_deviation_after_request_comes_as_new_message(self):
        cfg = self.cfg("auto")
        other = Plan(sections={"to-upgrade": [Solvable("bash", "5.3.4-1.1", "x86_64", edition_old="5.3.2-1.1")]},
                     packages_to_change=1)
        self.apply.side_effect = lambda args: (0, "", other)
        self.request(cfg)
        check.run(cfg)
        self.assertIn(55, self.tg.deleted)  # Wartemeldung weg, Bearbeiten hätte keinen Ton
        self.assertEqual(self.tg.edited, [])
        self.assertIn("nicht genau der geprüfte Plan", self.tg.sent[-1])
        self.assertFalse(self.tg.silent[-1])

    def test_matching_plan_reports_quietly(self):
        check.run(self.cfg("auto"))
        self.assertNotIn("nicht genau", self.tg.sent[0])

    def test_old_request_ignored(self):
        cfg = self.cfg("observe")
        self.request(cfg, age=7200)
        check.run(cfg)
        self.assertEqual(self.tg.edited, [])
        self.assertEqual(len(self.tg.sent), 1)

    def test_auto_applies(self):
        check.run(self.cfg("auto"))
        self.apply.assert_called_once()

    def test_plan_xml_saved_once(self):
        cfg = self.cfg("observe")
        check.run(cfg)
        check.run(cfg)
        self.assertEqual(len(list((cfg.state_dir / "plans").glob("dup-*.xml"))), 1)

    def test_save_plan_keeps_last(self):
        d = Path(self.tmp.name)
        for i in range(5):
            p = routine_plan()
            (d / "plans").mkdir(exist_ok=True)
            (d / "plans" / f"dup-2026010{i}-0000-x{i}.xml").write_text("alt")
        check.save_plan(d, routine_plan(), "neu", keep=3)
        names = sorted(f.name for f in (d / "plans").glob("dup-*.xml"))
        self.assertEqual(len(names), 3)
        self.assertTrue(names[-1].endswith("-neu.xml"))


class ChatMigrationConfigTest(unittest.TestCase):
    def test_remembered_migration_applies_while_config_is_old(self):
        from twguard.cli import apply_chat_migration
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(chat_id=-100, state_dir=Path(tmp))
            with state.locked(cfg.state_dir) as st:
                st.chat_migration = {"from": -100, "to": -1009, "at": 0}
            apply_chat_migration(cfg)
            self.assertEqual(cfg.chat_id, -1009)
            # Steht schon die neue (oder eine andere) ID in der Konfiguration, bleibt sie
            cfg = Config(chat_id=-1009, state_dir=Path(tmp))
            apply_chat_migration(cfg)
            self.assertEqual(cfg.chat_id, -1009)


class ConfigModeTest(unittest.TestCase):
    def test_invalid_mode_rejected(self):
        from twguard import config
        with tempfile.NamedTemporaryFile("w", suffix=".toml") as f:
            f.write('[general]\nmode = "automatisch"\n')
            f.flush()
            with self.assertRaises(SystemExit):
                config.load(f.name)

    def test_default_is_observe(self):
        self.assertTrue(Config().observe)


if __name__ == "__main__":
    unittest.main()
