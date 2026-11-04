# SPDX-License-Identifier: GPL-3.0-or-later
"""Dienste, die nach einem Update noch gelöschte Dateien festhalten: erkennen und neu starten."""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from fnmatch import fnmatchcase

from . import zypper
from .i18n import tr
from .state import State

SYSTEMCTL_TIMEOUT = 60
# So lange muss ein neu gestarteter Dienst aktiv sein, bevor er als erfolgreich gilt
SETTLE_SECONDS = 10
BOT_SERVICE = "twguard-bot"
# So viel älter als der letzte Start des Dienstes muss ein Prozess sein, um als übrig geblieben zu gelten (µs)
SURVIVOR_MARGIN = 2_000_000


@dataclass(frozen=True)
class Survey:
    """Dienste mit alten Dateien laut `zypper ps`; `lingering`: die, bei denen ein Neustart nichts bewirkt."""

    services: list[str]
    lingering: dict[str, list[zypper.Proc]]


def survey() -> Survey | None:
    """`zypper ps` abfragen und je Dienst bewerten; None, wenn zypper keine Auskunft gibt."""
    procs = zypper.processes_needing_restart()
    if procs is None:
        return None
    by_service: dict[str, list[zypper.Proc]] = {}
    for proc in procs:
        by_service.setdefault(proc.service, []).append(proc)
    return Survey(sorted(by_service), {name: found for name, found in sorted(by_service.items())
                                       if left_over(name, found)})


def left_over(name: str, procs: list[zypper.Proc]) -> bool:
    """Halten nur Prozesse alte Dateien, die ein Neustart nicht beendet?

    Bei KillMode=process (oder none) beendet systemd beim Neustart nur den Hauptprozess; was sonst im
    Cgroup des Dienstes läuft, bleibt (z. B. screen in einer SSH-Sitzung bei sshd, ein per Cron gestarteter
    Server bei cron). Ist der Hauptprozess schon frisch, hilft ein weiterer Neustart nie. Ohne Hauptprozess
    (eingebundene Init-Skripte mit RemainAfterExit) entscheidet das Stop-Skript; sind alle Prozesse älter als
    der letzte Start des Dienstes, hat es sie schon einmal nicht beendet. Ohne Auskunft wird der Neustart
    weiter angeboten.
    """
    try:
        p = _systemctl("show", "-p", "MainPID", "-p", "KillMode", "-p", "InactiveExitTimestampMonotonic",
                       "--", name)
    except (OSError, subprocess.TimeoutExpired):
        return False
    props = dict(line.partition("=")[::2] for line in p.stdout.splitlines())
    main = props.get("MainPID", "")
    if p.returncode or not main.isdigit() or props.get("KillMode") not in ("process", "none"):
        return False
    if int(main):
        return all(proc.pid != int(main) for proc in procs)
    started = props.get("InactiveExitTimestampMonotonic", "")
    if not started.isdigit() or not int(started):
        return False
    ages = [_started(proc.pid) for proc in procs]
    return all(age is not None and age < int(started) - SURVIVOR_MARGIN for age in ages)


def _started(pid: int) -> int | None:
    """Startzeit eines Prozesses in Mikrosekunden seit dem Boot; None, wenn nicht lesbar.

    Der Kernel zählt sie mit der Ruhezeit, systemd ohne (CLOCK_MONOTONIC); nach einem Ruhezustand wirkt ein
    Prozess dadurch jünger, und der Neustart wird wie bisher angeboten.
    """
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as f:
            fields = f.read().rpartition(")")[2].split()
        return int(fields[19]) * 1_000_000 // os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None


def digest(names: list[str]) -> str:
    """Kennung einer Liste für „Alle“: Ein alter Knopf darf keine später erkannten Dienste erfassen."""
    return hashlib.sha256("\n".join(names).encode()).hexdigest()[:12]


def split(found: list[str], exclude: list[str]) -> tuple[list[str], list[str]]:
    """Aufteilen in (anzubieten, ausgeschlossen)."""
    skipped = [name for name in found if any(fnmatchcase(name, pattern) for pattern in exclude)]
    return [name for name in found if name not in skipped], skipped


