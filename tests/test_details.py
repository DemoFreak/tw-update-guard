import unittest
from html.parser import HTMLParser
from pathlib import Path

import testlang  # noqa: F401
from twguard import details
from twguard.details import file_html, file_name, pages, snapshot, version_change
from twguard.zypper import Plan, Solvable, parse_plan


def plan_of(*items) -> dict:
    return snapshot(Plan(sections={"to-upgrade": [Solvable(n, new, edition_old=old) for n, old, new in items]},
                         packages_to_change=len(items)))


class Balance(HTMLParser):
    def __init__(self):
        super().__init__()
        self.open = []

    def handle_starttag(self, tag, attrs):
        self.open.append(tag)

    def handle_endtag(self, tag):
        assert self.open.pop() == tag


class VersionChangeTest(unittest.TestCase):
    def test_marks_changed_part_only(self):
        self.assertEqual(version_change("1.23.6-1699.4.pm.20", "1.23.6-1699.4.pm.21"),
                         "1.23.6-1699.4.pm.<b>20</b> → <b>21</b>")

    def test_does_not_split_numbers(self):
        self.assertEqual(version_change("1.19-2.1", "1.20-1.1"), "1.<b>19-2.1</b> → <b>20-1.1</b>")

    def test_without_old_version(self):
        self.assertEqual(version_change("", "1.0-1.1"), "1.0-1.1")

    def test_escapes(self):
        self.assertIn("&lt;", version_change("1.<2", "1.<3"))


class FileTest(unittest.TestCase):
    def test_marks_jump_and_rebuild_and_escapes(self):
        plan = snapshot(Plan(sections={"to-upgrade": [
            Solvable("neu", "2.0-1", edition_old="1.0-1", repository="oss"),
            Solvable("bau<x>", "1.0-2", edition_old="1.0-1")]}, packages_to_change=2))
        text = file_html(plan, "host<1>", 0)
        self.assertIn('class="row jump"', text)
        self.assertIn('class="row rebuild"', text)
        self.assertIn('<b class="new">2.0-1</b>', text)
        self.assertIn("bau&lt;x&gt;", text)
        self.assertIn("host&lt;1&gt;", text)
        self.assertIn("davon 1 mit neuer Version", text)
        self.assertNotIn("<script", text)

    def test_contains_all_packages_of_real_plan(self):
        xml = (Path(__file__).parent / "fixtures" / "upgrade-real.xml").read_text()
        plan = snapshot(parse_plan(xml))
        text = file_html(plan, "host", 0)
        self.assertTrue(all(row[1] in text for row in plan["rows"]))

    def test_file_name_is_safe(self):
        self.assertRegex(file_name("a b/c", 0), r"^pakete-a_b_c-\d{8}-\d{4}\.html$")


class PagesTest(unittest.TestCase):
    def test_by_name_is_sorted(self):
        text = pages(plan_of(("zlib", "1-1", "1-2"), ("attr", "1-1", "1-2")), "name")[0]
        self.assertLess(text.index("attr"), text.index("zlib"))
        self.assertIn("attr\n  1-<b>1</b> → <b>2</b>", text)

    def test_by_version_groups_jumps_before_rebuilds(self):
        text = pages(plan_of(("a", "1.0-1", "1.0-2"), ("b", "1.0-1", "1.0-2"), ("z", "1.0-1", "2.0-1")), "version")[0]
        self.assertIn("a, b", text)
        self.assertLess(text.index("z"), text.index("nur neu gebaut"))
        self.assertLess(text.index("nur neu gebaut"), text.index("a, b"))

    def test_long_list_keeps_every_package_on_pages(self):
        names = [f"paket-{i:03}" for i in range(300)]
        plan = plan_of(*[(n, f"1.0.{i}-1", f"1.0.{i}-2") for i, n in enumerate(names)])
        for view in details.VIEWS:
            result = pages(plan, view)
            self.assertGreater(len(result), 1)
            self.assertTrue(all(len(p) <= details.PAGE_LIMIT for p in result))
            self.assertTrue(all("Aktualisiert (300)" in p for p in result))
            joined = "\n".join(result)
            self.assertTrue(all(n in joined for n in names))

    def test_large_group_is_split(self):
        plan = plan_of(*[(f"paket-mit-recht-langem-namen-{i:03}", "1-1", "1-2") for i in range(300)])
        self.assertTrue(all(len(p) <= details.PAGE_LIMIT for p in pages(plan, "version")))

    def test_tags_are_balanced_on_real_plan(self):
        xml = (Path(__file__).parent / "fixtures" / "upgrade-real.xml").read_text()
        for view in details.VIEWS:
            for page in pages(snapshot(parse_plan(xml)), view):
                parser = Balance()
                parser.feed(page)
                self.assertEqual(parser.open, [])

    def test_problems_are_shown(self):
        self.assertIn("Problem &lt;x&gt;", pages({"summary": "0 Pakete", "rows": [], "problems": ["Problem <x>"]}, "name")[0])
