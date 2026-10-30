import contextlib
import io
import unittest
from unittest import mock

import testlang  # noqa: F401
from twguard import cli, config
from twguard.telegram import Telegram, TelegramError


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


if __name__ == "__main__":
    unittest.main()
