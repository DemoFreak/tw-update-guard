# SPDX-License-Identifier: GPL-3.0-or-later
"""Texte für Telegram und die Eskalationsleiter (wann wird wie oft erinnert)."""

from __future__ import annotations

import logging
import time
from html import escape

from . import messages
from .config import Config, Escalation
from .i18n import LANGUAGES, set_language, tr, trn
from .state import Issue, State
from .state import load as load_state
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


def use_language(cfg: Config) -> None:
    """Sprache für Prüflauf und Update: die mit /language gewählte, sonst die aus der Konfiguration."""
    try:
        override = load_state(cfg.state_dir / "state.json").language_override
    except (OSError, ValueError):
        override = ""  # z. B. ohne root kein Zugriff auf den Zustand
    set_language(override if override in LANGUAGES else cfg.language)


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


def _hint(issue: Issue) -> str:
    if issue.kind == "manual":
        if issue.approvable:
            return tr("Schaltflächen unten oder als Antwort /approve, /details, /later 1d.")
        return tr("Bitte per SSH lösen (zypper dup). „Details“ zeigt den Konflikt.")
    if issue.kind == "reboot":
        return tr("Schaltflächen unten oder als Antwort /reboot (Neustart in 1 Minute), /later 1d.")
    return tr("Bitte per SSH nachsehen: journalctl -u twguard-check")


def issue_buttons(issue: Issue, later: bool = True) -> Buttons:
    """Schaltflächen zu einem offenen Punkt; die Freigabe ist an den Fingerabdruck gebunden."""
    later_row = [[(tr("⏰ 1 Tag später"), f"later:24:{issue.kind}")]] if later else []
    if issue.kind == "manual":
        if issue.approvable and issue.fingerprint:
            return [[(tr("✅ Freigeben"), f"approve:{issue.fingerprint}"), ("📋 Details", "details")], *later_row]
        return [[("📋 Details", "details"), (tr("🔄 Neu prüfen"), "check")]]
    if issue.kind == "reboot":
        return [[(tr("🔁 Neu starten"), "reboot")], *later_row]
    return [[(tr("🔄 Neu prüfen"), "check"), ("📊 Status", "status")]]


def observed_buttons(fingerprint: str) -> Buttons:
    return [[(tr("▶️ Diesmal einspielen"), f"approve:{fingerprint}"), ("📋 Details", "details")]]


def format_issue(host: str, issue: Issue, now: float, esc: Escalation) -> str:
    current = level((now - issue.since) / 3600, ladder_for(issue.kind, esc)) or 0
    age = tr("offen seit {age}", age=fmt_age(now - issue.since))
    if issue.notify_count:
        age += tr(", {n}. Erinnerung", n=issue.notify_count)
    lines = [
        f"{ICONS[min(current, len(ICONS) - 1)]} <b>{escape(host)}</b>: {title(issue.kind)}",
        f"<i>{age}</i>",
    ]
    if issue.detail:
        lines += ["", escape(clip(issue.detail, 2500))]
    lines += ["", escape(_hint(issue))]
    return "\n".join(lines)


def _issue_lines(st: State, now: float) -> list[str]:
    lines = []
    for issue in st.issues.values():
        line = "• " + tr("{title}, seit {age}", title=title(issue.kind), age=fmt_age(now - issue.since))
        if issue.snoozed_until > now:
            line += tr(" (aufgeschoben bis {when})", when=stamp(issue.snoozed_until))
        lines.append(line)
    return lines


def _last_check(st: State, now: float) -> str:
    if st.last_check_ok:
        return tr("letzte Prüfung vor {age}", age=fmt_age(now - st.last_check_ok))
    return tr("noch keine Prüfung")


