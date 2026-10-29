# SPDX-License-Identifier: GPL-3.0-or-later
"""Texte für Telegram und die Eskalationsleiter (wann wird wie oft erinnert)."""

from __future__ import annotations

import logging
import time
from html import escape

from . import messages
from .config import Config, Escalation
from .state import Issue, State
from .telegram import Buttons, Telegram, TelegramError

log = logging.getLogger(__name__)

TITLES = {
    "manual": "Update braucht Freigabe",
    "reboot": "Neustart ausstehend",
    "error": "Update-Prüfung schlägt fehl",
    "apply-failed": "Update ist fehlgeschlagen",
    "stale": "Keine Update-Prüfung mehr",
}
# Das Aussehen ändert sich mit der Stufe, damit keine Gewöhnung an eine Form entsteht
ICONS = ("ℹ️", "⚠️", "🔴", "🚨")


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
        return "weniger als einer Stunde"
    if hours == 1:
        return "einer Stunde"
    if hours < 48:
        return f"{hours} Stunden"
    return f"{hours // 24} Tagen"


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0] + "\n…"


def format_reasons(reasons: list[str], limit: int = 15) -> str:
    lines = [f"• {r}" for r in reasons[:limit]]
    if len(reasons) > limit:
        lines.append(f"… und {len(reasons) - limit} weitere (/details)")
    return "\n".join(lines)


def _hint(issue: Issue) -> str:
    if issue.kind == "manual":
        if issue.approvable:
            return "Schaltflächen unten oder als Antwort /approve, /details, /later 1d."
        return "Bitte per SSH lösen (zypper dup). „Details“ zeigt den Konflikt."
    if issue.kind == "reboot":
        return "Schaltflächen unten oder als Antwort /reboot (Neustart in 1 Minute), /later 1d."
    return "Bitte per SSH nachsehen: journalctl -u twguard-check"


def issue_buttons(issue: Issue, later: bool = True) -> Buttons:
    """Schaltflächen zu einem offenen Punkt; die Freigabe ist an den Fingerabdruck gebunden."""
    later_row = [[("⏰ 1 Tag später", f"later:24:{issue.kind}")]] if later else []
    if issue.kind == "manual":
        if issue.approvable and issue.fingerprint:
            return [[("✅ Freigeben", f"approve:{issue.fingerprint}"), ("📋 Details", "details")], *later_row]
        return [[("📋 Details", "details"), ("🔄 Neu prüfen", "check")]]
    if issue.kind == "reboot":
        return [[("🔁 Neu starten", "reboot")], *later_row]
    return [[("🔄 Neu prüfen", "check"), ("📊 Status", "status")]]


def observed_buttons(fingerprint: str) -> Buttons:
    return [[("▶️ Diesmal einspielen", f"approve:{fingerprint}"), ("📋 Details", "details")]]


def format_issue(host: str, issue: Issue, now: float, esc: Escalation) -> str:
    current = level((now - issue.since) / 3600, ladder_for(issue.kind, esc)) or 0
    age = f"offen seit {fmt_age(now - issue.since)}"
    if issue.notify_count:
        age += f", {issue.notify_count}. Erinnerung"
    lines = [
        f"{ICONS[min(current, len(ICONS) - 1)]} <b>{escape(host)}</b>: {TITLES.get(issue.kind, issue.kind)}",
        f"<i>{age}</i>",
    ]
    if issue.detail:
        lines += ["", escape(clip(issue.detail, 2500))]
    lines += ["", escape(_hint(issue))]
    return "\n".join(lines)


def _issue_lines(st: State, now: float) -> list[str]:
    lines = []
    for issue in st.issues.values():
        line = f"• {TITLES.get(issue.kind, issue.kind)}, seit {fmt_age(now - issue.since)}"
        if issue.snoozed_until > now:
            line += time.strftime(" (aufgeschoben bis %d.%m. %H:%M)", time.localtime(issue.snoozed_until))
        lines.append(line)
    return lines


def _last_check(st: State, now: float) -> str:
    return f"letzte Prüfung vor {fmt_age(now - st.last_check_ok)}" if st.last_check_ok else "noch keine Prüfung"


def format_status(host: str, st: State, now: float) -> str:
    if not st.issues:
        return f"✅ <b>{escape(host)}</b>: nichts offen ({_last_check(st, now)})"
    return "\n".join([f"<b>{escape(host)}</b>:", *_issue_lines(st, now)])


def format_briefing(host: str, st: State, now: float, observe: bool, kernel: str, uptime: float) -> str:
    """Tägliche Zusammenfassung; kommt auch, wenn alles in Ordnung ist (Lebenszeichen)."""
    if st.issues:
        count = len(st.issues)
        lines = [f"☀️ <b>{escape(host)}</b>: {count} offene{'r' if count == 1 else ''} Punkt{'' if count == 1 else 'e'}",
                 *_issue_lines(st, now)]
    else:
        lines = [f"☀️ <b>{escape(host)}</b>: alles in Ordnung"]
    last = _last_check(st, now)
    info = [last[0].upper() + last[1:]]
    plan = st.last_plan_text.splitlines()[0] if st.last_plan_text else ""
    if observe and plan and not plan.startswith("0 Pakete"):
        info.append(f"ausstehend: {escape(plan)}")
    if st.reports:
        packages = sum(r.get("packages", 0) for r in st.reports)
        info.append(f"seit dem letzten Briefing {len(st.reports)}× eingespielt, {packages} Pakete")
    info.append(f"Kernel {escape(kernel)}, läuft seit {fmt_age(uptime)}")
    if observe:
        info.append("Beobachtungsmodus")
    return "\n".join([*lines, "", f"<i>{' · '.join(info)}</i>"])


