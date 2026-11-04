import subprocess
import unittest
from pathlib import Path
from unittest import mock

import testlang  # noqa: F401
from twguard import notify, panel, restarts, zypper
from twguard.config import Config, Services
from twguard.state import State

EXCLUDE = Services().exclude
FIXTURES = Path(__file__).parent / "fixtures"


def survey(names, lingering=()):
    return restarts.Survey(list(names), {name: [zypper.Proc(7, "alice", "screen", name)] for name in lingering})


def done(stdout="", rc=0, stderr=""):
    return subprocess.CompletedProcess([], rc, stdout, stderr)


class ZypperTest(unittest.TestCase):
    def ps(self, output, rc=0):
        with mock.patch.object(zypper.subprocess, "run", return_value=done(output, rc)):
            return zypper.processes_needing_restart()

    def test_real_table(self):
        self.assertEqual(self.ps((FIXTURES / "ps-ss-real.txt").read_text()),
                         [zypper.Proc(6341, "alice", "screen (deleted)", "sshd"),
                          zypper.Proc(3849108, "games", "screen (deleted)", "cron")])

    def test_nothing_found(self):
        self.assertEqual(self.ps("Es wurden keine Prozesse gefunden, die gelöschte Dateien verwenden.\n"), [])

    def test_own_units_and_empty_service_skipped(self):
        output = ("PID | PPID | UID | User | Command | Service\n"
                  "1 | 0 | 0 | root | a | twguard-check\n2 | 0 | 0 | root | b | \n3 | 0 | 0 | root | c | sshd\n")
        self.assertEqual([p.service for p in self.ps(output)], ["sshd"])

    def test_unexpected_table_is_unknown(self):
        self.assertIsNone(self.ps("PID | PPID | UID | User | Command | Service\n12 | 1 | 0 | root | sshd\n"))
        self.assertIsNone(self.ps("PID | PPID | UID | User | Command | Service\nPID | x | x | x | x | x\n"))

    def test_failure_is_not_an_empty_list(self):
        self.assertIsNone(self.ps("", rc=1))


class SurveyTest(unittest.TestCase):
    """Hilft ein Neustart? Nur bei KillMode=process/none und frischem Hauptprozess nicht."""

    def run_survey(self, show):
        procs = [zypper.Proc(6341, "alice", "screen", "sshd"), zypper.Proc(900, "root", "master", "postfix")]
        with mock.patch.object(zypper, "processes_needing_restart", return_value=procs), \
                mock.patch.object(restarts, "_systemctl", side_effect=lambda *a: done(show[a[-1]])):
            return restarts.survey()

    def test_left_over_only_with_kill_mode_process_and_fresh_main(self):
        found = self.run_survey({"sshd": "MainPID=4020428\nKillMode=process\n",
                                 "postfix": "MainPID=800\nKillMode=control-group\n"})
        self.assertEqual(found.services, ["postfix", "sshd"])
        self.assertEqual(list(found.lingering), ["sshd"])

    def test_restart_helps_when_main_process_affected(self):
        found = self.run_survey({"sshd": "MainPID=6341\nKillMode=process\n", "postfix": "MainPID=900\nKillMode=process\n"})
        self.assertEqual(found.lingering, {})

    def test_without_main_pid_or_answer_restart_stays(self):
        found = self.run_survey({"sshd": "MainPID=0\nKillMode=process\n", "postfix": ""})
        self.assertEqual(found.lingering, {})

    def test_zypper_unknown(self):
        with mock.patch.object(zypper, "processes_needing_restart", return_value=None):
            self.assertIsNone(restarts.survey())


class NoteTest(unittest.TestCase):
    def test_split_by_exclude_list(self):
        found = ["dbus-broker", "postfix", "sshd", "systemd-logind", "user@1000"]
        self.assertEqual(restarts.split(found, EXCLUDE),
                         (["postfix", "sshd"], ["dbus-broker", "systemd-logind", "user@1000"]))

    def test_opens_issue_with_skipped_named(self):
        st = State()
        restarts.note(st, survey(["dbus", "sshd"]), EXCLUDE)
        self.assertEqual(st.restart_services, ["sshd"])
        self.assertIn("sshd", st.issues["services"].detail)
        self.assertIn("dbus", st.issues["services"].detail)

    def test_only_excluded_opens_nothing(self):
        st = State()
        restarts.note(st, survey(["dbus"]), EXCLUDE)
        self.assertEqual(st.issues, {})

    def test_exclude_all_turns_offer_off(self):
        st = State()
        restarts.note(st, survey(["sshd"]), ["*"])
        self.assertEqual(st.issues, {})

    def test_resolved_when_gone(self):
        st = State()
        restarts.note(st, survey(["sshd"]), EXCLUDE)
        restarts.note(st, survey([]), EXCLUDE)
        self.assertEqual(st.issues, {})
        self.assertEqual(st.restart_services, [])

    def test_new_service_notifies_again_but_shrinking_does_not(self):
        st = State()
        restarts.note(st, survey(["a", "b"]), EXCLUDE)
        st.issues["services"].last_notified = 5.0
        restarts.note(st, survey(["a"]), EXCLUDE)
        self.assertEqual(st.issues["services"].last_notified, 5.0)
        restarts.note(st, survey(["a", "c"]), EXCLUDE)
        self.assertEqual(st.issues["services"].last_notified, 0.0)

    def test_left_over_is_hint_not_issue(self):
        st = State()
        restarts.note(st, survey(["sshd", "postfix"], lingering=["sshd"]), EXCLUDE)
        self.assertEqual(st.restart_services, ["postfix"])
        self.assertEqual(st.lingering, {"sshd": [{"pid": 7, "user": "alice", "command": "screen", "service": "sshd"}]})
        restarts.note(st, survey(["sshd"], lingering=["sshd"]), EXCLUDE)
        self.assertEqual(st.issues, {})
        shown = panel.render(Config(language="de", hostname="server1"), st, 0.0)
        self.assertIn("ℹ️ Dienst sshd neu gestartet. Prozess screen (PID 7, alice) nutzt bis zu seinem Ende weiter "
                      "gelöschte Dateien.", shown.text)
        # Ein Hinweis, kein Knopf: Ein Neustart des Dienstes beendet die Sitzung nicht
        self.assertEqual([b[1] for row in shown.buttons for b in row], ["check"])
        restarts.note(st, survey([]), EXCLUDE)
        self.assertEqual(st.lingering, {})

    def test_hint_for_several_processes(self):
        procs = [{"pid": n, "user": "games", "command": "screen (deleted)", "service": "cron"} for n in range(1, 6)]
        self.assertEqual(notify.lingering_line("cron", procs),
                         "ℹ️ Dienst cron neu gestartet. Prozesse screen (PID 1, games), screen (PID 2, games), "
                         "screen (PID 3, games), 2 weitere nutzen bis zu ihrem Ende weiter gelöschte Dateien.")

    def test_ignored_while_restart_runs(self):
        st = State(restart_request={"services": ["sshd"]})
        restarts.note(st, survey(["sshd"]), EXCLUDE)
        self.assertEqual(st.issues, {})


