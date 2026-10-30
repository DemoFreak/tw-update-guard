import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import testlang  # noqa: F401
from twguard import cli, config
from twguard.i18n import set_language
from twguard.telegram import Telegram, TelegramError


ROOT = Path(__file__).parent.parent


def run_ids(**patches):
    err = io.StringIO()
    out = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out), \
         mock.patch.object(cli, "bot_running", return_value=patches.pop("running", False)), \
         mock.patch.object(Telegram, "get_updates", **patches) as get_updates:
        rc = cli.cmd_telegram_ids(config.Config(language="de"))
    return rc, out.getvalue(), err.getvalue(), get_updates


class TelegramIdsTest(unittest.TestCase):
    def test_running_bot_is_reported_without_call(self):
        rc, _, err, get_updates = run_ids(running=True, return_value=[])
        self.assertEqual(rc, 1)
        self.assertIn("systemctl stop twguard-bot", err)
        get_updates.assert_not_called()

    def test_conflict_is_reported(self):
        error = TelegramError("getUpdates: Conflict: terminated by other getUpdates request")
        rc, _, err, _ = run_ids(side_effect=error)
        self.assertEqual(rc, 1)
        self.assertIn("systemctl stop twguard-bot", err)

    def test_timeout_retried(self):
        updates = [{"message": {"chat": {"id": -42, "type": "group", "title": "Server"},
                                "from": {"id": 7, "username": "admin"}, "text": "/start"}}]
        rc, out, err, get_updates = run_ids(side_effect=[TelegramError("getUpdates: timed out"), updates])
        self.assertEqual(rc, 0)
        self.assertEqual(get_updates.call_count, 2)
        self.assertIn("Chat -42", out)
        self.assertIn("Versuch 1", err)

    def test_cli_prints_telegram_error_without_traceback(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
             mock.patch.object(cli, "_config", return_value=config.Config(language="de")), \
             mock.patch.object(cli, "bot_running", return_value=False), \
             mock.patch.object(Telegram, "get_updates", side_effect=TelegramError("getUpdates: timed out")):
            rc = cli.main(["telegram-ids"])
        self.assertEqual(rc, 1)
        self.assertIn("Telegram: getUpdates: timed out", err.getvalue())


class UsageTest(unittest.TestCase):
    def test_placeholders_are_marked(self):
        for command, usage in (("self-update", "[<commit>]"), ("classify", "<file>")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
                cli.main([command, "--help"])
            self.assertIn(usage, out.getvalue())


class ConfigDiffTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "config.toml"

    def diff(self, text):
        self.path.write_text(text)
        return config.changed_values(self.path)

    def test_only_differences_remain(self):
        changed = self.diff('[telegram]\ntoken = "t"\nhelp_full = false\n[general]\nmode = "observe"\nlanguage = "de"\n'
                            '[update]\nbranch = "main"\n')
        self.assertEqual(changed, {"telegram": {"token": "t"}, "general": {"language": "de"}})

    def test_full_example_shortens_to_required_values(self):
        changed = config.changed_values(ROOT / "config.example.toml")
        self.assertEqual(list(changed), ["telegram"])
        self.assertEqual(set(changed["telegram"]), {"token", "chat_id", "allowed_user_ids"})

    def test_output_loads_to_same_configuration(self):
        original = ('[telegram]\ntoken = "a\\"b"\nchat_id = -5\nallowed_user_ids = [1, 2]\nhelp_full = true\n'
                    '[general]\nhostname = "h"\nmode = "auto"\n[rules]\nwatch = ["x:2"]\n'
                    '[escalation]\nladder = [[0, 12], [48, 1.5]]\n[briefing]\nat = ""\n')
        before = self.diff(original)
        self.assertEqual(config.changed_values(self.path), before)
        full = config.load(self.path)
        self.path.write_text(config.to_toml(before))
        self.assertEqual(config.load(self.path), full)

    def test_command_prints_differences(self):
        self.addCleanup(set_language, "de")  # main() übernimmt die Sprache aus der Datei
        self.path.write_text('[telegram]\ntoken = "t"\n')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--config", str(self.path), "config-diff"]), 0)
        self.assertEqual(out.getvalue(), '[telegram]\ntoken = "t"\n')

    def test_minimal_template_is_valid_and_short(self):
        cfg = config.load(ROOT / "config.minimal.toml")
        self.assertEqual(cfg.mode, "observe")
        self.assertEqual(cfg.rules, config.Config().rules)
        self.assertEqual(set(config.changed_values(ROOT / "config.minimal.toml")), {"telegram"})


if __name__ == "__main__":
    unittest.main()
