# SPDX-License-Identifier: GPL-3.0-or-later
"""Aufrufe von zypper und Auswertung der XML-Ausgabe (--xmlout)."""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Abschnitte von <install-summary>, in der Reihenfolge der zypper-Ausgabe
SECTIONS = (
    "to-install",
    "to-upgrade",
    "to-downgrade",
    "to-reinstall",
    "to-remove",
    "to-change-arch",
    "to-change-vendor",
)

# Exit-Codes laut zypper(8), die einen erfolgreichen Lauf bedeuten
EXIT_REBOOT_NEEDED = 102
EXIT_RESTART_NEEDED = 103
OK_CODES = {0, EXIT_REBOOT_NEEDED, EXIT_RESTART_NEEDED}
# Eigener Code für „abgebrochen, weil keine Antwort“ (zypper selbst nutzt nur positive)
EXIT_TIMEOUT = -1

# Zeitlimits: Abfragen im Bot dürfen ihn nicht blockieren; refresh und Probelauf brauchen
# mit Debug- und Quell-Repos länger. Das Einspielen bekommt bewusst keins, ein Abbruch
# mitten in der Installation wäre schlimmer als ein hängender Prüflauf.
QUERY_TIMEOUT = 120
CHECK_TIMEOUT = 1800


@dataclass(frozen=True)
class Solvable:
    name: str
    edition: str
    arch: str = ""
    edition_old: str = ""
    arch_old: str = ""
    repository: str = ""

    def label(self) -> str:
        if self.edition_old and self.edition_old != self.edition:
            return f"{self.name} {self.edition_old} → {self.edition}"
        return f"{self.name} {self.edition}"


@dataclass
class Plan:
    """Was `zypper dup` tun würde (aus dem Probelauf)."""

    sections: dict[str, list[Solvable]] = field(default_factory=dict)
    packages_to_change: int = 0
    download_size: int = 0
    need_reboot: bool = False
    problems: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    exit_code: int = 0
    # Unveränderte XML-Ausgabe des Probelaufs (wird zur Nachverfolgung gesichert)
    raw: str = field(default="", repr=False)

    def get(self, section: str) -> list[Solvable]:
        return self.sections.get(section, [])

    @property
    def empty(self) -> bool:
        return not any(self.sections.values()) and not self.problems

    @property
    def failed(self) -> bool:
        # Ein Solver-Konflikt ist kein Fehler des Laufs, sondern ein Fall für die Einstufung
        return self.exit_code not in OK_CODES and not self.problems

    def fingerprint(self) -> str:
        """Kennung des Plans; eine Freigabe gilt nur für genau diesen Stand.

        Ausgangsstand und Quelle gehören dazu: 8.4.0 → 8.4.1 ist Routine, 7.4.0 → 8.4.1 nicht.
        """
        h = hashlib.sha256()
        for entry in sorted(_identities(self)):
            h.update("|".join(entry).encode() + b"\n")
        return h.hexdigest()[:16]


def _identity(section: str, s: Solvable) -> tuple[str, ...]:
    """Alles, was ein Paket im Plan ausmacht; Fingerabdruck und Abgleich nutzen dieselben Felder."""
    return (section, s.name, s.edition, s.arch, s.edition_old, s.arch_old, s.repository)


def _identities(plan: Plan) -> dict[tuple[str, ...], tuple[str, Solvable]]:
    return {_identity(section, s): (section, s) for section in SECTIONS for s in plan.get(section)}


def parse_plan(xml_text: str, exit_code: int = 0) -> Plan:
    plan = Plan(exit_code=exit_code)
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        plan.errors.append(f"zypper-Ausgabe nicht lesbar: {e}")
        if plan.exit_code in OK_CODES:
            plan.exit_code = -1
        return plan

    for msg in root.iter("message"):
        if msg.get("type") == "error" and msg.text:
            plan.errors.append(msg.text.strip())

    summary = root.find("install-summary")
    if summary is not None:
        plan.packages_to_change = int(summary.get("packages-to-change", "0"))
        plan.download_size = int(summary.get("download-size", "0"))
        plan.need_reboot = summary.get("need-reboot") == "1"
        for section in SECTIONS:
            node = summary.find(section)
            if node is None:
                continue
            plan.sections[section] = [
                Solvable(
                    name=s.get("name", ""),
                    edition=s.get("edition", ""),
                    arch=s.get("arch", ""),
                    edition_old=s.get("edition-old", ""),
                    arch_old=s.get("arch-old", ""),
                    repository=s.get("repository", ""),
                )
                for s in node.findall("solvable")
                if s.get("type", "package") == "package"
            ]

    # Solver-Konflikte kommen als Rückfrage, deren Beschreibung mit "Problem" beginnt
    for prompt in root.iter("prompt"):
        desc = (prompt.findtext("description") or "").strip()
        if desc.startswith("Problem"):
            plan.problems.append(desc)
    return plan