class ButtonsTest(unittest.TestCase):
    def data(self, names):
        return [b[1] for row in notify.service_buttons(names) for b in row]

    def test_single_service_has_no_all_button(self):
        self.assertEqual(self.data(["sshd"]), ["restart:sshd"])

    def test_several_with_all(self):
        self.assertEqual(self.data(["a", "b"]), ["restart:a", "restart:b", f"restart:*:{restarts.digest(['a', 'b'])}"])

    def test_too_long_name_only_through_all(self):
        self.assertEqual(self.data(["x" * 60]), [f"restart:*:{restarts.digest(['x' * 60])}"])

    def test_many_services_are_capped(self):
        data = self.data([f"s{i}" for i in range(30)])
        self.assertEqual(len(data), notify.MAX_SERVICE_BUTTONS + 1)
        self.assertTrue(data[-1].startswith("restart:*:"))
        self.assertLessEqual(len(data[-1]), notify.MAX_CALLBACK_BYTES)


class SystemctlTest(unittest.TestCase):
    def test_start_one_by_one_without_waiting(self):
        with mock.patch.object(restarts.subprocess, "run", return_value=done()) as run:
            self.assertEqual(restarts.start(["sshd", "postfix"]), {})
        self.assertEqual([c.args[0] for c in run.call_args_list],
                         [["systemctl", "restart", "--no-block", "--", "sshd"],
                          ["systemctl", "restart", "--no-block", "--", "postfix"]])

    def test_rejected_service_does_not_stop_the_rest(self):
        with mock.patch.object(restarts.subprocess, "run",
                               side_effect=[done(rc=1, stderr="Unit masked"), done()]) as run:
            self.assertEqual(restarts.start(["a", "b"]), {"a": "Unit masked"})
        self.assertEqual(run.call_count, 2)

    def test_start_errors(self):
        with mock.patch.object(restarts.subprocess, "run", side_effect=OSError("kaputt")):
            self.assertEqual(restarts.start(["x"]), {"x": "kaputt"})
        # Zeitlimit: Der Auftrag kann angenommen sein, also weiter verfolgen statt als abgewiesen melden
        with mock.patch.object(restarts.subprocess, "run", side_effect=subprocess.TimeoutExpired("systemctl", 60)):
            self.assertEqual(restarts.start(["x"]), {})

    def test_pending_when_unknown(self):
        with mock.patch.object(restarts.subprocess, "run", return_value=done(rc=1)):
            self.assertIsNone(restarts.pending(["sshd"]))
        with mock.patch.object(restarts.subprocess, "run", side_effect=OSError("kaputt")):
            self.assertIsNone(restarts.pending(["sshd"]))

    def test_pending(self):
        jobs = "123 sshd.service restart running\n124 foo.service start waiting\n"
        with mock.patch.object(restarts.subprocess, "run", return_value=done(jobs)):
            self.assertEqual(restarts.pending(["sshd", "postfix"]), ["sshd"])
            self.assertEqual(restarts.pending(["postfix"]), [])

    def test_pending_without_services_asks_nothing(self):
        with mock.patch.object(restarts.subprocess, "run") as run:
            self.assertEqual(restarts.pending([]), [])
        run.assert_not_called()

    def test_states(self):
        # is-active endet mit Fehlercode, sobald ein Dienst nicht aktiv ist
        with mock.patch.object(restarts.subprocess, "run", return_value=done("active\nfailed\n", rc=3)):
            self.assertEqual(restarts.states(["a", "b"]), {"a": "active", "b": "failed"})
        with mock.patch.object(restarts.subprocess, "run", return_value=done("")):
            self.assertEqual(restarts.states(["a"]), {"a": "unknown"})