def format_report(host: str, packages: int, approved: bool, reboot: bool, services: list[str],
                  deviation: list[str] | None = None, unverified: bool = False) -> str:
    lines = [f"✅ <b>{escape(host)}</b>: {packages} Pakete aktualisiert" + (" (freigegeben)" if approved else "")]
    if unverified:
        lines.append("⚠️ Den beim Einspielen ausgeführten Plan konnte twguard nicht lesen; ob genau der "
                     "geprüfte eingespielt wurde, ist unbekannt. Bitte journalctl -u twguard-check ansehen.")
    if deviation:
        shown = "\n".join(deviation[:30]) + (f"\n… und {len(deviation) - 30} weitere" if len(deviation) > 30 else "")
        lines.append("⚠️ Eingespielt wurde nicht genau der geprüfte Plan (zwischendurch geändert?). "
                     f"Bitte ansehen:\n<pre>{escape(clip(shown, 2500))}</pre>")
    if reboot:
        lines.append("Ein Neustart ist nötig.")
    if services:
        shown = ", ".join(services[:20]) + (" …" if len(services) > 20 else "")
        lines.append(f"Dienste mit alten Bibliotheken: {escape(shown)}")
    return "\n".join(lines)


def format_observed(host: str, packages: int, reboot: bool) -> str:
    lines = [f"👀 <b>{escape(host)}</b>: {packages} Pakete wären Routine und würden eingespielt.",
             "Beobachtungsmodus: Es wurde nichts verändert."]
    if reboot:
        lines.append("Danach wäre ein Neustart nötig.")
    return "\n".join(lines)


def adopt_migration(cfg: Config, st: State, tg: Telegram, old: int, new: int) -> None:
    """Die Gruppe ist zur Supergruppe geworden (neue ID): übernehmen, merken und einmal melden.

    Telegram vergibt dabei eine neue Chat-ID; ohne Übernahme würde der Bot alle Befehle
    aus der Gruppe als „nicht freigegeben“ verwerfen und seine Meldungen gingen ins Leere.
    """
    if old != cfg.chat_id or not new or st.chat_migration.get("to") == new:
        return
    log.warning("Gruppe %s ist jetzt Supergruppe %s; bitte chat_id in config.toml anpassen", old, new)
    st.chat_migration = {"from": old, "to": new, "at": time.time()}
    cfg.chat_id = tg.chat_id = new
    # Nachrichten in der alten Gruppe lassen sich nicht mehr bearbeiten oder löschen
    st.messages = {t: m for t, m in st.messages.items() if m.get("chat_id") != old}
    st.ephemeral = [m for m in st.ephemeral if m.get("chat_id") != old]
    st.reports = [{"at": r.get("at", 0), "packages": r.get("packages", 0)} if r.get("chat_id") == old else r
                  for r in st.reports]
    try:
        tg.send(f"ℹ️ <b>{escape(cfg.hostname)}</b>: Die Gruppe ist jetzt eine Supergruppe mit neuer ID "
                f"<code>{new}</code>. twguard nutzt sie ab sofort; bitte in /etc/twguard/config.toml "
                f"<code>chat_id = {new}</code> eintragen.", chat_id=new)
    except TelegramError as e:
        log.warning("Hinweis zur neuen Gruppen-ID nicht gesendet: %s", e)


def mark_resolved(cfg: Config, st: State, tg: Telegram, now: float | None = None) -> None:
    """Meldungen erledigter Punkte bleiben als „erledigt“ stehen, ohne Schaltflächen."""
    stamp = time.strftime("%d.%m. %H:%M", time.localtime(now or time.time()))
    for topic in [t for t in st.messages if t.startswith("issue:")]:
        kind = topic.removeprefix("issue:")
        if kind not in st.issues:
            messages.edit(tg, st.messages.pop(topic),
                          f"✅ <b>{escape(cfg.hostname)}</b>: {TITLES.get(kind, kind)} – erledigt ({stamp})")


def send_due(cfg: Config, st: State, tg: Telegram, now: float | None = None) -> None:
    """Erledigte Meldungen abhaken und fällige Erinnerungen senden.

    Eine neue Erinnerung ersetzt die vorige zum selben Punkt; sie zählt nur bei Erfolg als gesendet.
    """
    now = now or time.time()
    mark_resolved(cfg, st, tg, now)
    for issue in list(st.issues.values()):
        if not is_due(issue, now, cfg.escalation):
            continue
        try:
            messages.replace(tg, st, f"issue:{issue.kind}", format_issue(cfg.hostname, issue, now, cfg.escalation),
                             buttons=issue_buttons(issue))
        except TelegramError as e:
            log.warning("Erinnerung nicht gesendet: %s", e)
            continue
        issue.last_notified = now
        issue.notify_count += 1
