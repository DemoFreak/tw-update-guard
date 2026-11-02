import subprocess
import unittest
from unittest import mock

import testlang  # noqa: F401
from twguard import notify, restarts, zypper
from twguard.config import Services
from twguard.state import State

EXCLUDE = Services().exclude


def done(stdout="", rc=0, stderr=""):
    return subprocess.CompletedProcess([], rc, stdout, stderr)


class ZypperTest(unittest.TestCase):
    def test_names_only(self):
        output = "sshd\npostfix\n\nHinweis: Bei Ausführung ohne root\nuser@1000\ntwguard-check\nsshd\n"
        with mock.patch.object(zypper.subprocess, "run", return_value=done(output)):
            self.assertEqual(zypper.services_needing_restart(), ["postfix", "sshd", "user@1000"])

    def test_failure_is_not_an_empty_list(self):
        with mock.patch.object(zypper.subprocess, "run", return_value=done(rc=1)):
            self.assertIsNone(zypper.services_needing_restart())


class NoteTest(unittest.TestCase):
    def test_split_by_exclude_list(self):
        found = ["dbus-broker", "postfix", "sshd", "systemd-logind", "user@1000"]
        self.assertEqual(restarts.split(found, EXCLUDE),
                         (["postfix", "sshd"], ["dbus-broker", "systemd-logind", "user@1000"]))

    def test_opens_issue_with_skipped_named(self):
        st = State()
        restarts.note(st, ["dbus", "sshd"], EXCLUDE)
        self.assertEqual(st.restart_services, ["sshd"])
        self.assertIn("sshd", st.issues["services"].detail)
        self.assertIn("dbus", st.issues["services"].detail)

    def test_only_excluded_opens_nothing(self):
        st = State()
        restarts.note(st, ["dbus"], EXCLUDE)
        self.assertEqual(st.issues, {})

    def test_exclude_all_turns_offer_off(self):
        st = State()
        restarts.note(st, ["sshd"], ["*"])
        self.assertEqual(st.issues, {})

    def test_resolved_when_gone(self):
        st = State()
        restarts.note(st, ["sshd"], EXCLUDE)
        restarts.note(st, [], EXCLUDE)
        self.assertEqual(st.issues, {})
        self.assertEqual(st.restart_services, [])

    def test_new_service_notifies_again_but_shrinking_does_not(self):
        st = State()
        restarts.note(st, ["a", "b"], EXCLUDE)
        st.issues["services"].last_notified = 5.0
        restarts.note(st, ["a"], EXCLUDE)
        self.assertEqual(st.issues["services"].last_notified, 5.0)
        restarts.note(st, ["a", "c"], EXCLUDE)
        self.assertEqual(st.issues["services"].last_notified, 0.0)

    def test_ignored_while_restart_runs(self):
        st = State(restart_request={"services": ["sshd"]})
        restarts.note(st, ["sshd"], EXCLUDE)
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