def format_status(host: str, st: State, now: float) -> str:
    pending = tr("ausstehend: {plan}", plan=escape(st.pending_text)) if st.pending_text else ""
    if not st.issues:
        if pending:
            return f"👀 <b>{escape(host)}</b>: {pending} ({_last_check(st, now)})"
        return f"✅ <b>{escape(host)}</b>: " + tr("nichts offen ({last})", last=_last_check(st, now))
    return "\n".join([f"<b>{escape(host)}</b>:", *_issue_lines(st, now), *([f"• {pending}"] if pending else [])])


def format_briefing(host: str, st: State, now: float, observe: bool, kernel: str, uptime: float) -> str:
    """Tägliche Zusammenfassung; kommt auch, wenn alles in Ordnung ist (Lebenszeichen)."""
    if st.issues:
        count = trn("{n} offener Punkt", "{n} offene Punkte", len(st.issues))
        lines = [f"☀️ <b>{escape(host)}</b>: {count}", *_issue_lines(st, now)]
    else:
        lines = [f"☀️ <b>{escape(host)}</b>: " + tr("alles in Ordnung")]
    last = _last_check(st, now)
    info = [last[0].upper() + last[1:]]
    if observe and st.pending_text:
        info.append(tr("ausstehend: {plan}", plan=escape(st.pending_text)))
    if st.reports:
        packages = sum(r.get("packages", 0) for r in st.reports)
        info.append(tr("seit dem letzten Briefing {times}× eingespielt, {packages}", times=len(st.reports),
                       packages=trn("{n} Paket", "{n} Pakete", packages)))
    info.append(tr("Kernel {kernel}, läuft seit {age}", kernel=escape(kernel), age=fmt_age(uptime)))
    if observe:
        info.append(tr("Beobachtungsmodus"))
    return "\n".join([*lines, "", f"<i>{' · '.join(info)}</i>"])


def format_report(host: str, packages: int, approved: bool, reboot: bool, services: list[str],
                  deviation: list[str] | None = None, unverified: bool = False) -> str:
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
    if reboot:
        lines.append(tr("Ein Neustart ist nötig."))
    if services:
        shown = ", ".join(services[:20]) + (" …" if len(services) > 20 else "")
        lines.append(tr("Dienste mit alten Bibliotheken: {services}", services=escape(shown)))
    return "\n".join(lines)


def format_observed(host: str, packages: int, reboot: bool) -> str:
    lines = [f"👀 <b>{escape(host)}</b>: " + trn("{n} Paket wäre Routine und würde eingespielt.",
                                                 "{n} Pakete wären Routine und würden eingespielt.", packages),
             tr("Beobachtungsmodus: Es wurde nichts verändert.")]
    if reboot:
        lines.append(tr("Danach wäre ein Neustart nötig."))
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
    st.reports = [{"at": r.get("at", 0), "packages": r.get("packages", 0)} if r.get("chat_id") == old else r
                  for r in st.reports]
    try:
        tg.send(f"ℹ️ <b>{escape(cfg.hostname)}</b>: " +
                tr("Die Gruppe ist jetzt eine Supergruppe mit neuer ID <code>{new}</code>. twguard nutzt sie ab sofort; "
                   "bitte in /etc/twguard/config.toml <code>chat_id = {new}</code> eintragen.", new=new), chat_id=new)
    except TelegramError as e:
        log.warning(tr("Hinweis zur neuen Gruppen-ID nicht gesendet: %s"), e)


def mark_resolved(cfg: Config, st: State, tg: Telegram, now: float | None = None) -> None:
    """Meldungen erledigter Punkte bleiben als „erledigt“ stehen, ohne Schaltflächen."""
    when = stamp(now or time.time())
    for topic in [t for t in st.messages if t.startswith("issue:")]:
        kind = topic.removeprefix("issue:")
        if kind not in st.issues:
            messages.edit(tg, st.messages.pop(topic),
                          f"✅ <b>{escape(cfg.hostname)}</b>: " + tr("{title} – erledigt ({when})", title=title(kind), when=when))


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
            log.debug(tr("Erinnerung nicht gesendet: %s"), e)
            continue
        issue.last_notified = now
        issue.notify_count += 1
