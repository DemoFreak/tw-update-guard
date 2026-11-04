import time
import unittest
from html.parser import HTMLParser

import testlang  # noqa: F401
from twguard import panel
from twguard.config import Config
from twguard.state import State

# Was Telegram in HTML erlaubt und hier vorkommt; jedes muss wieder geschlossen werden
TAGS = {"b", "i", "pre", "code"}


class Balance(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.open: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in TAGS:
            self.errors.append(f"unbekannt: <{tag}>")
        self.open.append(tag)

    def handle_endtag(self, tag):
        if not self.open or self.open[-1] != tag:
            self.errors.append(f"</{tag}> ohne passendes <{tag}>")
        else:
            self.open.pop()


def check_html(test: unittest.TestCase, text: str) -> None:
    parser = Balance()
    parser.feed(text)
    parser.close()
    test.assertEqual(parser.errors, [])
    test.assertEqual(parser.open, [], "offene Tags am Ende")


class RenderLimitTest(unittest.TestCase):
    """F23: Auch eine übervolle Zustandsnachricht bleibt gültiges HTML und nennt jeden Punkt."""

    def setUp(self):
        self.cfg = Config(language="de", token="x", chat_id=-100, hostname="server1")
        self.now = time.time()

    def crowded(self, detail=600) -> State:
        st = State()
        for kind in ("error", "apply-failed", "stale"):
            st.open_issue(kind, "Fehler <x> & \"y\"\n" + "z" * detail, now=self.now - 3600)
        st.open_issue("manual", "• paket <neu>\n" * 40, fingerprint="abc", approvable=True, now=self.now - 3600)
        st.update_offer = {"target": "b" * 40, "label": "v9.9.9 (bbbbbbbb)", "running": "v0.9.5 (aaaaaaaa)",
                           "commits": [f"{i:07x} " + "<Commit> & " * 13 for i in range(5)], "at": self.now}
        st.lingering = {f"dienst{i}": [{"pid": 1000 + i, "user": "root", "command": "/usr/bin/x" * 20}] * 5
                        for i in range(10)}
        return st

    def test_crowded_message_is_valid_html_within_limit(self):
        shown = panel.render(self.cfg, self.crowded(), self.now)
        self.assertLessEqual(len(shown.text), panel.TEXT_LIMIT)
        check_html(self, shown.text)
        for name in ("Update-Prüfung schlägt fehl", "Update ist fehlgeschlagen", "Keine Update-Prüfung mehr",
                     "Update braucht Freigabe"):
            self.assertIn(name, shown.text)

    def test_extreme_details_still_valid(self):
        shown = panel.render(self.cfg, self.crowded(detail=5000), self.now)
        self.assertLessEqual(len(shown.text), panel.TEXT_LIMIT)
        check_html(self, shown.text)

    def test_normal_message_unshortened(self):
        st = State()
        st.open_issue("reboot", "Kernel", now=self.now)
        shown = panel.render(self.cfg, st, self.now)
        self.assertNotIn("gekürzt", shown.text)
        check_html(self, shown.text)

    def test_clip_escaped_never_cuts_an_entity(self):
        for limit in range(5, 40):
            text = panel.clip_escaped("<<<<<<<<<<<<<<<<<<<<", limit)
            self.assertLessEqual(len(text), limit)
            self.assertNotRegex(text, r"&[^;]*$")


if __name__ == "__main__":
    unittest.main()
