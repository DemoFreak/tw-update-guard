# SPDX-License-Identifier: GPL-3.0-or-later
"""Zustand (offene Punkte, Freigabe, Telegram-Offset) als JSON-Datei mit Sperre."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Iterator


@dataclass
class Issue:
    """Ein offener Punkt. Er verschwindet erst, wenn der Zustand behoben ist."""

    kind: str  # manual, reboot, services, error, apply-failed, stale
    since: float
    detail: str = ""
    fingerprint: str = ""
    approvable: bool = False
    last_notified: float = 0.0
    notify_count: int = 0
    snoozed_until: float = 0.0
    snooze_count: int = 0


@dataclass
class State:
    issues: dict[str, Issue] = field(default_factory=dict)
    approved_fingerprint: str = ""
    last_check_ok: float = 0.0
    last_plan_text: str = ""
    # Dasselbe als Daten für /details (details.snapshot)
    last_plan: dict = field(default_factory=dict)
    first_seen: float = 0.0
    tg_offset: int = 0
    # twguard hat den Neustart ausgelöst; nach dem Boot meldet der Bot „wieder da“
    reboot_requested_at: float = 0.0
    # Bot plant gerade einen Neustart {"pid", "boot", "at"} (busy): schließt Einspielen dazwischen aus
    reboot_planning: dict = field(default_factory=dict)
    # Zuletzt gesehener geplanter Neustart laut logind (busy.Shutdown), nur für die Anzeige
    shutdown: dict = field(default_factory=dict)
    # Boot, in dem der Bot zuletzt gestartet ist; ein Wechsel heißt: der Server hat neu gestartet
    boot_id: str = ""
    # Seit dem Boot noch nicht abgefragt, weil zypper keine Auskunft gab: "reboot", "services"
    unverified: list = field(default_factory=list)
    rpmdb_mtime: float = 0.0
    # Beobachtungsmodus: zuletzt gemeldeter Routine-Plan (nicht täglich wiederholen)
    observed_fingerprint: str = ""
    # Beobachtungsmodus: Routine-Plan, der noch nicht eingespielt ist (Zusammenfassung), sonst leer
    pending_text: str = ""
    # Mit /language gewählte Sprache, solange sie von der Konfiguration abweicht; der Bot löscht
    # sie bei jedem Start, ein Neustart fällt also auf die Konfiguration zurück
    language_override: str = ""
    # Mit /mode gewählter Modus, gilt bis zum nächsten Start des Bots (auch für den Prüflauf)
    mode_override: str = ""
    # OTA: laufendes Update {"commit", "chat_id", "message_id", "at"}; Ergebnis meldet der neue Bot
    update_request: dict = field(default_factory=dict)
    # Commit, mit dem der Bot zuletzt gestartet ist
    bot_commit: str = ""
    # /check oder /approve wartet auf das Ergebnis {"chat_id", "message_id", "at"}
    check_request: dict = field(default_factory=dict)
    # Tag (JJJJ-MM-TT) des zuletzt gesendeten Briefings
    last_briefing_day: str = ""
    # Nachricht je Thema (messages.py): {"status": {"chat_id", "message_id", "at"}, "issue:reboot": …}
    messages: dict = field(default_factory=dict)
    # Kurzlebige Antworten, die der Bot wieder löscht: [{"chat_id", "message_id", "at", "delete_at"}]
    ephemeral: list = field(default_factory=list)
    # Stille Update-Berichte seit dem letzten Briefing: [{"chat_id"?, "message_id"?, "at", "packages"}]
    reports: list = field(default_factory=list)
    # Gruppe wurde zur Supergruppe: {"from": alte ID, "to": neue ID, "at"}; gilt, solange config.toml
    # noch die alte ID enthält
    chat_migration: dict = field(default_factory=dict)
    # Aufgeklappte Details: message_id → ursprünglicher Inhalt {"text", "entities", "reply_markup", "at"}
    collapsed: dict = field(default_factory=dict)
    # Dienste, die zum Neustart anstehen; „Alle“ trägt den Hash der gezeigten Liste (restarts.digest)
    restart_services: list = field(default_factory=list)
    # Laufender Neustart {"chat_id", "message_id", "at", "services", "errors", "overdue"}; überlebt den Neustart des Bots
    restart_request: dict = field(default_factory=dict)
    # Rückmeldungen, die noch zugestellt werden müssen (Dienst-Neustart, Rückkehr nach dem Boot),
    # je {"chat_id", "message_id", "at", "text", "loud"}; der Bot versucht es bei jedem Takt erneut
    outbox: list = field(default_factory=list)
    # Meldung „wieder da“, solange nach dem Boot die Paketverwaltung belegt ist (purge-kernels)
    # {"chat_id", "message_id", "at", "text"}; der Bot ergänzt sie, sobald die Sperre frei ist
    maintenance: dict = field(default_factory=dict)
    # Prüflauf spielt gerade ein {"pid", "boot", "at"}; schließt Dienst-Neustarts aus (holder/alive)
    applying: dict = field(default_factory=dict)
    # Fingerabdruck des zuletzt tatsächlich gezeigten Plans; /approve gilt nur für ihn.
    # Nicht in Issue: eine ältere Version scheitert nach dem Zurückrollen an unbekannten Feldern
    manual_shown: str = ""

    def open_issue(self, kind: str, detail: str, now: float | None = None, **attrs) -> tuple[Issue, bool]:
        """Legt den Punkt an oder aktualisiert ihn; `since` bleibt dabei erhalten."""
        issue = self.issues.get(kind)
        new = issue is None
        if new:
            issue = self.issues[kind] = Issue(kind=kind, since=now or time.time())
        issue.detail = detail
        for key, value in attrs.items():
            setattr(issue, key, value)
        return issue, new

    def resolve(self, kind: str) -> Issue | None:
        return self.issues.pop(kind, None)


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def holder() -> dict:
    """Kennung dieses Prozesses für eine Reservierung im Zustand."""
    return {"pid": os.getpid(), "boot": boot_id(), "at": time.time()}


def alive(entry: dict) -> bool:
    """Lebt der Prozess einer Reservierung noch? Nach Absturz oder Reboot gilt sie nicht mehr."""
    if not entry or entry.get("boot") != boot_id():
        return False
    try:
        os.kill(int(entry["pid"]), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except (KeyError, TypeError, ValueError):
        return False
    return True


def load(path: Path) -> State:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return State()
    issues = {k: Issue(**v) for k, v in data.pop("issues", {}).items()}
    # Bis 0.9.4 hieß die Warteschlange restart_reports
    if "restart_reports" in data:
        data.setdefault("outbox", data.pop("restart_reports"))
    known = {f.name for f in fields(State)}
    return State(issues=issues, **{k: v for k, v in data.items() if k in known})


def save(path: Path, state: State) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    with os.fdopen(fd, "w") as f:
        json.dump(asdict(state), f, indent=1)
    os.replace(tmp, path)


@contextmanager
def locked(state_dir: Path) -> Iterator[State]:
    """Zustand exklusiv laden und nach dem Block speichern (nicht bei Ausnahme)."""
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(state_dir / "state.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load(state_dir / "state.json")
        yield state
        save(state_dir / "state.json", state)
