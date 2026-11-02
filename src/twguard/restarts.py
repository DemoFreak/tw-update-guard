# SPDX-License-Identifier: GPL-3.0-or-later
"""Dienste, die nach einem Update noch gelöschte Dateien festhalten: erkennen und neu starten."""

from __future__ import annotations

import hashlib
import subprocess
from fnmatch import fnmatchcase

from .i18n import tr
from .state import State

SYSTEMCTL_TIMEOUT = 60
BOT_SERVICE = "twguard-bot"


def digest(names: list[str]) -> str:
    """Kennung einer Liste für „Alle“: Ein alter Knopf darf keine später erkannten Dienste erfassen."""
    return hashlib.sha256("\n".join(names).encode()).hexdigest()[:12]


def split(found: list[str], exclude: list[str]) -> tuple[list[str], list[str]]:
    """Aufteilen in (anzubieten, ausgeschlossen)."""
    skipped = [name for name in found if any(fnmatchcase(name, pattern) for pattern in exclude)]
    return [name for name in found if name not in skipped], skipped


def note(st: State, found: list[str], exclude: list[str]) -> None:
    """Offenen Punkt `services` an das Ergebnis von `zypper ps` anpassen.

    Ausgeschlossene Dienste allein öffnen keinen Punkt: Er ließe sich nur mit einem Neustart des
    Servers schließen und würde sonst dauerhaft erinnern. Solange ein Neustart läuft, gilt erst
    dessen Ergebnis.
    """
    if st.restart_request:
        return
    offered, skipped = split(found, exclude)
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
