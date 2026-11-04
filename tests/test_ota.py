import contextlib
import io
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import testlang  # noqa: F401
from twguard import busy, ota, state
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

    def test_new_commits_carry_target_version(self):
        package = self.work / "src" / "twguard"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text('"""x"""\n\n__version__ = "1.2.3"\n')
        git(self.work, "add", "src")
        git(self.work, "commit", "-q", "-m", "Version 1.2.3")
        git(self.work, "push", "-q", "origin", "HEAD:main")
        self.assertEqual(ota.check_remote(self.upd).version, "1.2.3")

    def test_version_unknown_without_init(self):
        self.commit("ohne Paket")
        git(self.work, "push", "-q", "origin", "HEAD:main")
        self.assertEqual(ota.check_remote(self.upd).version, "")

    def test_local_changes_refused(self):
        (self.server / "datei.txt").write_text("lokal geändert")
        with self.assertRaisesRegex(ota.UpdateError, "lokale Änderungen"):
            ota.check_remote(self.upd)

    def test_not_configured(self):
        with self.assertRaisesRegex(ota.UpdateError, "nicht eingerichtet"):
            ota.check_remote(Update())


class SelfUpdateTest(CheckRemoteTest):
    """Ablauf von self-update mit Attrappen für Tests, install.sh und systemctl."""

    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name)
        self.cfg = Config(language="de", token="x", chat_id=-100, hostname="server1", state_dir=root / "state",
                          update=self.upd)
        self.installed, self.previous = root / "twguard", root / "twguard.prev"
        self.installed.write_text("alt")
        self.units = root / "units"
        self.units.mkdir()
        (self.units / "twguard-bot.service").write_text("alte Unit")
        self.systemctl = mock.patch.object(ota, "_systemctl", return_value=0).start()
        for name, value in (("INSTALLED", self.installed), ("PREVIOUS", self.previous), ("UNIT_DIR", self.units),
                            ("START_TIMEOUT", 0.5)):
            mock.patch.object(ota, name, value).start()
        self.run_cmd = mock.patch.object(ota, "_run").start()
        mock.patch.object(ota, "_restart_bot").start()
        mock.patch.object(ota.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)
        self.commit("Neue Funktion")
        git(self.work, "push", "-q", "origin", "HEAD:main")
        self.target = ota.check_remote(self.upd).target
        self.old = git(self.server, "rev-parse", "HEAD")

    @property
    def sent(self) -> list[str]:
        """Ergebnisse gehen als Ereignis in den Zustand; zustellen tut sie der Bot."""
        return [e["text"] for e in state.load(self.cfg.state_dir / "state.json").outbox]

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

    def test_failure_releases_only_own_request(self):
        own = state.holder()
        for runner, kept in ((own, False), ({"pid": 1, "boot": "x", "at": 1.0}, True)):
            with state.locked(self.cfg.state_dir) as st:
                st.update_request = {"commit": self.target, "at": time.time(), "runner": own}
            self.run_cmd.side_effect = None
            def other_takes_over(cmd, *args, runner=runner, **kw):
                with state.locked(self.cfg.state_dir) as st:
                    st.update_request["runner"] = runner
                raise ota.UpdateError("1 Test fehlgeschlagen")
            self.run_cmd.side_effect = other_takes_over
            self.assertEqual(ota.self_update(self.cfg, self.target), 1)
            with state.locked(self.cfg.state_dir) as st:
                self.assertEqual(bool(st.update_request), kept)

    def test_failing_tests_keep_old_version(self):
        self.run_cmd.side_effect = ota.UpdateError("1 Test fehlgeschlagen")
        self.assertEqual(ota.self_update(self.cfg, self.target), 1)
        self.assertEqual(git(self.server, "rev-parse", "HEAD"), self.old)
        self.assertEqual(self.installed.read_text(), "alt")
        self.assertIn("fehlgeschlagen", self.sent[0])

    def test_missing_installer_rolls_back(self):
        def install(cmd, *args, **kw):
            if cmd == ["./install.sh"]:
                self.installed.write_text("halb neu")
                raise FileNotFoundError("install.sh")
        self.run_cmd.side_effect = install
        self.assertEqual(ota.self_update(self.cfg, self.target), 1)
        self.assertEqual(git(self.server, "rev-parse", "HEAD"), self.old)
        self.assertEqual(self.installed.read_text(), "alt")
        self.assertIn("fehlgeschlagen", self.sent[0])

    def test_failed_backup_rolls_back(self):
        with mock.patch.object(ota.shutil, "copy2", side_effect=OSError("Platte voll")):
            self.assertEqual(ota.self_update(self.cfg, self.target), 1)
        self.assertEqual(git(self.server, "rev-parse", "HEAD"), self.old)
        self.assertIn("Platte voll", self.sent[0])

    def test_run_turns_process_errors_into_update_error(self):
        mock.patch.stopall()
        with self.assertRaises(ota.UpdateError):
            ota._run(["/gibt/es/nicht"], self.tmp.name, 5)

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


class CommandTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = Config(language="de", state_dir=Path(tmp.name), update=Update(repo="/r"))
        self.pending = ota.Pending("a" * 40, "b" * 40, ["bbb neu"])
        for name, target in (("check", "check_remote"), ("update", "self_update")):
            patcher = mock.patch.object(ota, target)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        self.check.return_value = self.pending
        self.update.return_value = 0
        patcher = mock.patch.object(busy, "shutdown", return_value=busy.Shutdown("none"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_command(self, commit=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ota.command(self.cfg, commit)
        return rc, out.getvalue(), err.getvalue()

    def test_without_commit_takes_latest(self):
        rc, out, _ = self.run_command()
        self.assertEqual(rc, 0)
        self.update.assert_called_once_with(self.cfg, "b" * 40)
        self.assertIn("läuft jetzt mit bbbbbbbb.", out)

    def test_up_to_date_does_nothing(self):
        self.check.return_value = ota.Pending("a" * 40, "a" * 40, [])
        rc, out, _ = self.run_command()
        self.assertEqual(rc, 0)
        self.update.assert_not_called()
        self.assertIn("aktuell", out)

    def test_unknown_commit_is_rejected_without_update(self):
        with mock.patch.object(ota, "_git", side_effect=ota.UpdateError("x")):
            rc, _, err = self.run_command("commit")
        self.assertEqual(rc, 2)
        self.update.assert_not_called()
        self.assertIn("Unbekannter Commit: commit", err)

    def test_given_commit_is_resolved_to_full_hash(self):
        with mock.patch.object(ota, "_git", return_value="c" * 40):
            self.run_command("ccc")
        self.update.assert_called_once_with(self.cfg, "c" * 40)

    def bot_request(self, **extra):
        with state.locked(self.cfg.state_dir) as st:
            st.update_request = {"commit": "d" * 40, "chat_id": -100, "message_id": 7, "at": time.time(), **extra}

    def test_unit_started_by_bot_goes_straight_to_update(self):
        self.bot_request()
        def seen(cfg, commit):
            with state.locked(cfg.state_dir) as st:
                self.assertEqual(st.update_request["runner"]["pid"], os.getpid())
                self.assertEqual(st.update_request["message_id"], 7)
            return 0
        self.update.side_effect = seen
        self.assertEqual(self.run_command("d" * 40)[0], 0)
        self.update.assert_called_once_with(self.cfg, "d" * 40)
        self.check.assert_not_called()

    def test_second_runner_of_bot_update_refused(self):
        # F20: Ein zweiter Aufruf während des ersten darf weder einspielen noch dessen Auftrag lösen
        self.bot_request()
        inner = []
        def first(cfg, commit):
            for again in ("d" * 40, "e" * 40, ""):
                with mock.patch.object(ota, "_git", return_value=again or "f" * 40):
                    inner.append(self.run_command(again))
            with state.locked(cfg.state_dir) as st:
                self.assertEqual(st.update_request["runner"]["pid"], os.getpid())
                self.assertEqual(st.update_request["message_id"], 7)
            return 0
        self.update.side_effect = first
        self.assertEqual(self.run_command("d" * 40)[0], 0)
        self.update.assert_called_once()
        self.assertEqual([rc for rc, _, _ in inner], [1, 1, 1])
        self.assertTrue(all("läuft bereits" in err or "Update von twguard läuft" in err for _, _, err in inner))

    def test_bot_update_claimed_only_once(self):
        # Nach einem Absturz des Ausführers übernimmt niemand den Auftrag des Bots; derselbe Commit von Hand
        # ist ein eigenes Update mit eigener Reservierung
        self.bot_request(runner={"pid": 999999999, "boot": state.boot_id(), "at": 1.0})
        def seen(cfg, commit):
            with state.locked(cfg.state_dir) as st:
                self.assertTrue(st.update_request["console"])
                self.assertNotIn("message_id", st.update_request)
            return 0
        self.update.side_effect = seen
        with mock.patch.object(ota, "_git", return_value="d" * 40):
            self.assertEqual(self.run_command("d" * 40)[0], 0)
        self.update.assert_called_once()

    def test_console_update_after_crashed_bot_update(self):
        self.bot_request(runner={"pid": 999999999, "boot": state.boot_id(), "at": 1.0})
        self.assertEqual(self.run_command()[0], 0)
        self.update.assert_called_once_with(self.cfg, "b" * 40)

    def test_second_console_runner_refused(self):
        inner = []
        def first(cfg, commit):
            inner.append(self.run_command())
            return 0
        self.update.side_effect = first
        self.assertEqual(self.run_command()[0], 0)
        self.update.assert_called_once()
        self.assertEqual(inner[0][0], 1)
        self.assertIn("Update von twguard läuft", inner[0][2])

    def test_without_commit_while_update_runs(self):
        self.bot_request()
        rc, _, err = self.run_command()
        self.assertEqual(rc, 1)
        self.update.assert_not_called()
        self.assertIn("läuft bereits", err)

    def test_console_update_reserves_like_the_bot(self):
        # Sonst liefe ein Update von Hand mitten in ein Einspielen oder einen Dienst-Neustart
        def seen(cfg, commit):
            with state.locked(cfg.state_dir) as st:
                self.assertEqual(st.update_request["commit"], commit)
                self.assertTrue(st.update_request["console"])
            return 0
        self.update.side_effect = seen
        self.assertEqual(self.run_command()[0], 0)
        self.update.assert_called_once()

    def test_console_update_refused_while_twguard_applies(self):
        with state.locked(self.cfg.state_dir) as st:
            st.applying = state.holder()
        rc, _, err = self.run_command()
        self.assertEqual(rc, 1)
        self.update.assert_not_called()
        self.assertIn("twguard spielt gerade ein", err)
        with state.locked(self.cfg.state_dir) as st:
            self.assertEqual(st.update_request, {})

    def test_second_console_update_refused(self):
        with state.locked(self.cfg.state_dir) as st:
            st.update_request = {"commit": "d" * 40, "at": time.time(), "console": True, "runner": state.holder()}
        rc, _, err = self.run_command()
        self.assertEqual(rc, 1)
        self.update.assert_not_called()
        self.assertIn("Update von twguard läuft", err)

    def test_failed_update_is_reported(self):
        self.update.return_value = 1
        rc, out, _ = self.run_command()
        self.assertEqual(rc, 1)
        self.assertIn("fehlgeschlagen", out)


class StartupReportTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(language="de", hostname="server1")

    def test_started_with(self):
        self.assertTrue(ota.started_with("abcdef1234567890", "abcdef123456"))
        self.assertTrue(ota.started_with("abcdef1234567890", "abcdef123456+lokal"))
        self.assertFalse(ota.started_with("abcdef1234567890", "fff"))
        self.assertFalse(ota.started_with("abcdef1234567890", ""))

    @mock.patch.object(ota, "COMMIT", "abcdef123456")
    @mock.patch.object(ota, "__version__", "0.9.3")
    def test_success_reported_by_new_bot(self):
        st = state.State(update_request={"commit": "abcdef1234567890", "at": 100.0})
        text = ota.startup_report(self.cfg, st, now=200.0)
        self.assertIn("läuft jetzt mit v0.9.3 (abcdef12)", text)
        self.assertEqual([(e["text"], e["loud"]) for e in st.outbox], [(text, False)])
        self.assertEqual(st.update_request, {})

    @mock.patch.object(ota, "COMMIT", "abcdef123456")
    def test_console_success_stays_on_console(self):
        st = state.State(update_request={"commit": "abcdef1234567890", "at": 100.0, "console": True})
        self.assertIsNone(ota.startup_report(self.cfg, st, now=200.0))
        self.assertEqual(st.update_request, {})
        self.assertEqual(st.outbox, [])

    def test_waits_while_update_runs(self):
        st = state.State(update_request={"commit": "f" * 40, "at": 100.0})
        self.assertIsNone(ota.startup_report(self.cfg, st, now=200.0))
        self.assertTrue(st.update_request)

    def test_stuck_update_reported(self):
        st = state.State(update_request={"commit": "f" * 40, "at": 100.0})
        text = ota.startup_report(self.cfg, st, now=100.0 + ota.STUCK_AFTER + 1)
        self.assertIn("keine Rückmeldung", text)
        self.assertTrue(st.outbox[0]["loud"])
        self.assertEqual(st.update_request, {})


class LabelTest(unittest.TestCase):
    def test_version_and_short_hash(self):
        self.assertEqual(ota.label("abcdef1234567890", "0.9.3"), "v0.9.3 (abcdef12)")

    def test_without_version_only_hash(self):
        self.assertEqual(ota.label("abcdef1234567890"), "abcdef12")

    def test_local_suffix_kept(self):
        self.assertEqual(ota.label("abcdef123456+lokal", "0.9.3"), "v0.9.3 (abcdef12+lokal)")


if __name__ == "__main__":
    unittest.main()
