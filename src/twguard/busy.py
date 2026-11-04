# SPDX-License-Identifier: GPL-3.0-or-later
"""Was gerade läuft und was deshalb nicht starten darf: Server-Neustart, Einspielen, Dienst-Neustart, OTA.

Eigene Vorgänge reservieren sich im Zustand (`applying`, `reboot_planning`, `restart_request`,
`update_request`). Wer prüft und danach reserviert, tut beides unter derselben Sperre von state.json;
von zwei gleichzeitigen Aktionen gewinnt so genau eine. Fremde Vorgänge (zypper per SSH, purge-kernels
nach dem Boot, ein auf der Konsole geplanter Neustart) lassen sich nur abfragen, nicht ausschließen.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import asdict, dataclass

from . import state, zypper
from .i18n import tr
from .state import State

log = logging.getLogger(__name__)

# Vorgänge
SHUTDOWN = "shutdown"  # Neustart oder Herunterfahren geplant (logind) oder wird gerade geplant
UNKNOWN = "unknown"    # logind gibt keine Auskunft
ZYPP = "zypp"          # Paketverwaltung von einem anderen Prozess gesperrt
APPLY = "apply"        # ein Prüflauf spielt ein
CHECK = "check"        # twguard-check.service läuft
RESTART = "restart"    # Dienste werden neu gestartet
UPDATE = "update"      # OTA-Update von twguard läuft

# Welche Vorgänge eine Aktion abweisen. Auf eine belegte Paketverwaltung warten Prüfen und Einspielen
# selbst (zypper meldet sie mit Exit-Code 7), das Einspielen außerdem auf einen Dienst-Neustart.
# Prüfen und Freigeben ändern nichts am System; nur ein geplanter Neustart brächte ihr Ergebnis um.
# "check" fragt der Prüflauf selbst, "start-check" der Bot vor dem Anstoßen: Ein zweites Anstoßen
# während eines Prüflaufs bewirkt nichts (systemctl start startet ihn nicht noch einmal).
BLOCKERS = {
    "check": (SHUTDOWN,),
    "start-check": (SHUTDOWN, CHECK),
    "approve": (SHUTDOWN,),
    "apply": (SHUTDOWN, UNKNOWN, APPLY, UPDATE, RESTART),
    "reboot": (SHUTDOWN, UNKNOWN, ZYPP, APPLY, RESTART, UPDATE),
    "restart": (SHUTDOWN, UNKNOWN, ZYPP, APPLY, CHECK, RESTART, UPDATE),
    "update": (SHUTDOWN, UNKNOWN, APPLY, RESTART, UPDATE),
}

# Welche Aktion eine Schaltfläche auslöst, nach dem Anfang ihrer Callback-Daten; die übrigen (Details,
# Aufschub, Abbrechen …) ändern nichts am System und bleiben immer
BUTTON_ACTIONS = {"check": "start-check", "approve": "approve", "reboot": "reboot", "restart": "restart",
                  "update": "update"}

CHECK_UNIT = "twguard-check.service"
# Vorgänge, bei denen eine Aktion still nichts tut statt abgewiesen zu werden: Ein zweites Anstoßen während
# eines Prüflaufs ist überflüssig, und die Zustandsnachricht zeigt den laufenden schon
NOOP = {"start-check": (CHECK,)}
# So lange gilt ein OTA-Auftrag als laufend; danach meldet der Bot ihn als hängen geblieben
UPDATE_MAX_AGE = 900
QUERY_TIMEOUT = 5
LOGIND = ("org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager")
NO_TIME = 2**64 - 1


@dataclass(frozen=True)
class Shutdown:
    """Laut logind: "none", "planned" (geplant oder läuft schon) oder "unknown"."""

    state: str
    mode: str = ""   # reboot, poweroff, halt, kexec …
    at: float = 0.0  # geplante Zeit; 0, wenn sie schon läuft

    @property
    def reboot(self) -> bool:
        return self.mode in ("", "reboot", "kexec", "soft-reboot")


def shutdown() -> Shutdown:
    """Geplantes Herunterfahren über die öffentliche Schnittstelle von logind (`shutdown -c` hebt es auf)."""
    try:
        p = subprocess.run(["busctl", "--system", "get-property", *LOGIND, "ScheduledShutdown", "PreparingForShutdown"],
                           capture_output=True, text=True, timeout=QUERY_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning(tr("logind nicht erreichbar: %s"), e)
        return Shutdown("unknown")
    lines = p.stdout.splitlines()
    scheduled = re.fullmatch(r'\(st\) "([a-z-]*)" (\d+)', lines[0].strip()) if lines else None
    preparing = re.fullmatch(r"b (true|false)", lines[1].strip()) if len(lines) > 1 else None
    if p.returncode or not (scheduled and preparing):
        log.warning(tr("logind: unerwartete Antwort: %s"), (p.stdout + p.stderr).strip())
        return Shutdown("unknown")
    mode, usec = scheduled.group(1), int(scheduled.group(2))
    if preparing.group(1) == "true":
        return Shutdown("planned", mode)
    if mode:
        # Eine schon verstrichene Zeit ist keine Entwarnung: logind führt ihn gerade aus
        return Shutdown("planned", mode, 0.0 if usec in (0, NO_TIME) else usec / 1e6)
    return Shutdown("none")


def note_shutdown(st: State, sd: Shutdown) -> None:
    """Für die Anzeige merken; Aktionen fragen logind selbst."""
    st.shutdown = {} if sd.state == "none" else asdict(sd)


def check_running() -> bool:
    """Läuft der Prüflauf? Als oneshot steht er währenddessen auf „activating“, nicht „active“."""
    try:
        p = subprocess.run(["systemctl", "is-active", CHECK_UNIT], capture_output=True, text=True,
                           timeout=QUERY_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return True
    return p.stdout.strip() in ("active", "activating", "reloading", "deactivating")


def planned_text(sd: Shutdown | dict) -> str:
    sd = Shutdown(**sd) if isinstance(sd, dict) else sd
    if sd.state == "unknown":
        return tr("logind gibt keine Auskunft, ob ein Neustart geplant ist")
    when = time.strftime("%H:%M", time.localtime(sd.at)) if sd.at else ""
    if sd.reboot:
        return tr("Neustart geplant um {when}", when=when) if when else tr("Neustart läuft")
    return tr("Herunterfahren geplant um {when}", when=when) if when else tr("Herunterfahren läuft")


def update_running(st: State) -> bool:
    """Ein OTA-Auftrag läuft, solange sein Ausführer lebt; vor dessen Start (systemd-run) höchstens UPDATE_MAX_AGE."""
    request = st.update_request
    if not request:
        return False
    if request.get("runner"):
        return state.alive(request["runner"])
    return time.time() - request.get("at", 0) < UPDATE_MAX_AGE


def blockers(action: str, st: State, seen: bool = False) -> list[tuple[str, str]]:
    """Was `action` gerade abweist, je (Vorgang, Begründung); leer heißt frei.

    Fragt nur ab, was für die Aktion zählt (logind, Sperrdatei, systemctl). Wer danach reserviert, ruft
    das unter `state.locked` auf und reserviert im selben Block. Mit `seen` fragt es nichts ab, sondern
    nimmt den zuletzt beobachteten Stand aus dem Zustand (für die Anzeige, siehe `allowed`).
    """
    found = []
    kinds = BLOCKERS[action]
    if SHUTDOWN in kinds:
        if state.alive(st.reboot_planning):
            found.append((SHUTDOWN, tr("Neustart wird gerade geplant")))
        else:
            sd = Shutdown(**st.shutdown) if seen and st.shutdown else Shutdown("none") if seen else shutdown()
            if not seen:
                note_shutdown(st, sd)
            if sd.state == "planned" or (sd.state == "unknown" and UNKNOWN in kinds):
                found.append((SHUTDOWN if sd.state == "planned" else UNKNOWN, planned_text(sd)))
    if ZYPP in kinds:
        holder = st.zypp_holder if seen else zypper.lock_holder()
        if seen and own_holder(holder) and not st.activity.get("check"):
            holder = ""  # beobachtet, bevor der eigene Prüflauf endete; seine Sperre ist mit ihm vorbei
        if holder:
            found.append((ZYPP, tr("Paketverwaltung belegt durch {holder}", holder=holder)))
    if APPLY in kinds and state.alive(st.applying):
        found.append((APPLY, tr("twguard spielt gerade ein")))
    if RESTART in kinds and st.restart_request:
        found.append((RESTART, tr("Dienste werden gerade neu gestartet")))
    if UPDATE in kinds and update_running(st):
        found.append((UPDATE, tr("ein Update von twguard läuft")))
    if CHECK in kinds and (bool(st.activity.get("check")) if seen else check_running()):
        found.append((CHECK, tr("ein Prüflauf läuft")))
    return found


def own_holder(holder: str) -> bool:
    """Hält der eigene Prüflauf die Paketverwaltung? (Text aus `zypper.lock_holder`)"""
    return holder.startswith(f"{CHECK_UNIT} (")


def decide(action: str, st: State) -> tuple[str, str]:
    """("allow", ""), ("noop", "") oder ("deny", Ablehnung ohne HTML); eine Abfrage für alle drei.

    Abgewiesen wird mit dem ersten Grund, der nicht in NOOP steht; bleiben nur solche, tut die Aktion still nichts.
    """
    found = blockers(action, st)
    deny = [reason for kind, reason in found if kind not in NOOP.get(action, ())]
    if deny:
        return "deny", tr("Gerade nicht möglich: {reason}. Bitte danach erneut.", reason=deny[0])
    return ("noop", "") if found else ("allow", "")


def allowed(data: str, st: State) -> bool:
    """Darf die Schaltfläche mit diesen Callback-Daten gerade angeboten werden? Nach dem beobachteten Stand.

    Dieselbe Tabelle wie beim Druck: Was der Bot jetzt abweisen würde, zeigt die Zustandsnachricht nicht an.
    Beim Druck fragt der Bot trotzdem noch einmal selbst nach (`refusal`), der Stand kann sich geändert haben.
    """
    action = BUTTON_ACTIONS.get(data.partition(":")[0])
    return action is None or not blockers(action, st, seen=True)


def refusal(action: str, st: State) -> str:
    """Ablehnung mit dem ersten Grund (ohne HTML), leer heißt frei oder still nichts tun (siehe `decide`)."""
    return decide(action, st)[1]
