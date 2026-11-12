# SPDX-License-Identifier: GPL-3.0-or-later
"""Texte für Telegram und die Eskalationsleiter (wann wird wie oft erinnert)."""

from __future__ import annotations

import logging
import time
from html import escape

from . import restarts
from .config import MODES, Config, Escalation
from .i18n import LANGUAGES, set_language, tr, trn
from .state import Issue, State
from .state import load as load_state
from .telegram import Buttons, Telegram, TelegramError

log = logging.getLogger(__name__)

TITLES = {
    "manual": "Update braucht Freigabe",
    "reboot": "Neustart ausstehend",
    "services": "Dienste nutzen alte Dateien",
    "error": "Update-Prüfung schlägt fehl",
    "apply-failed": "Update ist fehlgeschlagen",
    "stale": "Keine Update-Prüfung mehr",
}
# Das Aussehen ändert sich mit der Stufe, damit keine Gewöhnung an eine Form entsteht
ICONS = ("ℹ️", "⚠️", "🔴", "🚨")
MAX_SERVICE_BUTTONS = 12
# Grenze von Telegram für die Daten einer Schaltfläche
MAX_CALLBACK_BYTES = 64


def use_language(cfg: Config) -> None:
    """Sprache für Prüflauf und Update: die mit /language gewählte, sonst die aus der Konfiguration."""
    try:
        override = load_state(cfg.state_dir / "state.json").language_override
    except (OSError, ValueError):
        override = ""  # z. B. ohne root kein Zugriff auf den Zustand
    set_language(override if override in LANGUAGES else cfg.language)


def use_mode(cfg: Config) -> None:
    """Modus für den Prüflauf: der mit /mode gewählte, sonst der aus der Konfiguration."""
    try:
        override = load_state(cfg.state_dir / "state.json").mode_override
    except (OSError, ValueError):
        override = ""
    if override in MODES:
        cfg.mode = override


def mode_value(mode: str) -> str:
    """Moduswert, wie er in config.toml und hinter /mode steht; als Code, damit er nicht wie
    Fließtext („Beobachtungsmodus“) wirkt."""
    return f"<code>{escape(mode)}</code>"


def title(kind: str) -> str:
    return tr(TITLES[kind]) if kind in TITLES else kind


def stamp(at: float) -> str:
    return time.strftime(tr("%d.%m. %H:%M"), time.localtime(at))


def ladder_for(kind: str, esc: Escalation) -> list[list[float]]:
    return esc.ladder_error if kind == "error" else esc.ladder


def level(age_hours: float, ladder: list[list[float]]) -> int | None:
    """Index der erreichten Stufe, None vor der ersten."""
    current = None
    for i, (start, _interval) in enumerate(ladder):
        if age_hours >= start:
            current = i
    return current


def minutes_of_day(hhmm: str) -> int:
    hours, minutes = hhmm.strip().split(":")
    return int(hours) * 60 + int(minutes)


def in_quiet_hours(now: float, spec: str) -> bool:
    if not spec:
        return False
    start, end = (minutes_of_day(x) for x in spec.split("-"))
    t = time.localtime(now)
    minute = t.tm_hour * 60 + t.tm_min
    if start <= end:
        return start <= minute < end
    return minute >= start or minute < end


def is_due(issue: Issue, now: float, esc: Escalation) -> bool:
    if issue.snoozed_until > now:
        return False
    ladder = ladder_for(issue.kind, esc)
    current = level((now - issue.since) / 3600, ladder)
    if current is None or in_quiet_hours(now, esc.quiet_hours):
        return False
    return now - issue.last_notified >= ladder[current][1] * 3600


