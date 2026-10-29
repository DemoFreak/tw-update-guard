# SPDX-License-Identifier: GPL-3.0-or-later
"""Einstufung eines Update-Plans: Routine (automatisch) oder Freigabe nötig."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from .zypper import Plan, Solvable


@dataclass
class Rules:
    # Jede Änderung braucht Freigabe: Bootloader, Netzwerk, SSH (Aussperrgefahr bei headless)
    block: list[str] = field(default_factory=lambda: [
        "grub2", "grub2-*", "shim", "systemd-boot", "dracut",
        "openssh-server*", "wicked*", "NetworkManager", "systemd-network", "firewalld",
    ])
    # Freigabe bei Versionssprung, außerdem bei neu hinzukommenden oder entfernten Paketen
    # dieses Musters. "muster:2" vergleicht die ersten zwei Versionsteile (z. B. PHP 8.4 → 8.5).
    # Typische Serverdienste; Muster ohne Treffer schaden nicht.
    watch: list[str] = field(default_factory=lambda: [
        # Basis
        "systemd", "glibc", "openssl", "mdadm", "python3[0-9][0-9]",
        # Web und Datenbanken
        "apache2", "php8*:2", "mariadb:2", "redis", "postgresql*", "nginx", "nextcloud",
        "roundcubemail:2", "forgejo",
        # Mail und DNS
        "postfix", "dovecot*:2", "amavisd-new", "spamassassin", "clamav:2", "bind:2",
        # Sonstige Dienste
        "bareos-filedaemon", "samba:2", "docker", "containerd", "podman", "syncthing",
        "snapd", "fail2ban:2",
    ])
    # Nur zur Information: Diese Pakete ziehen einen Neustart nach sich
    reboot: list[str] = field(default_factory=lambda: ["kernel-default", "kernel-longterm"])
    # Entfernungen, die bei Tumbleweed normal sind (alte Bibliotheksversionen u. Ä.)
    routine_removals: list[str] = field(default_factory=lambda: ["lib*", "*-lang", "typelib-*"])
    max_removals: int = 20
    # 0 = keine Grenze; große Rebuilds sind bei Tumbleweed normal
    max_packages: int = 0


@dataclass
class Verdict:
    auto: bool
    reasons: list[str]
    approvable: bool = True
    reboot_expected: bool = False


def _matches(name: str, patterns: list[str]) -> bool:
    return any(fnmatchcase(name, p.split(":", 1)[0]) for p in patterns)


def _watch_depth(name: str, patterns: list[str]) -> int | None:
    for p in patterns:
        pattern, _, depth = p.partition(":")
        if fnmatchcase(name, pattern):
            return int(depth) if depth else 1
    return None


def version_key(edition: str, depth: int) -> tuple[str, ...]:
    """Die ersten `depth` Zahlen der Version, ohne Epoch und Release ("1:17.4-2.1" → ("17",))."""
    version = edition.split(":", 1)[-1].split("-", 1)[0]
    return tuple(re.findall(r"\d+", version)[:depth])


def classify(plan: Plan, rules: Rules) -> Verdict:
    if plan.problems:
        return Verdict(
            auto=False,
            reasons=["Solver-Konflikt, nur per SSH lösbar:", *plan.problems],
            approvable=False,
        )

    reasons: list[str] = []
    for section, text in (
        ("to-downgrade", "Downgrade"),
        ("to-change-arch", "Architekturwechsel"),
        ("to-change-vendor", "Anbieterwechsel"),
    ):
        reasons += [f"{text}: {s.label()}" for s in plan.get(section)]

    installs = plan.get("to-install")
    upgrades = plan.get("to-upgrade") + plan.get("to-downgrade")
    removals = plan.get("to-remove")
    changed = installs + upgrades + plan.get("to-reinstall")

    for s in changed:
        if _matches(s.name, rules.block):
            reasons.append(f"kritisches Paket: {s.label()}")
    for s in removals:
        if _matches(s.name, rules.block):
            reasons.append(f"kritisches Paket wird entfernt: {s.label()}")

    for s in upgrades:
        depth = _watch_depth(s.name, rules.watch)
        if not depth:
            continue
        # Ohne Ausgangsversion lässt sich ein Sprung nicht ausschließen
        if not s.edition_old:
            reasons.append(f"Ausgangsversion unbekannt: {s.label()}")
        elif version_key(s.edition_old, depth) != version_key(s.edition, depth):
            reasons.append(f"Versionssprung: {s.label()}")

    installed_names = {s.name for s in installs}
    for s in installs:
        if _watch_depth(s.name, rules.watch) and not _matches(s.name, rules.block):
            reasons.append(f"neues Paket: {s.label()}")

    # Bei mehrfach installierbaren Paketen (Kernel) ersetzt die neue Version die alte
    unexpected = [
        s for s in removals
        if s.name not in installed_names
        and not _matches(s.name, rules.routine_removals)
        and not _matches(s.name, rules.block)
    ]
    reasons += [f"wird entfernt: {s.label()}" for s in unexpected]
    if len(removals) > rules.max_removals:
        reasons.append(f"{len(removals)} Pakete werden entfernt (Grenze {rules.max_removals})")
    if rules.max_packages and plan.packages_to_change > rules.max_packages:
        reasons.append(f"{plan.packages_to_change} Pakete (Grenze {rules.max_packages})")

    reboot_expected = plan.need_reboot or any(_matches(s.name, rules.reboot) for s in changed)
    reasons = list(dict.fromkeys(reasons))
    return Verdict(auto=not reasons, reasons=reasons, reboot_expected=reboot_expected)


def _first_lines(items: list[Solvable], limit: int) -> list[str]:
    lines = [f"  {s.label()}" for s in items[:limit]]
    if len(items) > limit:
        lines.append(f"  … und {len(items) - limit} weitere")
    return lines


SECTION_TITLES = {
    "to-install": "Neu",
    "to-upgrade": "Aktualisiert",
    "to-downgrade": "Downgrade",
    "to-reinstall": "Neu installiert",
    "to-remove": "Entfernt",
    "to-change-arch": "Architekturwechsel",
    "to-change-vendor": "Anbieterwechsel",
}


def describe(plan: Plan, per_section: int = 40) -> str:
    """Lesbare Übersicht des Plans für /details."""
    lines = [f"{plan.packages_to_change} Pakete, Download {plan.download_size / 1e6:.0f} MB"]
    for section, title in SECTION_TITLES.items():
        items = plan.get(section)
        if items:
            lines.append(f"{title} ({len(items)}):")
            lines += _first_lines(items, per_section)
    lines += plan.problems
    return "\n".join(lines)
