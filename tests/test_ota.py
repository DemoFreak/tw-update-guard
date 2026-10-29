import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from twguard import ota, state
from twguard.config import Config, Update


def git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


class CheckRemoteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.origin, self.work, self.server = root / "origin.git", root / "work", root / "server"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.work)], check=True,
                       capture_output=True)
        self.commit("erster Stand")
        git(self.work, "push", "-q", "origin", "HEAD:main")
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.server)], check=True)
        self.upd = Update(repo=str(self.server))

    def commit(self, message):
        (self.work / "datei.txt").write_text(message)
        git(self.work, "add", "datei.txt")
        git(self.work, "commit", "-q", "-m", message)

    def test_up_to_date(self):
        self.assertEqual(ota.check_remote(self.upd).commits, [])

    def test_new_commits(self):
        self.commit("Neue Funktion")
        git(self.work, "push", "-q", "origin", "HEAD:main")
        pending = ota.check_remote(self.upd)
        self.assertEqual(len(pending.commits), 1)
        self.assertIn("Neue Funktion", pending.commits[0])
        self.assertEqual(pending.target, git(self.work, "rev-parse", "HEAD"))

    def test_local_changes_refused(self):
        (self.server / "datei.txt").write_text("lokal geändert")
        with self.assertRaisesRegex(ota.UpdateError, "lokale Änderungen"):
            ota.check_remote(self.upd)

    def test_not_configured(self):
        with self.assertRaisesRegex(ota.UpdateError, "nicht eingerichtet"):
            ota.check_remote(Update())


class SelfUpdateTest(CheckRemoteTest):
    """Ablauf von self-update mit Attrappen für Tests, install.sh, systemctl und Telegram."""

    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name)
        self.cfg = Config(token="x", chat_id=-100, hostname="server1", state_dir=root / "state",
                          update=self.upd)
        self.installed, self.previous = root / "twguard", root / "twguard.prev"
        self.installed.write_text("alt")
        self.units = root / "units"
        self.units.mkdir()
        (self.units / "twguard-bot.service").write_text("alte Unit")
        self.sent = []
        tg = mock.Mock()
        def send(text, **kw):
            self.sent.append(text)
            return {"message_id": len(self.sent), "chat": {"id": -100}}

        tg.send.side_effect = send
        self.systemctl = mock.patch.object(ota, "_systemctl", return_value=0).start()
        for name, value in (("INSTALLED", self.installed), ("PREVIOUS", self.previous), ("UNIT_DIR", self.units),
                            ("START_TIMEOUT", 0.5), ("Telegram", mock.Mock(return_value=tg))):
            mock.patch.object(ota, name, value).start()
        self.run_cmd = mock.patch.object(ota, "_run").start()
        mock.patch.object(ota, "_restart_bot").start()
        mock.patch.object(ota.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)
        self.commit("Neue Funktion")
        git(self.work, "push", "-q", "origin", "HEAD:main")
        self.target = ota.check_remote(self.upd).target
        self.old = git(self.server, "rev-parse", "HEAD")

    def test_success(self):
        # Der neue Bot meldet sich (install.sh hat ihn gestartet)
        def install(cmd, *args, **kw):
            if cmd == ["./install.sh"]:
                self.installed.write_text("neu")
                with state.locked(self.cfg.state_dir) as st:
                    st.bot_commit = self.target[:12]
        self.run_cmd.side_effect = install
        self.assertEqual(ota.self_update(self.cfg, self.target), 0)
        self.assertEqual(git(self.server, "rev-parse", "HEAD"), self.target)
        self.assertEqual(self.installed.read_text(), "neu")
        self.assertEqual(self.previous.read_text(), "alt")

    def test_failing_tests_keep_old_version(self):
        self.run_cmd.side_effect = ota.UpdateError("1 Test fehlgeschlagen")
        self.assertEqual(ota.self_update(self.cfg, self.target), 1)
        self.assertEqual(git(self.server, "rev-parse", "HEAD"), self.old)
        self.assertEqual(self.installed.read_text(), "alt")
        self.assertIn("fehlgeschlagen", self.sent[0])

    def test_bot_not_starting_rolls_back(self):
        def install(cmd, *args, **kw):
            if cmd == ["./install.sh"]:
                self.installed.write_text("neu, startet nicht")
                (self.units / "twguard-bot.service").write_text("kaputte Unit")
        self.run_cmd.side_effect = install
        self.assertEqual(ota.self_update(self.cfg, self.target), 1)
        self.assertEqual(self.installed.read_text(), "alt")
        self.assertEqual((self.units / "twguard-bot.service").read_text(), "alte Unit")
        self.systemctl.assert_any_call("daemon-reload")
        self.assertEqual(git(self.server, "rev-parse", "HEAD"), self.old)
        self.assertIn("nicht gestartet", self.sent[0])
        self.assertIn("alte Version wieder eingespielt", self.sent[0])

    def test_incomplete_rollback_reported(self):
        def install(cmd, *args, **kw):
            if cmd == ["./install.sh"]:
                raise ota.UpdateError("install.sh kaputt")
        self.run_cmd.side_effect = install
        self.systemctl.return_value = 1
        self.assertEqual(ota.self_update(self.cfg, self.target), 1)
        self.assertIn("Zurückrollen unvollständig", self.sent[0])
        self.assertIn("daemon-reload", self.sent[0])

    # Die Tests der Basisklasse hier nicht wiederholen
    test_up_to_date = test_new_commits = test_local_changes_refused = test_not_configured = None


class StartupReportTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(hostname="server1")

    def test_started_with(self):
        self.assertTrue(ota.started_with("abcdef1234567890", "abcdef123456"))
        self.assertTrue(ota.started_with("abcdef1234567890", "abcdef123456+lokal"))
        self.assertFalse(ota.started_with("abcdef1234567890", "fff"))
        self.assertFalse(ota.started_with("abcdef1234567890", ""))

    @mock.patch.object(ota, "COMMIT", "abcdef123456")
    def test_success_reported_by_new_bot(self):
        st = state.State(update_request={"commit": "abcdef1234567890", "at": 100.0})
        text, _ = ota.startup_report(self.cfg, st, now=200.0)
        self.assertIn("läuft jetzt", text)
        self.assertEqual(st.update_request, {})

    def test_waits_while_update_runs(self):
        st = state.State(update_request={"commit": "f" * 40, "at": 100.0})
        self.assertIsNone(ota.startup_report(self.cfg, st, now=200.0))
        self.assertTrue(st.update_request)

    def test_stuck_update_reported(self):
        st = state.State(update_request={"commit": "f" * 40, "at": 100.0})
        text, _ = ota.startup_report(self.cfg, st, now=100.0 + ota.STUCK_AFTER + 1)
        self.assertIn("keine Rückmeldung", text)
        self.assertEqual(st.update_request, {})


if __name__ == "__main__":
    unittest.main()
