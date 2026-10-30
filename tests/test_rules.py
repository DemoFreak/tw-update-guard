import tomllib
import unittest
from pathlib import Path

from twguard.config import Config
from twguard.rules import Rules, classify, version_key
from twguard.zypper import Plan, Solvable


def plan(**sections) -> Plan:
    p = Plan(sections={k.replace("_", "-"): v for k, v in sections.items()})
    p.packages_to_change = sum(len(v) for v in sections.values())
    return p


def up(name, old, new):
    return Solvable(name, new, "x86_64", edition_old=old)


def pkg(name, edition="1.0-1.1"):
    return Solvable(name, edition, "x86_64")


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.rules = Rules()

    def test_routine_upgrades(self):
        v = classify(plan(to_upgrade=[up("bash", "5.3.2-1.1", "5.3.3-1.1"), up("vim", "9.1-1", "9.1-2")]), self.rules)
        self.assertTrue(v.auto)
        self.assertEqual(v.reasons, [])

    def test_kernel_is_routine_but_needs_reboot(self):
        p = plan(to_install=[pkg("kernel-default", "6.18.2-1.1")],
                 to_remove=[pkg("kernel-default", "6.17.9-1.1")])
        v = classify(p, self.rules)
        self.assertTrue(v.auto)
        self.assertTrue(v.reboot_expected)

    def test_blocked_package(self):
        v = classify(plan(to_upgrade=[up("grub2-x86_64-efi", "2.12-59.1", "2.12-60.1")]), self.rules)
        self.assertFalse(v.auto)
        self.assertIn("kritisches Paket", v.reasons[0])

    def test_major_jump(self):
        v = classify(plan(to_upgrade=[up("systemd", "258.4-2.1", "259.1-1.1")]), self.rules)
        self.assertFalse(v.auto)
        self.assertIn("Versionssprung", v.reasons[0])

    def test_unknown_old_version_of_watched_package(self):
        v = classify(plan(to_upgrade=[up("mariadb", "", "12.0.1-1.1")]), self.rules)
        self.assertFalse(v.auto)
        self.assertIn("Ausgangsversion unbekannt", v.reasons[0])
        # Bei nicht überwachten Paketen spielt sie keine Rolle
        self.assertTrue(classify(plan(to_upgrade=[up("vim", "", "9.1-2")]), self.rules).auto)

    def test_minor_step_of_watched_package_is_routine(self):
        v = classify(plan(to_upgrade=[up("systemd", "258.3-1.1", "258.4-1.1")]), self.rules)
        self.assertTrue(v.auto)

    def test_watch_depth_two(self):
        v = classify(plan(to_upgrade=[up("php8-fpm", "8.4.12-1.1", "8.5.0-1.1")]), self.rules)
        self.assertFalse(v.auto)
        v = classify(plan(to_upgrade=[up("php8-fpm", "8.4.12-1.1", "8.4.13-1.1")]), self.rules)
        self.assertTrue(v.auto)

    def test_new_watched_package(self):
        v = classify(plan(to_install=[pkg("postgresql18-server", "18.1-1.1")]), self.rules)
        self.assertFalse(v.auto)
        self.assertIn("neues Paket", v.reasons[0])

    def test_routine_removals(self):
        p = plan(to_install=[pkg("libfoo3")], to_remove=[pkg("libfoo2"), pkg("bar-lang")])
        self.assertTrue(classify(p, self.rules).auto)

    def test_unexpected_removal(self):
        v = classify(plan(to_remove=[pkg("python311-requests")]), self.rules)
        self.assertFalse(v.auto)
        self.assertIn("wird entfernt", v.reasons[0])

    def test_too_many_removals(self):
        v = classify(plan(to_remove=[pkg(f"lib{i}") for i in range(25)]), self.rules)
        self.assertFalse(v.auto)

    def test_vendor_change(self):
        v = classify(plan(to_change_vendor=[up("ffmpeg-7", "7.1-1", "7.1-2")]), self.rules)
        self.assertFalse(v.auto)
        self.assertTrue(v.approvable)

    def test_conflict_not_approvable(self):
        p = plan()
        p.problems = ["Problem: 1: ..."]
        v = classify(p, self.rules)
        self.assertFalse(v.auto)
        self.assertFalse(v.approvable)

    def test_dovecot_switch_23_to_24(self):
        # dovecot 2.3 → 2.4 braucht eine neue Konfiguration
        p = plan(to_install=[pkg("dovecot24", "2.4.2-1.1")], to_remove=[pkg("dovecot23", "2.3.21.1-1.1")])
        v = classify(p, self.rules)
        self.assertFalse(v.auto)
        self.assertIn("neues Paket: dovecot24", v.reasons[0])
        self.assertTrue(any("wird entfernt: dovecot23" in r for r in v.reasons))

    def test_mariadb_minor_jump(self):
        v = classify(plan(to_upgrade=[up("mariadb", "13.0.2-1.1", "13.1.0-1.1")]), self.rules)
        self.assertFalse(v.auto)
        v = classify(plan(to_upgrade=[up("mariadb", "13.0.2-1.1", "13.0.3-1.1")]), self.rules)
        self.assertTrue(v.auto)

    def test_new_python_interpreter(self):
        v = classify(plan(to_install=[pkg("python314", "3.14.2-1.1")]), self.rules)
        self.assertFalse(v.auto)
        v = classify(plan(to_install=[pkg("python314-requests", "2.33-1.1")]), self.rules)
        self.assertTrue(v.auto)

    def test_forgejo_major(self):
        v = classify(plan(to_upgrade=[up("forgejo", "17.0.0-1.1", "18.0.0-1.1")]), self.rules)
        self.assertFalse(v.auto)
        v = classify(plan(to_upgrade=[up("forgejo", "17.0.0-1.1", "17.0.1-1.1")]), self.rules)
        self.assertTrue(v.auto)

    def test_version_key(self):
        self.assertEqual(version_key("1:17.4-2.1", 1), ("17",))
        self.assertEqual(version_key("10.0p2-1.1", 2), ("10", "0"))
        self.assertEqual(version_key("8.4.12-1.1", 2), ("8", "4"))


class ExampleConfigTest(unittest.TestCase):
    def test_example_rules_match_defaults(self):
        # config.example.toml soll die eingebauten Regeln wiedergeben
        with open(Path(__file__).parent.parent / "config.example.toml", "rb") as f:
            data = tomllib.load(f)
        self.assertEqual(Rules(**data["rules"]), Rules())

    def test_example_error_limit_matches_default(self):
        with open(Path(__file__).parent.parent / "config.example.toml", "rb") as f:
            data = tomllib.load(f)
        self.assertEqual(data["telegram"]["max_errors_per_hour"], Config.max_errors_per_hour)


if __name__ == "__main__":
    unittest.main()
