import subprocess
import unittest
from unittest import mock
from pathlib import Path

from twguard import zypper
from twguard.rules import Rules, classify
from twguard.zypper import Plan, Solvable, message_text, parse_plan

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


class ParsePlanTest(unittest.TestCase):
    # Die *-real.xml stammen aus echten zypper-Läufen (siehe fixtures/README.md)

    def test_nothing_to_do(self):
        plan = parse_plan(fixture("empty-real.xml"))
        self.assertTrue(plan.empty)
        self.assertFalse(plan.failed)
        self.assertEqual(plan.packages_to_change, 0)

    def test_upgrade(self):
        plan = parse_plan(fixture("upgrade-real.xml"))
        self.assertEqual(plan.packages_to_change, 5)
        # Muster und Produkte zählen nicht als Pakete
        self.assertEqual(sum(len(items) for items in plan.sections.values()), 5)
        grub = next(s for s in plan.get("to-upgrade") if s.name == "grub2")
        self.assertEqual((grub.edition_old, grub.edition), ("2.16-3.1", "2.16-4.1"))
        self.assertEqual(grub.arch_old, "x86_64")
        self.assertEqual(grub.repository, "openSUSE:repo-oss")

    def test_upgrade_classified_by_real_format(self):
        verdict = classify(parse_plan(fixture("upgrade-real.xml")), Rules())
        self.assertFalse(verdict.auto)
        self.assertEqual(verdict.reasons, ["kritisches Paket: grub2 2.16-3.1 → 2.16-4.1"])

    def test_install(self):
        plan = parse_plan(fixture("in-real.xml"))
        names = [s.name for s in plan.get("to-install")]
        self.assertEqual(names, ["postgresql17", "postgresql17-llvmjit", "postgresql17-server"])
        self.assertEqual(plan.get("to-install")[0].edition, "17.11-3.1")
        self.assertEqual(plan.packages_to_change, 3)
        self.assertEqual(plan.problems, [])  # die Rückfrage "Continue?" ist kein Konflikt

    def test_remove(self):
        plan = parse_plan(fixture("rm-real.xml"))
        self.assertEqual([s.name for s in plan.get("to-remove")], ["nano", "nano-lang"])

    def test_conflict(self):
        plan = parse_plan(fixture("conflict-real.xml"), exit_code=4)
        self.assertEqual(len(plan.problems), 1)
        self.assertIn("conflicts with", plan.problems[0])
        self.assertFalse(plan.failed)
        self.assertFalse(plan.empty)

    def test_upgrade_with_old_edition(self):
        plan = parse_plan(
            '<stream><install-summary packages-to-change="1" need-reboot="1"><to-upgrade>'
            '<solvable type="package" name="bash" edition="5.3.3-1.1" arch="x86_64" '
            'edition-old="5.3.2-2.1" arch-old="x86_64"/></to-upgrade></install-summary></stream>'
        )
        s = plan.get("to-upgrade")[0]
        self.assertEqual(s.edition_old, "5.3.2-2.1")
        self.assertEqual(s.label(), "bash 5.3.2-2.1 → 5.3.3-1.1")
        self.assertTrue(plan.need_reboot)

    def test_error_exit(self):
        plan = parse_plan('<stream><message type="error">System management is locked</message></stream>', 7)
        self.assertTrue(plan.failed)
        self.assertEqual(plan.errors, ["System management is locked"])

    def test_garbage(self):
        plan = parse_plan("kein xml")
        self.assertTrue(plan.failed)
        self.assertTrue(plan.errors)

    def test_fingerprint_depends_on_content_only(self):
        a = parse_plan(fixture("in-real.xml"))
        b = parse_plan(fixture("in-real.xml"))
        c = parse_plan(fixture("rm-real.xml"))
        self.assertEqual(a.fingerprint(), b.fingerprint())
        self.assertNotEqual(a.fingerprint(), c.fingerprint())

    def test_timeouts(self):
        hang = subprocess.TimeoutExpired("zypper", 1)
        with mock.patch.object(zypper.subprocess, "run", side_effect=hang) as run:
            self.assertEqual(zypper.refresh(), ["zypper refresh: keine Antwort nach 1800 s, abgebrochen"])
            plan = zypper.dry_run([])
            self.assertTrue(plan.failed)
            self.assertIn("keine Antwort", plan.errors[0])
            self.assertIsNone(zypper.needs_rebooting())
            self.assertEqual(zypper.services_needing_restart(), [])
            limits = [c.kwargs["timeout"] for c in run.call_args_list]
        self.assertEqual(limits, [1800, 1800, 120, 120])

    def test_apply_without_timeout(self):
        done = subprocess.CompletedProcess([], 0, "<stream/>", "")
        with mock.patch.object(zypper.subprocess, "run", return_value=done) as run:
            zypper.apply([])
        self.assertIsNone(run.call_args.kwargs["timeout"])

    def test_message_text(self):
        xml = '<stream><message type="info">Lade Pakete</message><message type="error">kaputt</message></stream>'
        self.assertEqual(message_text(xml), "Lade Pakete\nkaputt\n")
        self.assertEqual(message_text("kein xml"), "kein xml")

    def test_fingerprint_includes_origin(self):
        def fp(**kw):
            s = Solvable("php8", "8.4.1-1.1", "x86_64", **kw)
            return Plan(sections={"to-upgrade": [s]}).fingerprint()

        base = fp(edition_old="8.4.0-1.1", repository="oss")
        self.assertNotEqual(base, fp(edition_old="7.4.0-1.1", repository="oss"))
        self.assertNotEqual(base, fp(edition_old="8.4.0-1.1", repository="packman"))
        self.assertNotEqual(base, fp(edition_old="8.4.0-1.1", arch_old="i586", repository="oss"))
        self.assertEqual(base, fp(edition_old="8.4.0-1.1", repository="oss"))


if __name__ == "__main__":
    unittest.main()
