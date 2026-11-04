import multiprocessing
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import testlang  # noqa: F401
from twguard import check, state
from twguard.config import Config
from twguard.i18n import set_language
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
        self.logind = mock.patch.object(check.busy, "shutdown", return_value=check.busy.Shutdown("none")).start()
        self.addCleanup(mock.patch.stopall)

    def cfg(self, mode):
        return Config(language="de", token="x", chat_id=-100, hostname="server1", mode=mode, state_dir=Path(self.tmp.name))

    def test_changed_critical_plan_is_a_new_offer(self):
        cfg = self.cfg("auto")
        cfg.escalation.quiet_hours = ""
        cfg.rules.max_packages = 1
        plan_a, plan_b = routine_plan(), routine_plan()
        plan_b.sections["to-upgrade"] = [Solvable("bash", "9.9-1.1", "x86_64", edition_old="5.3.2-1.1")]
        self.dry_run.side_effect = lambda args: plan_a
        plan_a.packages_to_change = plan_b.packages_to_change = 2
        check.run(cfg)
        self.assertEqual(len(self.tg.sent), 1)
        with state.locked(cfg.state_dir) as st:
            st.issues["manual"].snoozed_until = time.time() + 3600
            st.issues["manual"].snooze_count = 2
        check.run(cfg)  # gleicher Plan: keine neue Meldung
        self.assertEqual(len(self.tg.sent), 1)
        self.dry_run.side_effect = lambda args: plan_b
        check.run(cfg)
        self.assertEqual(len(self.tg.sent), 2)
        with state.locked(cfg.state_dir) as st:
            self.assertEqual(st.manual_shown, plan_b.fingerprint())
            self.assertEqual(st.issues["manual"].snoozed_until, 0.0)
            self.assertEqual(st.issues["manual"].snooze_count, 0)

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

    def test_uses_language_chosen_at_runtime(self):
        self.addCleanup(set_language, "de")
        cfg = self.cfg("observe")
        with state.locked(cfg.state_dir) as st:
            st.language_override = "en"
        check.run(cfg)
        self.assertIn("Observation mode", self.tg.sent[0])

    def test_observe_remembers_pending_until_applied(self):
        cfg = self.cfg("observe")
        check.run(cfg)
        with state.locked(cfg.state_dir) as st:
            self.assertIn("Paket", st.pending_text)
            st.approved_fingerprint = routine_plan().fingerprint()
        check.run(cfg)
        with state.locked(cfg.state_dir) as st:
            self.assertEqual(st.pending_text, "")

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

    def test_requested_check_shows_progress_before_applying(self):
        cfg = self.cfg("auto")
        self.request(cfg)
        shown = []
        self.apply.side_effect = lambda args: (shown.extend(self.tg.edited), (0, "", routine_plan()))[1]
        check.run(cfg)
        # Während zypper läuft, steht schon „spielt ein“ in der Wartemeldung, danach das Ergebnis
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown[0][0], 55)
        self.assertIn("spielt das Update ein", shown[0][1])
        self.assertEqual(self.tg.edited[-1][0], 55)
        self.assertNotIn("spielt das Update ein", self.tg.edited[-1][1])

    def test_timer_run_sends_no_progress(self):
        check.run(self.cfg("auto"))
        self.assertFalse([text for _, text, _ in self.tg.edited if "spielt das Update ein" in text])

    def test_mode_from_bot_applies_to_check(self):
        cfg = self.cfg("observe")
        with state.locked(cfg.state_dir) as st:
            st.mode_override = "auto"
        check.run(cfg)
        self.apply.assert_called_once()

    def mode_bot(self, configured):
        """Echter Bot mit eigener Konfiguration und demselben Zustand, wie im Betrieb getrennt vom Prüflauf."""
        from twguard.bot import Bot
        bot = Bot(self.cfg(configured), tg=FakeTelegram(), me={"id": 1000, "username": "server1_bot"})
        return bot

    def switch_during(self, target, configured, mode):
        """Während `target` (refresh oder dry_run) läuft, per /mode umschalten."""
        bot = self.mode_bot(configured)
        original = getattr(self, target).side_effect
        answers = []

        def switch(*args):
            answers.append(bot.cmd_mode([mode], addressed=True))
            return original(*args) if original else []
        getattr(self, target).side_effect = switch
        return answers

    def test_mode_observe_during_refresh_stops_apply(self):
        answers = self.switch_during("refresh", "auto", "observe")
        cfg = self.cfg("auto")
        check.run(cfg)
        self.assertIn("observe", answers[0].text)
        with state.locked(cfg.state_dir) as st:
            self.assertEqual(st.mode_override, "observe")
        self.apply.assert_not_called()
        self.assertIn("Beobachtungsmodus", self.tg.sent[0])

    def test_mode_auto_during_dry_run_applies(self):
        self.switch_during("dry_run", "observe", "auto")
        check.run(self.cfg("observe"))
        self.apply.assert_called_once()

    def test_mode_back_to_configured_during_refresh(self):
        cfg = self.cfg("auto")
        with state.locked(cfg.state_dir) as st:
            st.mode_override = "observe"
        # /mode auto bei auto in der Konfiguration leert den Wert: wieder die Konfiguration
        self.switch_during("refresh", "auto", "auto")
        check.run(cfg)
        with state.locked(cfg.state_dir) as st:
            self.assertEqual(st.mode_override, "")
        self.apply.assert_called_once()

    def test_bot_restart_during_refresh_returns_to_config(self):
        cfg = self.cfg("observe")
        with state.locked(cfg.state_dir) as st:
            st.mode_override = "auto"
        bot = self.mode_bot("observe")
        with mock.patch.object(bot, "_set_menu"):
            self.refresh.side_effect = lambda: (bot.startup(), [])[1]
            check.run(cfg)
        self.apply.assert_not_called()

    def test_approval_still_applies_after_mode_observe(self):
        cfg = self.cfg("auto")
        cfg.rules.block = ["bash"]
        with state.locked(cfg.state_dir) as st:
            st.approved_fingerprint = routine_plan().fingerprint()
        self.switch_during("refresh", "auto", "observe")
        check.run(cfg)
        self.apply.assert_called_once()

    def test_mode_observe_while_applying_says_it_continues(self):
        cfg = self.cfg("auto")
        bot = self.mode_bot("auto")
        answers = []
        self.apply.side_effect = lambda args: (answers.append(bot.cmd_mode(["observe"], addressed=True)),
                                               (0, "", routine_plan()))[1]
        check.run(cfg)
        self.assertIn("läuft noch zu Ende", answers[0].text)

    def test_apply_waits_for_service_restart(self):
        cfg = self.cfg("auto")
        with state.locked(cfg.state_dir) as st:
            st.restart_request = {"chat_id": -100, "message_id": 9, "at": time.time(), "services": ["postfix"]}

        def restart_done(seconds):
            with state.locked(cfg.state_dir) as st:
                st.restart_request = {}
        with mock.patch.object(check.time, "sleep", side_effect=restart_done) as sleep:
            check.run(cfg)
        sleep.assert_called_once()
        self.apply.assert_called_once()

    def test_no_apply_while_service_restart_runs(self):
        cfg = self.cfg("auto")
        self.request(cfg)
        fingerprint = routine_plan().fingerprint()
        with state.locked(cfg.state_dir) as st:
            st.restart_request = {"chat_id": -100, "message_id": 9, "at": time.time(), "services": ["postfix"]}
            st.approved_fingerprint = fingerprint
        with mock.patch.object(check, "RESTART_WAIT", 0), mock.patch.object(check.time, "sleep"):
            self.assertEqual(check.run(cfg), 0)
        self.apply.assert_not_called()
        self.assertIn("nicht eingespielt", self.tg.sent[-1])
        with state.locked(cfg.state_dir) as st:
            self.assertEqual(st.approved_fingerprint, fingerprint)  # die Freigabe gilt beim nächsten Lauf
            self.assertEqual(st.applying, {})

    def locked_then(self, times, result):
        """Wie zypper bei belegter Paketverwaltung: `times`-mal Locked, dann `result`."""
        calls = []

        def call(*args):
            calls.append(args)
            if len(calls) <= times:
                raise check.zypper.Locked("purge-kernels.service (zypper, PID 707)")
            return result(*args) if callable(result) else result
        return call

    def test_waits_for_locked_package_management(self):
        # Nach dem Boot sperrt purge-kernels zypper minutenlang; das ist kein Fehler
        cfg = self.cfg("auto")
        self.request(cfg)
        self.refresh.side_effect = self.locked_then(2, [])
        with mock.patch.object(check.time, "sleep") as sleep:
            self.assertEqual(check.run(cfg), 0)
        self.assertEqual(sleep.call_count, 2)
        self.apply.assert_called_once()
        # Ein Hinweis, solange derselbe Besitzer sperrt
        waits = [t for _, t, _ in self.tg.edited if "Paketverwaltung ist belegt" in t]
        self.assertEqual(len(waits), 1)
        self.assertIn("purge-kernels.service", waits[0])
        with state.locked(cfg.state_dir) as st:
            self.assertNotIn("error", st.issues)

    def test_lock_beyond_wait_is_no_error(self):
        cfg = self.cfg("auto")
        self.request(cfg)
        fingerprint = routine_plan().fingerprint()
        with state.locked(cfg.state_dir) as st:
            st.approved_fingerprint = fingerprint
        self.dry_run.side_effect = self.locked_then(99, None)
        with mock.patch.object(check, "LOCK_WAIT", 0), mock.patch.object(check.time, "sleep"):
            self.assertEqual(check.run(cfg), 0)
        self.apply.assert_not_called()
        self.assertIn("seit 0 min belegt durch purge-kernels.service", self.tg.sent[-1])
        with state.locked(cfg.state_dir) as st:
            self.assertEqual(st.issues, {})
            self.assertEqual(st.approved_fingerprint, fingerprint)
            self.assertEqual(st.check_request, {})

    def test_apply_waits_for_lock_and_shows_progress_again(self):
        cfg = self.cfg("auto")
        self.request(cfg)
        self.apply.side_effect = self.locked_then(1, lambda args: (0, "", routine_plan()))
        with mock.patch.object(check.time, "sleep"):
            check.run(cfg)
        self.assertEqual(self.apply.call_count, 2)
        texts = [t for _, t, _ in self.tg.edited]
        wait = next(i for i, t in enumerate(texts) if "Paketverwaltung ist belegt" in t)
        self.assertTrue(any("spielt das Update ein" in t for t in texts[wait + 1:]))
        with state.locked(cfg.state_dir) as st:
            self.assertEqual(st.applying, {})
            self.assertNotIn("apply-failed", st.issues)

    def test_lock_after_wait_releases_reservation(self):
        cfg = self.cfg("auto")
        self.apply.side_effect = self.locked_then(99, None)
        with mock.patch.object(check, "LOCK_WAIT", 0), mock.patch.object(check.time, "sleep"):
            self.assertEqual(check.run(cfg), 0)
        with state.locked(cfg.state_dir) as st:
            self.assertEqual(st.applying, {})
            self.assertNotIn("apply-failed", st.issues)

    def test_no_check_while_reboot_is_planned(self):
        # Der Neustart bräche die Prüfung ab; ihr Ergebnis käme nie an
        cfg = self.cfg("auto")
        self.request(cfg)
        self.logind.return_value = check.busy.Shutdown("planned", "reboot", time.time() + 50)
        self.assertEqual(check.run(cfg), 0)
        self.refresh.assert_not_called()
        self.apply.assert_not_called()
        self.assertIn("Neustart geplant um", self.tg.edited[-1][1])

    def test_no_apply_when_reboot_gets_planned_during_check(self):
        cfg = self.cfg("auto")
        self.request(cfg)

        def dry_run(args):
            self.logind.return_value = check.busy.Shutdown("planned", "reboot", time.time() + 50)
            return routine_plan()
        self.dry_run.side_effect = dry_run
        self.assertEqual(check.run(cfg), 0)
        self.apply.assert_not_called()
        self.assertIn("Nichts eingespielt: Neustart geplant um", self.tg.sent[-1])
        with state.locked(cfg.state_dir) as st:
            self.assertEqual(st.applying, {})

    def test_reboot_cancelled_on_console_does_not_block(self):
        # Nur logind zählt: Ein selbst ausgelöster, dann per shutdown -c abgebrochener Neustart sperrt nicht
        cfg = self.cfg("auto")
        with state.locked(cfg.state_dir) as st:
            st.reboot_requested_at = time.time() - 30
        check.run(cfg)
        self.apply.assert_called_once()

    def test_unknown_logind_checks_but_does_not_apply(self):
        cfg = self.cfg("auto")
        self.logind.return_value = check.busy.Shutdown("unknown")
        self.assertEqual(check.run(cfg), 0)
        self.refresh.assert_called_once()
        self.apply.assert_not_called()
        self.assertIn("logind gibt keine Auskunft", self.tg.sent[-1])

    def test_mode_switch_during_lock_wait_is_respected(self):
        # F18: Nach dem Warten wird neu entschieden, nicht nur zypper wiederholt
        cfg = self.cfg("auto")
        self.apply.side_effect = self.locked_then(1, lambda args: (0, "", routine_plan()))

        def switch(seconds):
            with state.locked(cfg.state_dir) as st:
                st.mode_override = "observe"
        with mock.patch.object(check.time, "sleep", side_effect=switch):
            check.run(cfg)
        self.assertEqual(self.apply.call_count, 1)  # nur der gesperrte Versuch
        self.assertEqual(self.refresh.call_count, 2)
        self.assertIn("Beobachtungsmodus", self.tg.sent[-1])

    def test_plan_changed_during_lock_wait_drops_approval(self):
        # F18: Eine fremde Paketoperation während des Wartens kann den Plan ändern; die Freigabe galt dem alten
        cfg = self.cfg("observe")
        cfg.escalation.quiet_hours = ""
        old = routine_plan()
        new = routine_plan()
        new.sections["to-upgrade"] = [Solvable("bash", "5.3.9-1.1", "x86_64", edition_old="5.3.2-1.1")]
        with state.locked(cfg.state_dir) as st:
            st.approved_fingerprint = old.fingerprint()
        plans = iter([old, new])
        self.dry_run.side_effect = lambda args: next(plans)
        self.apply.side_effect = self.locked_then(1, lambda args: (0, "", new))
        with mock.patch.object(check.time, "sleep"):
            check.run(cfg)
        self.assertEqual(self.apply.call_count, 1)  # der neue Plan wird nicht ungefragt eingespielt
        with state.locked(cfg.state_dir) as st:
            self.assertEqual(st.approved_fingerprint, "")
            self.assertEqual(st.applying, {})

    def test_reboot_planned_during_lock_wait_stops(self):
        cfg = self.cfg("auto")
        self.refresh.side_effect = self.locked_then(1, [])

        def plan(seconds):
            self.logind.return_value = check.busy.Shutdown("planned", "reboot", time.time() + 50)
        with mock.patch.object(check.time, "sleep", side_effect=plan):
            self.assertEqual(check.run(cfg), 0)
        self.assertEqual(self.refresh.call_count, 1)
        self.apply.assert_not_called()

    def test_dry_run_waits_on_console_only(self):
        # F19: --dry-run sendet nichts, auch nicht die Wartemeldung einer offenen Anfrage
        cfg = self.cfg("auto")
        self.request(cfg)
        self.refresh.side_effect = self.locked_then(1, [])
        with mock.patch.object(check.time, "sleep"), mock.patch("builtins.print"):
            self.assertEqual(check.run(cfg, dry_run=True), 0)
        self.assertEqual((self.tg.sent, self.tg.edited, self.tg.deleted), ([], [], []))
        self.apply.assert_not_called()

    def test_reservation_held_during_apply_and_released(self):
        cfg = self.cfg("auto")
        seen = []

        def apply(args):
            with state.locked(cfg.state_dir) as st:
                seen.append(state.alive(st.applying))
            return 0, "", routine_plan()
        self.apply.side_effect = apply
        check.run(cfg)
        self.assertEqual(seen, [True])
        with state.locked(cfg.state_dir) as st:
            self.assertEqual(st.applying, {})

    def test_reservation_released_when_apply_crashes(self):
        cfg = self.cfg("auto")
        self.apply.side_effect = OSError("zypper fehlt")
        with self.assertRaises(OSError):
            check.run(cfg)
        with state.locked(cfg.state_dir) as st:
            self.assertEqual(st.applying, {})

    def start_holder(self, cfg):
        """Prüflauf A als eigener Prozess, angehalten mitten im Einspielen."""
        ctx = multiprocessing.get_context("fork")
        inside, release = ctx.Event(), ctx.Event()

        def apply(args):
            inside.set()
            release.wait(30)
            return 0, "", routine_plan()

        def run_a():
            self.apply.side_effect = apply
            os._exit(check.run(cfg))
        a = ctx.Process(target=run_a)
        a.start()
        self.addCleanup(lambda: (release.set(), a.join(10), a.is_alive() and a.kill()))
        self.assertTrue(inside.wait(10), "A erreicht das Einspielen nicht")
        return a, release

    def test_second_check_never_takes_over_live_reservation(self):
        cfg = self.cfg("auto")
        a, release = self.start_holder(cfg)
        with state.locked(cfg.state_dir) as st:
            held = dict(st.applying)
        self.assertEqual(held["pid"], a.pid)
        # B kommt mit fertigem Probelauf zur Entscheidung; sein zypper wäre gesperrt (Exit-Code 7)
        self.apply.side_effect = lambda args: (7, "System management is locked", Plan())
        self.request(cfg)
        self.assertEqual(check.run(cfg), 0)
        self.apply.assert_not_called()
        self.assertIn("twguard spielt gerade ein", self.tg.sent[-1])
        with state.locked(cfg.state_dir) as st:
            self.assertEqual(st.applying, held)  # weder übernommen noch gelöst
            self.assertTrue(state.alive(st.applying))
        release.set()
        a.join(10)
        self.assertEqual(a.exitcode, 0)
        with state.locked(cfg.state_dir) as st:
            self.assertEqual(st.applying, {})  # A gibt seine Reservierung selbst frei

    def test_second_check_failing_does_not_release_foreign_reservation(self):
        cfg = self.cfg("auto")
        a, release = self.start_holder(cfg)
        with state.locked(cfg.state_dir) as st:
            held = dict(st.applying)
        self.refresh.side_effect = OSError("Abbruch in B")
        with self.assertRaises(OSError):
            check.run(cfg)
        with state.locked(cfg.state_dir) as st:
            self.assertEqual(st.applying, held)

    def test_release_only_by_owner(self):
        cfg = self.cfg("auto")
        foreign = {"pid": os.getppid(), "boot": state.holder()["boot"], "at": 1.0}
        with state.locked(cfg.state_dir) as st:
            st.applying = foreign
        check._release(cfg, {**foreign, "pid": os.getpid()})
        check._release(cfg, {})
        with state.locked(cfg.state_dir) as st:
            self.assertEqual(st.applying, foreign)

    def test_orphaned_reservation_is_replaced(self):
        cfg = self.cfg("auto")
        with state.locked(cfg.state_dir) as st:
            st.applying = {**state.holder(), "boot": "vor-dem-reboot"}
        check.run(cfg)
        self.apply.assert_called_once()
        with state.locked(cfg.state_dir) as st:
            self.assertEqual(st.applying, {})

    def test_check_follows_config_without_override(self):
        check.run(self.cfg("observe"))
        self.apply.assert_not_called()

    def test_requested_check_reports_nothing_to_do(self):
        cfg = self.cfg("auto")
        self.dry_run.side_effect = lambda args: Plan(raw="<stream/>")
        self.request(cfg)
        check.run(cfg)
        self.assertIn("keine Updates", self.tg.edited[0][1])
        self.assertIsNone(self.tg.edited[0][2])

    def test_nothing_to_do_keeps_open_items_operable(self):
        cfg = self.cfg("auto")
        self.dry_run.side_effect = lambda args: Plan(raw="<stream/>")
        with state.locked(cfg.state_dir) as st:
            st.open_issue("reboot", "Neustart nötig")
        self.request(cfg)
        check.run(cfg)
        _, text, buttons = self.tg.edited[0]
        self.assertIn("keine Updates", text)
        self.assertIn("Neustart", text)
        self.assertIn("reboot", [b[1] for row in buttons for b in row])

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
        # Bearbeitet wird nur der Zwischenstand vor dem Einspielen, nicht mit dem Ergebnis
        self.assertEqual([text for _, text, _ in self.tg.edited if "spielt das Update ein" not in text], [])
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
            cfg = Config(language="de", chat_id=-100, state_dir=Path(tmp))
            with state.locked(cfg.state_dir) as st:
                st.chat_migration = {"from": -100, "to": -1009, "at": 0}
            apply_chat_migration(cfg)
            self.assertEqual(cfg.chat_id, -1009)
            # Steht schon die neue (oder eine andere) ID in der Konfiguration, bleibt sie
            cfg = Config(language="de", chat_id=-1009, state_dir=Path(tmp))
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
