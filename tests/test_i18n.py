import ast
import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import testlang  # noqa: F401
from twguard import bot, cli, i18n, messages, notify, rules, zypper
from twguard.bot import Bot
from twguard.config import Config
from twguard.details import file_html, file_name
from twguard.i18n import EN, set_language, tr, trn
from twguard.state import Issue

from fakes import FakeTelegram

SOURCE = Path(bot.__file__).parent


def source_texts() -> set[str]:
    """Alle Texte aus tr()- und trn()-Aufrufen im Code und aus den Tabellen."""
    texts = set(notify.TITLES.values()) | set(rules.SECTION_TITLES.values()) | {bot.HELP, messages.REPLACED, cli.BOT_RUNNING}
    texts |= {text for _, text in bot.MENU} | set(bot.KEY_LABELS)
    for path in SOURCE.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("tr", "trn"):
                count = 2 if node.func.id == "trn" else 1
                texts |= {a.value for a in node.args[:count] if isinstance(a, ast.Constant)}
    return texts


class DefaultLanguageTest(unittest.TestCase):
    def test_english_is_the_default(self):
        self.assertEqual(Config().language, "en")
        out = subprocess.run([sys.executable, "-c", "from twguard.i18n import current; print(current())"],
                             capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=str(SOURCE.parent)))
        self.assertEqual(out.stdout.strip(), "en")


class TableTest(unittest.TestCase):
    def test_every_text_has_a_translation(self):
        # Die Beschriftungen ohne Sprachanteil bleiben gleich
        same = {"📊 Status", "⬆️ Update", "🏷️ Version"}
        self.assertEqual(sorted(source_texts() - EN.keys() - same), [])

    def test_no_unused_translations(self):
        self.assertEqual(sorted(EN.keys() - source_texts()), [])

    def test_placeholders_match(self):
        fields = lambda text: sorted(set(re.findall(r"\{(\w+)\}", text)))
        for german, english in EN.items():
            self.assertEqual(fields(german), fields(english), german)

    def test_log_placeholders_match(self):
        fields = lambda text: re.findall(r"%[sd]|%\.1f", text)
        for german, english in EN.items():
            if "%m" not in german:
                self.assertEqual(fields(german), fields(english), german)

    def test_no_german_left_in_english(self):
        for english in EN.values():
            self.assertNotRegex(english, r"[äöüß„]", english)


class LanguageTest(unittest.TestCase):
    def setUp(self):
        set_language("en")
        self.addCleanup(set_language, "de")

    def test_tr_and_plural(self):
        self.assertEqual(tr("{n} Tagen", n=3), "3 days")
        self.assertEqual(trn("{n} Paket", "{n} Pakete", 1), "1 package")
        self.assertEqual(trn("{n} Paket", "{n} Pakete", 2), "2 packages")

    def test_value_with_braces_is_safe(self):
        self.assertEqual(tr("zypper-Ausgabe nicht lesbar: {error}", error="{x}"), "zypper output not readable: {x}")

    def test_issue_in_english(self):
        issue = Issue(kind="reboot", since=0.0)
        text = notify.format_issue("host", issue, 7200.0, Config().escalation)
        self.assertIn("Reboot pending", text)
        self.assertIn("open for 2 hours", text)
        self.assertEqual(notify.issue_buttons(issue)[0][0][0], "🔁 Reboot")

    def test_file_is_english(self):
        plan = {"summary": "1 package", "rows": [["to-upgrade", "a", "1-1", "2-1", ""]], "problems": []}
        text = file_html(plan, "host", 0)
        self.assertIn('lang="en"', text)
        self.assertIn("Updated (1)", text)
        self.assertTrue(file_name("host", 0).startswith("packages-host-"))


class BotLanguageTest(unittest.TestCase):
    def make_bot(self, language):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(set_language, "de")
        cfg = Config(language=language, state_dir=Path(tmp.name))
        return Bot(cfg, FakeTelegram(), {"id": 1, "username": "b"})

    def test_bot_uses_configured_language(self):
        self.make_bot("en")
        self.assertEqual(i18n.current(), "en")
        self.assertEqual(bot.keyboard_markup()["keyboard"][0][1]["text"], "🔄 Check all")

    def test_keys_of_all_languages_are_understood(self):
        # Die Tastatur kann von einem Bot mit anderer Sprache stammen
        for label, command in (("🔄 Alle prüfen", "checkall"), ("🔄 Check all", "checkall"), ("❓ Help", "help")):
            self.assertEqual(bot.KEYS[label], command)


class CommandLineLanguageTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(set_language, "de")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_dir = Path(tmp.name)
        self.path = self.state_dir / "config.toml"
        self.path.write_text(f'[general]\nlanguage = "en"\nhostname = "h"\nstate_dir = "{tmp.name}"\n')

    def test_log_lines_use_configured_language(self):
        set_language("en")
        with self.assertLogs("twguard.zypper", "INFO") as logs, \
                mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout="", stderr="")):
            zypper.needs_rebooting()
        self.assertIn("starting: zypper", logs.output[0])

    def test_status_uses_configured_language(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--config", str(self.path), "status"]), 0)
        self.assertEqual(out.getvalue(), "h: last successful check never\nNothing open.\n")

    def test_help_uses_configured_language(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["--config", str(self.path), "--help"])
        self.assertIn("show open issues", out.getvalue())

    def test_help_without_config_stays_default(self):
        set_language("en")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["--config", str(self.path) + ".fehlt", "--help"])
        self.assertIn("show open issues", out.getvalue())