def note(st: State, found: Survey, exclude: list[str]) -> None:
    """Offenen Punkt `services` und die Hinweise auf übrig gebliebene Prozesse an `zypper ps` anpassen.

    Ausgeschlossene Dienste allein öffnen keinen Punkt: Er ließe sich nur mit einem Neustart des
    Servers schließen und würde sonst dauerhaft erinnern. Dasselbe gilt für Dienste, bei denen ein
    Neustart nichts bewirkt; sie stehen als Hinweis ohne Schaltfläche und ohne Erinnerung im Status.
    Solange ein Neustart läuft, gilt erst dessen Ergebnis.
    """
    if st.restart_request:
        return
    st.lingering = {name: [asdict(proc) for proc in procs] for name, procs in found.lingering.items()}
    offered, skipped = split([name for name in found.services if name not in found.lingering], exclude)
    added = set(offered) - set(st.restart_services)
    st.restart_services = offered
    if not offered:
        st.resolve("services")
        return
    detail = tr("Diese Dienste nutzen noch alte Dateien:\n{services}",
                services="\n".join(f"• {name}" for name in offered))
    if skipped:
        detail += "\n\n" + tr("Nicht angeboten (Ausschlussliste): {services}", services=", ".join(skipped))
    issue, new = st.open_issue("services", detail)
    if added and not new:
        issue.last_notified = 0.0  # die Meldung soll die neuen Schaltflächen zeigen


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT)


def start(names: list[str]) -> dict[str, str]:
    """Neustarts einzeln einreihen, ohne zu warten; Rückgabe: Dienst → Fehlertext der abgewiesenen.

    Einzeln, damit ein abgewiesener Dienst die übrigen nicht verdeckt. Ohne Warten, weil ein Dienst
    lange zum Beenden brauchen kann. Der Bot selbst gehört als Letzter in einen eigenen Aufruf: Sein
    Neustart beendet auch dieses systemctl (siehe Bot.press_restart).
    """
    errors = {}
    for name in names:
        try:
            p = _systemctl("restart", "--no-block", "--", name)
        except subprocess.TimeoutExpired:
            continue  # Ob der Auftrag angenommen wurde, ist offen; das Ergebnis zeigt es
        except OSError as e:
            errors[name] = str(e)
            continue
        if p.returncode:
            errors[name] = p.stderr.strip() or tr("Exit-Code {rc}", rc=p.returncode)
    return errors


def pending(names: list[str]) -> list[str] | None:
    """Die Dienste, für die noch ein Auftrag ansteht; `None`, wenn systemd keine Auskunft gibt."""
    if not names:
        return []
    try:
        p = _systemctl("list-jobs", "--no-legend", "--plain")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode:
        return None
    units = {line.split()[1].removesuffix(".service") for line in p.stdout.splitlines() if len(line.split()) > 1}
    return [name for name in names if name in units]


def states(names: list[str]) -> dict[str, str]:
    """Zustand je Dienst laut `systemctl is-active` (active, failed, …)."""
    try:
        lines = _systemctl("is-active", "--", *names).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        lines = []
    if len(lines) != len(names):
        return dict.fromkeys(names, "unknown")
    return dict(zip(names, lines))


def young(names: list[str], seconds: float = SETTLE_SECONDS) -> list[str] | None:
    """Die Dienste, die erst seit weniger als `seconds` aktiv sind; `None`, wenn systemd keine Auskunft gibt.

    Bei `Type=simple` ist der Startauftrag erledigt, sobald der Prozess erzeugt ist. Scheitert das Programm
    selbst (z. B. fehlende Datei, 203/EXEC), steht der Dienst einen Augenblick lang trotzdem auf „active“.
    """
    if not names:
        return []
    try:
        p = _systemctl("show", "--property=ActiveEnterTimestampMonotonic", "--value", "--", *names)
    except (OSError, subprocess.TimeoutExpired):
        return None
    values = p.stdout.split()
    if p.returncode or len(values) != len(names) or not all(value.isdigit() for value in values):
        return None
    # systemd zählt wie time.monotonic() in CLOCK_MONOTONIC, aber in Mikrosekunden
    now = time.monotonic() * 1_000_000
    return [name for name, value in zip(names, values) if now - int(value) < seconds * 1_000_000]