def _run(args: list[str], xml: bool = True, timeout: float | None = None) -> subprocess.CompletedProcess:
    cmd = ["zypper", "--non-interactive"]
    if xml:
        cmd.append("--xmlout")
    cmd += args
    env = dict(os.environ, LC_ALL="C.UTF-8")
    log.info("starte: %s", " ".join(cmd))
    try:
        return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.error("%s: keine Antwort nach %d s, abgebrochen", " ".join(cmd), timeout)
        return subprocess.CompletedProcess(cmd, EXIT_TIMEOUT, "", f"keine Antwort nach {timeout:.0f} s, abgebrochen")


def refresh() -> list[str]:
    """Repos auffrischen. Gibt Fehlermeldungen zurück, leer heißt erfolgreich."""
    p = _run(["refresh"], timeout=CHECK_TIMEOUT)
    if p.returncode == 0:
        return []
    if p.returncode == EXIT_TIMEOUT:
        return [f"zypper refresh: {p.stderr}"]
    errors = parse_plan(p.stdout, p.returncode).errors
    return errors or [f"zypper refresh: Exit-Code {p.returncode}", p.stderr.strip()]


def dry_run(dup_args: list[str]) -> Plan:
    p = _run(["--no-refresh", "dup", "--dry-run", "--details", *dup_args], timeout=CHECK_TIMEOUT)
    if p.returncode == EXIT_TIMEOUT:
        return Plan(exit_code=EXIT_TIMEOUT, errors=[f"zypper dup --dry-run: {p.stderr}"])
    plan = parse_plan(p.stdout, p.returncode)
    plan.raw = p.stdout
    if plan.failed and not plan.errors:
        plan.errors.append(f"zypper dup --dry-run: Exit-Code {p.returncode} {p.stderr.strip()}")
    return plan


def apply(dup_args: list[str]) -> tuple[int, str, Plan]:
    """Einspielen. Rückgabe: Exit-Code, Meldungen und der Plan, den zypper tatsächlich ausgeführt hat.

    zypper kann keinen vorher geprüften Plan ausführen, sondern rechnet neu. Ändert sich zwischen
    Probelauf und Einspielen etwas (fremder Refresh, Paketoperation), weicht der Plan ab; das
    lässt sich nicht verhindern, aber hinterher am ausgegebenen Plan erkennen.
    """
    # --no-refresh: derselbe Repo-Stand wie im Probelauf, sonst passt der Fingerabdruck nicht
    p = _run(["--no-refresh", "dup", "--details", "--auto-agree-with-licenses", *dup_args])
    return p.returncode, message_text(p.stdout) + p.stderr, parse_plan(p.stdout, p.returncode)


def message_text(xml_text: str) -> str:
    """Lesbare Meldungen aus der XML-Ausgabe; ist sie nicht lesbar, unverändert."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return xml_text
    return "".join(f"{m.text.strip()}\n" for m in root.iter("message") if m.text and m.text.strip())


def diff_plans(checked: Plan, applied: Plan) -> list[str]:
    """Was beim Einspielen anders war als im geprüften Plan, je Zeile ein Paket."""
    def line(section: str, s: Solvable) -> str:
        arch = f"{s.arch_old} → {s.arch}" if s.arch_old and s.arch_old != s.arch else s.arch
        return f"{section}: {s.label()} ({', '.join(x for x in (arch, s.repository) if x)})"

    a, b = _identities(checked), _identities(applied)
    return ([f"+ {line(*b[k])}" for k in sorted(b.keys() - a.keys())]
            + [f"− {line(*a[k])}" for k in sorted(a.keys() - b.keys())])


def needs_rebooting() -> bool | None:
    """True/False laut `zypper needs-rebooting`, None wenn es nicht feststellbar war."""
    rc = _run(["needs-rebooting"], xml=False, timeout=QUERY_TIMEOUT).returncode
    if rc == EXIT_REBOOT_NEEDED:
        return True
    if rc == 0:
        return False
    log.warning("zypper needs-rebooting: Exit-Code %s", rc)
    return None


def services_needing_restart() -> list[str]:
    p = _run(["ps", "-sss"], xml=False, timeout=QUERY_TIMEOUT)
    if p.returncode == EXIT_TIMEOUT:
        log.warning("zypper ps: %s", p.stderr)
    return sorted({line.strip() for line in p.stdout.splitlines() if line.strip()})
