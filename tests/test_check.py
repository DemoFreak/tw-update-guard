import multiprocessing
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import testlang  # noqa: F401
from twguard import check, panel, state
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
        for target, kwargs in (
            ("refresh", {"return_value": []}),
            ("dry_run", {"side_effect": lambda args: routine_plan()}),
            ("apply", {"side_effect": lambda args: (0, "", routine_plan())}),
            ("needs_rebooting", {"return_value": False}),
        ):
            setattr(self, target, mock.patch.object(check.zypper, target, **kwargs).start())
        self.survey = mock.patch.object(check.restarts, "survey", return_value=check.restarts.Survey([], {})).start()
        self.logind = mock.patch.object(check.busy, "shutdown", return_value=check.busy.Shutdown("none")).start()
        self.addCleanup(mock.patch.stopall)

    def cfg(self, mode):
        return Config(language="de", token="x", chat_id=-100, hostname="server1", mode=mode, state_dir=Path(self.tmp.name))

    def outbox(self) -> list[dict]:
        """Der Prüflauf sendet nichts selbst; Ergebnisse stehen als Ereignis im Zustand."""
        return state.load(Path(self.tmp.name) / "state.json").outbox

    def events(self) -> list[str]:
        return [e["text"] for e in self.outbox()]

    def activity(self) -> dict:
        return state.load(Path(self.tmp.name) / "state.json").activity

    def test_sends_nothing_itself(self):
        # Ein Sender je Server: Zugestellt wird alles vom Bot
        self.assertFalse(hasattr(check, "Telegram"))

    def test_changed_critical_plan_is_a_new_offer(self):
        cfg = self.cfg("auto")
        cfg.escalation.quiet_hours = ""
        cfg.rules.max_packages = 1
        plan_a, plan_b = routine_plan(), routine_plan()
        plan_b.sections["to-upgrade"] = [Solvable("bash", "9.9-1.1", "x86_64", edition_old="5.3.2-1.1")]
        self.dry_run.side_effect = lambda args: plan_a
        plan_a.packages_to_change = plan_b.packages_to_change = 2
        check.run(cfg)
        with state.locked(cfg.state_dir) as st:
            st.issues["manual"].last_notified = time.time()
            st.issues["manual"].snoozed_until = time.time() + 3600
            st.issues["manual"].snooze_count = 2
        check.run(cfg)  # gleicher Plan: Aufschub bleibt
        with state.locked(cfg.state_dir) as st:
            self.assertEqual(st.issues["manual"].snooze_count, 2)
        self.dry_run.side_effect = lambda args: plan_b
        check.run(cfg)
        with state.locked(cfg.state_dir) as st:
            # Neuer Plan: sofort fällig (die Zustandsnachricht kommt mit Ton), der Aufschub galt dem alten
            self.assertEqual(st.issues["manual"].fingerprint, plan_b.fingerprint())
            self.assertEqual(st.issues["manual"].last_notified, 0.0)
            self.assertEqual(st.issues["manual"].snoozed_until, 0.0)
            self.assertEqual(st.issues["manual"].snooze_count, 0)

    def test_observe_does_not_apply(self):
        cfg = self.cfg("observe")
        check.run(cfg)
        self.apply.assert_not_called()
        self.assertIn("im Beobachtungsmodus nicht", self.events()[0])
        # gleicher Stand am nächsten Tag: kein weiteres Ereignis, er steht in der Zustandsnachricht
        check.run(cfg)
        self.assertEqual(len(self.events()), 1)

    def test_observe_applies_after_approval(self):
        cfg = self.cfg("observe")
        check.run(cfg)
        fingerprint = routine_plan().fingerprint()
        with state.locked(cfg.state_dir) as st:
            self.assertEqual(st.observed_fingerprint, fingerprint)
            self.assertEqual(panel.render(cfg, st, time.time()).buttons[0][0],
                             ("▶️ Diesmal einspielen", f"approve:{fingerprint}"))
            st.approved_fingerprint = fingerprint
        check.run(cfg)
        self.apply.assert_called_once()

    def test_uses_language_chosen_at_runtime(self):
        self.addCleanup(set_language, "de")
        cfg = self.cfg("observe")
        with state.locked(cfg.state_dir) as st:
            st.language_override = "en"
        check.run(cfg)
        self.assertIn("not in observation mode", self.events()[0])

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
            st.check_request = {"at": time.time() - age}

    def test_requested_check_reports_result_as_event(self):
        cfg = self.cfg("observe")
        self.request(cfg)
        check.run(cfg)
        self.assertEqual(len(self.events()), 1)
        self.assertIn("im Beobachtungsmodus nicht", self.events()[0])
        with state.locked(cfg.state_dir) as st:
            self.assertEqual(st.check_request, {})

    def test_phases_shown_while_running_and_cleared_after(self):
        cfg = self.cfg("auto")
        seen = []
        record = lambda result: lambda *args: (seen.append(self.activity()["check"]["phase"]), result)[1]
        self.refresh.side_effect = record([])
        self.dry_run.side_effect = record(routine_plan())
        self.apply.side_effect = record((0, "", routine_plan()))
        check.run(cfg)
        # Während zypper einspielt, steht schon „spielt ein“ samt Umfang in der Zustandsnachricht
        self.assertEqual(seen, ["refresh", "dry-run", "apply"])
        self.assertEqual(self.activity(), {})

    def test_phase_carries_own_process(self):
        cfg = self.cfg("auto")
        self.apply.side_effect = lambda args: (seen.append(self.activity()["check"]), (0, "", routine_plan()))[1]
        seen = []
        check.run(cfg)
        self.assertEqual(seen[0]["pid"], os.getpid())
        self.assertIn("Paket", seen[0]["detail"])

    def test_timer_run_reports_routine_quietly(self):
        check.run(self.cfg("auto"))
        self.assertEqual([e["loud"] for e in self.outbox()], [False])
        self.assertIn("1 Paket aktualisiert", self.events()[0])

    def test_timer_run_without_updates_stays_silent(self):
        self.dry_run.side_effect = lambda args: Plan(raw="<stream/>")
        check.run(self.cfg("auto"))
        self.assertEqual(self.events(), [])

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
        self.assertIn("im Beobachtungsmodus nicht", self.events()[0])

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
            st.restart_request = {"at": time.time(), "services": ["postfix"]}

        def restart_done(seconds):
            phases.append(self.activity()["check"]["phase"])
            with state.locked(cfg.state_dir) as st:
                st.restart_request = {}
        phases = []
        with mock.patch.object(check.time, "sleep", side_effect=restart_done) as sleep:
            check.run(cfg)
        sleep.assert_called_once()
        self.assertEqual(phases, ["wait-restart"])
        self.apply.assert_called_once()

    def test_no_apply_while_service_restart_runs(self):
        cfg = self.cfg("auto")
        self.request(cfg)
        fingerprint = routine_plan().fingerprint()
        with state.locked(cfg.state_dir) as st:
            st.restart_request = {"at": time.time(), "services": ["postfix"]}
            st.approved_fingerprint = fingerprint
        with mock.patch.object(check, "RESTART_WAIT", 0), mock.patch.object(check.time, "sleep"):
            self.assertEqual(check.run(cfg), 0)
        self.apply.assert_not_called()
        self.assertIn("nicht eingespielt", self.events()[-1])
        self.assertTrue(self.outbox()[-1]["loud"])
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
        waits = []
        with mock.patch.object(check.time, "sleep", side_effect=lambda s: waits.append(self.activity()["check"])) as sleep:
            self.assertEqual(check.run(cfg), 0)
        self.assertEqual(sleep.call_count, 2)
        self.apply.assert_called_once()
        # Die Zustandsnachricht nennt den Besitzer der Sperre; kein Ereignis je Warteversuch
        self.assertEqual([w["phase"] for w in waits], ["wait-zypp", "wait-zypp"])
        self.assertIn("purge-kernels.service", waits[0]["detail"])
        self.assertEqual(len(self.events()), 1)
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
        self.assertIn("seit 0 min belegt durch purge-kernels.service", self.events()[-1])
        with state.locked(cfg.state_dir) as st:
            self.assertEqual(st.issues, {})
            self.assertEqual(st.approved_fingerprint, fingerprint)
            self.assertEqual(st.check_request, {})

    def test_apply_waits_for_lock_and_shows_progress_again(self):
        cfg = self.cfg("auto")
        self.request(cfg)
        self.apply.side_effect = self.locked_then(1, lambda args: (0, "", routine_plan()))
        phases = []
        original = self.apply.side_effect
        self.apply.side_effect = lambda args: (phases.append(self.activity()["check"]["phase"]), original(args))[1]
        with mock.patch.object(check.time, "sleep", side_effect=lambda s: phases.append(self.activity()["check"]["phase"])):
            check.run(cfg)
        self.assertEqual(self.apply.call_count, 2)
        self.assertEqual(phases, ["apply", "wait-zypp", "apply"])
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
        self.assertIn("Neustart geplant um", self.events()[-1])

    def test_no_apply_when_reboot_gets_planned_during_check(self):
        cfg = self.cfg("auto")
        self.request(cfg)

        def dry_run(args):
            self.logind.return_value = check.busy.Shutdown("planned", "reboot", time.time() + 50)
            return routine_plan()
        self.dry_run.side_effect = dry_run
        self.assertEqual(check.run(cfg), 0)
        self.apply.assert_not_called()
        self.assertIn("Nichts eingespielt: Neustart geplant um", self.events()[-1])
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
        # Auch ohne Anfrage: sonst bliebe ein stummes logind unbemerkt
        self.assertIn("logind gibt keine Auskunft", self.events()[-1])

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
        self.assertIn("im Beobachtungsmodus nicht", self.events()[-1])

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
        st = state.load(cfg.state_dir / "state.json")
        self.assertEqual((st.outbox, st.activity), ([], {}))
        self.assertTrue(st.check_request)  # die Anfrage beantwortet der nächste echte Prüflauf
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
        # Das Ergebnis meldet A selbst; B schweigt dazu
        self.assertFalse([t for t in self.events() if "twguard spielt gerade ein" in t])
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
        self.assertEqual(self.events(), ["🔄 <b>server1</b>: Prüfung fertig, keine Updates."])

    def test_nothing_to_do_keeps_open_items_operable(self):
        # Beobachtung auf einem Server: Das Prüfergebnis zeigte die offenen Punkte ohne den Neustart-Knopf
        cfg = self.cfg("auto")
        self.dry_run.side_effect = lambda args: Plan(raw="<stream/>")
        with state.locked(cfg.state_dir) as st:
            st.open_issue("reboot", "Neustart nötig")
            st.open_issue("services", "", )
            st.restart_services = ["postfix"]
        self.request(cfg)
        check.run(cfg)
        self.assertNotIn("Neustart", self.events()[0])  # das Ereignis nennt nur das Ergebnis
        with state.locked(cfg.state_dir) as st:
            shown = panel.render(cfg, st, time.time())
        actions = [b[1] for row in shown.buttons for b in row]
        self.assertIn("reboot", actions)
        self.assertIn("restart:postfix", actions)

    def test_requested_check_with_manual_result(self):
        cfg = self.cfg("auto")
        self.dry_run.side_effect = lambda args: Plan(
            sections={"to-upgrade": [Solvable("grub2", "2.12-60.1", "x86_64", edition_old="2.12-59.1")]},
            packages_to_change=1, raw="<stream/>")
        self.request(cfg)
        check.run(cfg)
        self.apply.assert_not_called()
        self.assertIn("Freigabe nötig", self.events()[0])
        with state.locked(cfg.state_dir) as st:
            self.assertTrue(st.issues["manual"].approvable)
            shown = panel.render(cfg, st, time.time())
        self.assertTrue(shown.buttons[0][0][1].startswith("approve:"))
        # Der Umfang des Plans steht auch bei einer nötigen Freigabe in der Zustandsnachricht
        self.assertIn("1 Paket, Download 0 MB", shown.text)

    def test_expired_approval_is_reported(self):
        cfg = self.cfg("auto")
        cfg.rules.block = ["bash"]
        with state.locked(cfg.state_dir) as st:
            st.approved_fingerprint = "alt"
        check.run(cfg)
        self.apply.assert_not_called()
        self.assertIn("vorige Freigabe ist verfallen", self.events()[0])
        with state.locked(cfg.state_dir) as st:
            self.assertIn("älteren Stand", st.issues["manual"].detail)

    def test_failure_is_issue_and_event_on_request(self):
        cfg = self.cfg("auto")
        self.refresh.return_value = ["Repo x nicht erreichbar"]
        check.run(cfg)
        self.assertEqual(self.events(), [])  # vom Timer: nur der offene Punkt
        self.request(cfg)
        check.run(cfg)
        self.assertIn("Repos lassen sich nicht auffrischen", self.events()[0])
        with state.locked(cfg.state_dir) as st:
            self.assertIn("Repo x", st.issues["error"].detail)

    def test_routine_report_can_be_switched_off(self):
        cfg = self.cfg("auto")
        cfg.report_routine = False
        check.run(cfg)
        self.assertEqual(self.events(), [])
        self.request(cfg)
        check.run(cfg)
        self.assertEqual(len(self.events()), 1)  # auf Anfrage trotzdem

    def test_deviating_plan_reported_with_sound(self):
        cfg = self.cfg("auto")
        cfg.report_routine = False
        other = Plan(sections={"to-upgrade": [Solvable("bash", "5.3.4-1.1", "x86_64", edition_old="5.3.2-1.1")]},
                     packages_to_change=1)
        self.apply.side_effect = lambda args: (0, "", other)
        check.run(cfg)
        self.assertIn("nicht genau der geprüfte Plan", self.events()[0])
        self.assertIn("+ to-upgrade: bash 5.3.2-1.1 → 5.3.4-1.1 (x86_64)", self.events()[0])
        self.assertIn("− to-upgrade: bash 5.3.2-1.1 → 5.3.3-1.1 (x86_64)", self.events()[0])
        self.assertTrue(self.outbox()[0]["loud"])

    def test_arch_only_deviation_reported(self):
        cfg = self.cfg("auto")
        cfg.report_routine = False
        other = Plan(sections={"to-upgrade": [Solvable("bash", "5.3.3-1.1", "x86_64", edition_old="5.3.2-1.1",
                                                       arch_old="i586")]}, packages_to_change=1)
        self.apply.side_effect = lambda args: (0, "", other)
        check.run(cfg)
        self.assertIn("+ to-upgrade: bash 5.3.2-1.1 → 5.3.3-1.1 (i586 → x86_64)", self.events()[0])
        self.assertTrue(self.outbox()[0]["loud"])

    def test_unreadable_applied_plan_warns(self):
        cfg = self.cfg("auto")
        cfg.report_routine = False
        self.apply.side_effect = lambda args: (0, "", Plan())
        check.run(cfg)
        self.assertIn("nicht lesen", self.events()[0])
        self.assertTrue(self.outbox()[0]["loud"])

    def test_reboot_after_apply_is_issue_not_report_line(self):
        cfg = self.cfg("auto")
        self.apply.side_effect = lambda args: (check.zypper.EXIT_REBOOT_NEEDED, "", routine_plan())
        check.run(cfg)
        with state.locked(cfg.state_dir) as st:
            self.assertIn("reboot", st.issues)
        self.assertNotIn("Neustart", self.events()[0])

    def test_matching_plan_reports_quietly(self):
        check.run(self.cfg("auto"))
        self.assertNotIn("nicht genau", self.events()[0])
        self.assertFalse(self.outbox()[0]["loud"])

    def test_old_request_ignored(self):
        cfg = self.cfg("auto")
        self.dry_run.side_effect = lambda args: Plan(raw="<stream/>")
        self.request(cfg, age=7200)
        check.run(cfg)
        self.assertEqual(self.events(), [])

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
