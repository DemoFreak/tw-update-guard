import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import testlang  # noqa: F401
from twguard import busy, state, zypper


def logind(stdout, rc=0):
    return mock.patch.object(busy.subprocess, "run", return_value=subprocess.CompletedProcess([], rc, stdout, ""))


class ShutdownTest(unittest.TestCase):
    def test_nothing_planned(self):
        with logind('(st) "" 18446744073709551615\nb false\n'):
            self.assertEqual(busy.shutdown(), busy.Shutdown("none"))

    def test_reboot_planned(self):
        with logind('(st) "reboot" 1762236787000000\nb false\n'):
            sd = busy.shutdown()
        self.assertEqual((sd.state, sd.mode, sd.at), ("planned", "reboot", 1762236787.0))
        self.assertTrue(sd.reboot)

    def test_poweroff_is_no_reboot(self):
        with logind('(st) "poweroff" 1762236787000000\nb false\n'):
            sd = busy.shutdown()
        self.assertEqual(sd.state, "planned")
        self.assertFalse(sd.reboot)
        self.assertIn("Herunterfahren geplant", busy.planned_text(sd))

    def test_preparing_counts_as_planned(self):
        # Läuft schon: auch ohne geplante Zeit keine Entwarnung
        with logind('(st) "" 18446744073709551615\nb true\n'):
            sd = busy.shutdown()
        self.assertEqual(sd.state, "planned")
        self.assertEqual(busy.planned_text(sd), "Neustart läuft")

    def test_unexpected_answers_are_unknown(self):
        for stdout, rc in (("", 1), ("(st) reboot\nb false\n", 0), ('(st) "" 0\n', 0), ("Kaputt", 0)):
            with logind(stdout, rc):
                self.assertEqual(busy.shutdown().state, "unknown", stdout)
        for error in (OSError("busctl fehlt"), subprocess.TimeoutExpired("busctl", 5)):
            with mock.patch.object(busy.subprocess, "run", side_effect=error):
                self.assertEqual(busy.shutdown().state, "unknown")

    def test_note_shutdown_keeps_only_planned_or_unknown(self):
        st = state.State()
        busy.note_shutdown(st, busy.Shutdown("planned", "reboot", 5.0))
        self.assertEqual(st.shutdown, {"state": "planned", "mode": "reboot", "at": 5.0})
        busy.note_shutdown(st, busy.Shutdown("none"))
        self.assertEqual(st.shutdown, {})


class CheckRunningTest(unittest.TestCase):
    def test_oneshot_counts_while_activating(self):
        results = {"activating": True, "active": True, "inactive": False, "failed": False}
        for output, expected in results.items():
            with mock.patch.object(busy.subprocess, "run",
                                   return_value=subprocess.CompletedProcess([], 3, output + "\n", "")):
                self.assertEqual(busy.check_running(), expected, output)
        with mock.patch.object(busy.subprocess, "run", side_effect=OSError):
            self.assertTrue(busy.check_running())  # im Zweifel läuft er


class BlockersTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.sd = mock.patch.object(busy, "shutdown", return_value=busy.Shutdown("none")).start()
        self.holder = mock.patch.object(zypper, "lock_holder", return_value="").start()
        self.checking = mock.patch.object(busy, "check_running", return_value=False).start()

    def kinds(self, action, st):
        return [kind for kind, _ in busy.blockers(action, st)]

    def test_free(self):
        for action in busy.BLOCKERS:
            self.assertEqual(busy.refusal(action, state.State()), "", action)

    def test_table(self):
        st = state.State(applying=state.holder(), restart_request={"services": ["x"]},
                         update_request={"commit": "c", "at": time.time()})
        self.holder.return_value = "purge-kernels.service (zypper, PID 707)"
        self.checking.return_value = True
        self.sd.return_value = busy.Shutdown("planned", "reboot", time.time() + 60)
        self.assertEqual(self.kinds("check", st), [busy.SHUTDOWN])
        self.assertEqual(self.kinds("approve", st), [busy.SHUTDOWN])
        self.assertEqual(self.kinds("apply", st), [busy.SHUTDOWN, busy.APPLY, busy.RESTART, busy.UPDATE])
        self.assertEqual(self.kinds("reboot", st), [busy.SHUTDOWN, busy.ZYPP, busy.APPLY, busy.RESTART, busy.UPDATE])
        self.assertEqual(self.kinds("restart", st),
                         [busy.SHUTDOWN, busy.ZYPP, busy.APPLY, busy.RESTART, busy.UPDATE, busy.CHECK])
        self.assertEqual(self.kinds("update", st), [busy.SHUTDOWN, busy.APPLY, busy.RESTART, busy.UPDATE])

    def test_unknown_logind_only_blocks_what_changes_the_system(self):
        self.sd.return_value = busy.Shutdown("unknown")
        st = state.State()
        self.assertEqual(self.kinds("check", st), [])
        self.assertEqual(self.kinds("apply", st), [busy.UNKNOWN])
        self.assertIn("logind gibt keine Auskunft", busy.refusal("reboot", st))

    def test_reboot_being_planned_blocks_without_asking_logind(self):
        st = state.State(reboot_planning=state.holder())
        self.assertEqual(self.kinds("apply", st), [busy.SHUTDOWN])
        self.sd.assert_not_called()

    def test_stale_reservations_do_not_block(self):
        # Prozess aus einem anderen Boot, OTA ohne Rückmeldung seit über 15 min
        st = state.State(applying={"pid": 1, "boot": "anderer", "at": 0}, reboot_planning={"pid": 1, "boot": "x"},
                         update_request={"commit": "c", "at": time.time() - busy.UPDATE_MAX_AGE - 1})
        self.assertEqual(self.kinds("reboot", st), [])

    def test_shown_state_follows_logind(self):
        st = state.State()
        self.sd.return_value = busy.Shutdown("planned", "reboot", 5.0)
        busy.blockers("check", st)
        self.assertEqual(st.shutdown["state"], "planned")


class StateMigrationTest(unittest.TestCase):
    def test_old_restart_reports_become_outbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text('{"restart_reports": [{"text": "a"}]}')
            self.assertEqual(state.load(path).outbox, [{"text": "a"}])


if __name__ == "__main__":
    unittest.main()


class SeenTest(unittest.TestCase):
    """Die Anzeige fragt dieselbe Tabelle, aber nach dem beobachteten Stand und ohne Abfragen."""

    def test_seen_queries_nothing(self):
        st = state.State()
        st.zypp_holder = "purge-kernels.service (zypper, PID 707)"
        st.shutdown = {"state": "planned", "mode": "reboot", "at": 0.0}
        st.activity = {"check": {"phase": "queued", "at": time.time()}}
        with mock.patch("subprocess.run") as run, mock.patch.object(zypper, "lock_holder") as holder:
            kinds = {kind for kind, _ in busy.blockers("restart", st, seen=True)}
        run.assert_not_called()
        holder.assert_not_called()
        self.assertEqual(kinds, {busy.SHUTDOWN, busy.ZYPP, busy.CHECK})

    def test_allowed_follows_blockers(self):
        st = state.State()
        st.restart_request = {"at": time.time(), "services": ["postfix"]}
        self.assertFalse(busy.allowed("reboot", st))
        self.assertFalse(busy.allowed("restart:sshd", st))
        self.assertFalse(busy.allowed("update:abc", st))
        self.assertTrue(busy.allowed("approve:abc", st))  # Freigeben ändert nichts, das Einspielen wartet
        self.assertTrue(busy.allowed("check", st))
        self.assertTrue(busy.allowed("later:24", st))
        self.assertTrue(busy.allowed("cancel", st))

    def test_every_button_action_is_in_the_table(self):
        for action in busy.BUTTON_ACTIONS.values():
            self.assertIn(action, busy.BLOCKERS)