def fmt_age(seconds: float) -> str:
    hours = int(seconds // 3600)
    if hours < 1:
        return tr("weniger als einer Stunde")
    if hours == 1:
        return tr("einer Stunde")
    if hours < 48:
        return tr("{n} Stunden", n=hours)
    return tr("{n} Tagen", n=hours // 24)


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0] + "\n…"


def format_reasons(reasons: list[str], limit: int = 15) -> str:
    lines = [f"• {r}" for r in reasons[:limit]]
    if len(reasons) > limit:
        lines.append(tr("… und {n} weitere (/details)", n=len(reasons) - limit))
    return "\n".join(lines)


def service_buttons(names: list[str]) -> Buttons:
    """Je Dienst eine Schaltfläche, soweit Platz ist; mehrere zusammen mit „Alle“."""
    single = [(f"🔁 {name}", f"restart:{name}") for name in names[:MAX_SERVICE_BUTTONS]
              if len(f"restart:{name}".encode()) <= MAX_CALLBACK_BYTES]
    rows = [single[i:i + 2] for i in range(0, len(single), 2)]
    if len(names) > 1 or not single:
        rows.append([(tr("🔁 Alle neu starten ({n})", n=len(names)), f"restart:*:{restarts.digest(names)}")])
    return rows


def shutdown_planned(st: State) -> bool:
    """Zuletzt gesehener Stand von logind (`busy.note_shutdown`); nur für die Anzeige."""
    return st.shutdown.get("state") == "planned"


def due_issues(st: State, now: float, esc: Escalation) -> list[Issue]:
    """Fällige Erinnerungen; ein schon geplanter Neustart erinnert nicht mehr an sich selbst."""
    return [issue for issue in st.issues.values() if is_due(issue, now, esc)
            and not (issue.kind == "reboot" and shutdown_planned(st))]


def lingering_line(service: str, procs: list[dict]) -> str:
    """Hinweis ohne Schaltfläche: Ein Neustart des Dienstes beendet diese Prozesse nicht."""
    names = [tr("{command} (PID {pid}, {user})", command=escape(clip(p["command"].removesuffix(" (deleted)"), 40)),
                pid=p["pid"], user=escape(clip(p["user"], 32))) for p in procs[:3]]
    if len(procs) > 3:
        names.append(tr("{n} weitere", n=len(procs) - 3))
    return "ℹ️ " + trn("Dienst {service} neu gestartet. Prozess {procs} nutzt bis zu seinem Ende weiter gelöschte Dateien.",
                      "Dienst {service} neu gestartet. Prozesse {procs} nutzen bis zu ihrem Ende weiter gelöschte Dateien.",
                      len(procs), service=escape(service), procs=", ".join(names))


UNVERIFIED = {"reboot": lambda: tr("Neustartbedarf"), "services": lambda: tr("Dienste mit alten Dateien")}


def format_report(host: str, packages: int, approved: bool, services: list[str],
                    deviation: list[str] | None = None, unverified: bool = False) -> str:
    """Ereignis nach dem Einspielen; ein nötiger Neustart steht als offener Punkt in der Zustandsnachricht."""
    lines = [f"✅ <b>{escape(host)}</b>: " + trn("{n} Paket aktualisiert", "{n} Pakete aktualisiert", packages)
             + (tr(" (freigegeben)") if approved else "")]
    if unverified:
        lines.append(tr("⚠️ Den beim Einspielen ausgeführten Plan konnte twguard nicht lesen; ob genau der "
                        "geprüfte eingespielt wurde, ist unbekannt. Bitte journalctl -u twguard-check ansehen."))
    if deviation:
        shown = "\n".join(deviation[:30])
        if len(deviation) > 30:
            shown += "\n" + tr("… und {n} weitere", n=len(deviation) - 30)
        lines.append(tr("⚠️ Eingespielt wurde nicht genau der geprüfte Plan (zwischendurch geändert?). "
                        "Bitte ansehen:\n<pre>{diff}</pre>", diff=escape(clip(shown, 2500))))
    if services:
        shown = ", ".join(services[:20]) + (" …" if len(services) > 20 else "")
        lines.append(tr("Ausgenommen vom Neustart, nutzen alte Bibliotheken: {services}", services=escape(shown)))
    return "\n".join(lines)


def adopt_migration(cfg: Config, st: State, tg: Telegram, old: int, new: int) -> None:
    """Die Gruppe ist zur Supergruppe geworden (neue ID): übernehmen, merken und einmal melden.

    Telegram vergibt dabei eine neue Chat-ID; ohne Übernahme würde der Bot alle Befehle
    aus der Gruppe als „nicht freigegeben“ verwerfen und seine Meldungen gingen ins Leere.
    """
    if old != cfg.chat_id or not new or st.chat_migration.get("to") == new:
        return
    log.warning(tr("Gruppe %s ist jetzt Supergruppe %s; bitte chat_id in config.toml anpassen"), old, new)
    st.chat_migration = {"from": old, "to": new, "at": time.time()}
    cfg.chat_id = tg.chat_id = new
    # Nachrichten in der alten Gruppe lassen sich nicht mehr bearbeiten oder löschen
    st.messages = {t: m for t, m in st.messages.items() if m.get("chat_id") != old}
    st.ephemeral = [m for m in st.ephemeral if m.get("chat_id") != old]
    st.stale_messages = [m for m in st.stale_messages if m.get("chat_id") != old]
    try:
        tg.send(f"ℹ️ <b>{escape(cfg.hostname)}</b>: " +
                tr("Die Gruppe ist jetzt eine Supergruppe mit neuer ID <code>{new}</code>. twguard nutzt sie ab sofort; "
                   "bitte in /etc/twguard/config.toml <code>chat_id = {new}</code> eintragen.", new=new), chat_id=new)
    except TelegramError as e:
        log.warning(tr("Hinweis zur neuen Gruppen-ID nicht gesendet: %s"), e)


def issue_identity(st: State, issue: Issue) -> tuple:
    """Woran sich erkennen lässt, ob ein Punkt noch so ist wie in einer gesendeten Nachricht.

    Ein neu angelegter Punkt hat ein anderes `since`, ein anderer Plan einen anderen Fingerabdruck.
    """
    services = tuple(st.restart_services) if issue.kind == "services" else ()
    return issue.since, issue.fingerprint, issue.detail, services


def note_reminded(issue: Issue, now: float) -> None:
    """Eine Erinnerung ist angekommen (die Zustandsnachricht wurde mit Ton neu gesendet).

    Welcher Plan gezeigt wurde (`manual_shown`), weiß nur der Sender: Er kennt den gesendeten Stand.
    """
    issue.last_notified = now
    issue.notify_count += 1
