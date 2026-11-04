# SPDX-License-Identifier: GPL-3.0-or-later
"""Zustandsnachricht und Ereignisse.

Je Server gibt es genau eine Zustandsnachricht (Thema "state"): alle offenen Punkte, was gerade läuft,
Hinweise, und als einzige Nachricht Schaltflächen. `render` baut sie aus dem Zustand neu auf und fragt
dabei nichts ab (kein Telegram, zypper oder systemctl); was der Bot dafür wissen muss (logind,
Sperrbesitzer), legt er vorher im Zustand ab.

Ereignisse (`event`) sind kurze Zeilen ohne Schaltflächen, die nachvollziehbar machen, was geschah.
Prüflauf und Update schreiben sie nur in den Zustand; gesendet wird alles vom Bot.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from html import escape, unescape
from typing import Sequence

from . import busy, details
from .config import Config
from .i18n import tr, trn
from .notify import ICONS, UNVERIFIED, clip, ladder_for, level, lingering_line, service_buttons, shutdown_planned, stamp, title
from .state import Issue, State
from .telegram import Buttons

TOPIC = "state"
# Telegram erlaubt 4096 Zeichen; HTML-Auszeichnungen zählen hier mit, das lässt Reserve
TEXT_LIMIT = 3800
DETAIL_LIMIT = 1200
UPDATE_COMMITS = 5


@dataclass(frozen=True)
class Panel:
    text: str
    buttons: Buttons
    # Inhalt ohne die Uhrzeit „Stand“: Ändert er sich, muss die Nachricht nachgeführt werden
    digest: str
    # Dasselbe für die Übersicht, auch wenn gerade Details gezeigt werden
    overview: str


def event(st: State, text: str, loud: bool = False, chat_id: int | None = None, now: float | None = None) -> None:
    """Ereignis zur Zustellung vormerken; mit `loud` kommt es mit Ton (etwas ist schiefgegangen).

    Jedes Ereignis passt allein in eine Nachricht. Ein zu langes wird als Klartext gekürzt, nicht im
    HTML: so bleibt kein Tag offen.
    """
    now = now or time.time()
    if len(text) > TEXT_LIMIT:
        plain = unescape(re.sub(r"<[^>]*>", "", text))
        text = clip_escaped(plain, TEXT_LIMIT - 200) + "\n" + tr("(gekürzt, siehe journalctl)")
    st.outbox.append({"id": f"{now:.6f}-{os.getpid()}-{len(st.outbox)}", "chat_id": chat_id, "message_id": None,
                      "at": now, "text": text, "loud": loud})


def clip_escaped(plain: str, limit: int) -> str:
    """Klartext für HTML maskieren und so kürzen, dass das Ergebnis höchstens `limit` Zeichen hat.

    Erst maskieren, dann kürzen: Maskieren verlängert den Text („<“ wird „&lt;“). Gekürzt wird an einer
    Zeilengrenze, sonst ohne ein angeschnittenes Entity.
    """
    text = escape(plain)
    if len(text) <= limit:
        return text
    cut = text[:limit - 2]
    if "\n" in cut:
        cut = cut.rsplit("\n", 1)[0]
    return re.sub(r"&[^;\s]*$", "", cut) + "\n…"


def line(host: str, icon: str, text: str) -> str:
    """Ereigniszeile: Symbol, Server, Text."""
    return f"{icon} <b>{escape(host)}</b>: {text}"


def _digest(text: str, buttons: Buttons) -> str:
    return hashlib.sha256(json.dumps([text, buttons], ensure_ascii=False).encode()).hexdigest()[:16]


def _issue_block(st: State, issue: Issue, now: float, cfg: Config) -> list[str]:
    current = level((now - issue.since) / 3600, ladder_for(issue.kind, cfg.escalation)) or 0
    head = f"{ICONS[min(current, len(ICONS) - 1)]} <b>{title(issue.kind)}</b> " + tr("seit {when}", when=stamp(issue.since))
    if issue.snoozed_until > now:
        head += " · " + tr("aufgeschoben bis {when}", when=stamp(issue.snoozed_until))
    lines = [head]
    if issue.kind == "services":
        lines.append(escape(clip(", ".join(st.restart_services), 600)))
    elif issue.kind == "manual":
        lines.append(escape(clip(issue.detail, DETAIL_LIMIT)))
        if not issue.approvable:
            lines.append(tr("Bitte per SSH lösen (zypper dup). „Details“ zeigt den Konflikt."))
    elif issue.kind == "apply-failed":
        lines.append(escape(clip(issue.detail, 600)))
        lines.append(tr("Bitte per SSH nachsehen (journalctl -u twguard-check), danach „Behoben“."))
    elif issue.kind in ("error", "stale"):
        lines.append(escape(clip(issue.detail, 600)))
    return lines


CHECK_PHASES = {
    "queued": lambda d: tr("🔄 Prüfung startet …"),
    "refresh": lambda d: tr("🔄 Prüfung läuft: Repos auffrischen"),
    "dry-run": lambda d: tr("🔄 Prüfung läuft: Probelauf"),
    "wait-zypp": lambda d: tr("⏳ Prüfung wartet auf die Paketverwaltung: {holder}", holder=escape(d)),
    "wait-restart": lambda d: tr("⏳ Einspielen wartet, bis die Dienste neu gestartet sind"),
    "apply": lambda d: tr("⏳ Spielt ein: {summary}", summary=escape(d)),
}


def _running_lines(st: State) -> list[str]:
    """Was gerade läuft; mit Schaltfläche nur der geplante Neustart (Abbrechen)."""
    lines = []
    check = st.activity.get("check")
    if check:
        lines.append(CHECK_PHASES.get(check.get("phase"), CHECK_PHASES["queued"])(check.get("detail", "")))
        if check.get("phase") == "queued" and st.approved_fingerprint:
            lines[-1] = tr("⏳ Freigegeben, Prüfung und Einspielen starten …")
    if st.restart_request:
        lines.append(tr("🔁 Dienste werden neu gestartet: {services}",
                        services=escape(", ".join(st.restart_request.get("services", [])))))
        if st.restart_request.get("overdue"):
            lines[-1] += " " + tr("(dauert ungewöhnlich lange, systemd arbeitet noch daran)")
    if st.update_request:
        # Ohne die Frist wirkt ein Bot, der nicht startet, bis zum Zurückrollen einfach tot
        request = st.update_request
        lines.append(tr("⬆️ Update von twguard auf {commit} läuft, der Bot antwortet so lange nicht. Tests und "
                        "Installation dauern einige Minuten, danach wartet twguard bis zu {seconds} s auf den neuen "
                        "Bot. Meldet er sich nicht oder schlägt die Installation fehl, wird die ursprüngliche Version "
                        "wiederhergestellt.", commit=escape(request.get("label") or request.get("commit", "")[:12]),
                        seconds=request.get("wait", 90)))
    if st.shutdown:
        lines.append(("🔁 " if shutdown_planned(st) else "❔ ") + busy.planned_text(st.shutdown))
    # Der eigene Prüflauf steht schon mit seiner Phase da; als „belegt“ wirkte er, als warte er auf sich selbst
    if st.zypp_holder and not busy.own_holder(st.zypp_holder) and (check or {}).get("phase") != "wait-zypp":
        lines.append(tr("🔒 Paketverwaltung belegt: {holder}. Prüfen und Einspielen warten darauf.",
                        holder=escape(st.zypp_holder)))
    return lines


def _hint_lines(cfg: Config, st: State, configured_mode: str) -> list[str]:
    lines = []
    if st.unverified:
        lines.append("❔ " + tr("Seit dem Boot noch nicht abgefragt: {what} (zypper gibt noch keine Auskunft)",
                               what=", ".join(UNVERIFIED[k]() for k in st.unverified if k in UNVERIFIED)))
    lines += [lingering_line(name, procs) for name, procs in st.lingering.items()]
    if cfg.mode != configured_mode:
        lines.append("⚙️ " + tr("Modus {mode} bis zum nächsten Neustart des Bots, in der Konfiguration {configured}.",
                               mode=cfg.mode, configured=configured_mode))
    return lines


def _offer_lines(st: State) -> list[str]:
    offer = st.update_offer
    if not offer:
        return []
    commits = offer.get("commits", [])
    shown = "\n".join(commits[:UPDATE_COMMITS])
    if len(commits) > UPDATE_COMMITS:
        shown += "\n" + tr("… und {n} weitere", n=len(commits) - UPDATE_COMMITS)
    return [tr("⬆️ twguard {target} verfügbar, {n} neue Commits (läuft mit {commit}):", target=escape(offer.get("label", "")),
               n=len(commits), commit=escape(offer.get("running", ""))), f"<pre>{escape(clip(shown, 800))}</pre>"]


def _buttons(st: State) -> Buttons:
    """Schaltflächen aller offenen Punkte; jede gehört zu dieser einen Nachricht."""
    rows: Buttons = []
    details_shown = False
    manual = st.issues.get("manual")
    if manual and manual.approvable and manual.fingerprint:
        rows.append([(tr("✅ Freigeben"), f"approve:{manual.fingerprint}"), ("📋 Details", "details")])
        details_shown = True
    elif manual:
        rows.append([("📋 Details", "details")])
        details_shown = True
    elif st.pending_text and st.observed_fingerprint:
        rows.append([(tr("▶️ Diesmal einspielen"), f"approve:{st.observed_fingerprint}"), ("📋 Details", "details")])
        details_shown = True
    if shutdown_planned(st):
        rows.append([(tr("✖️ Neustart abbrechen") if busy.Shutdown(**st.shutdown).reboot
                      else tr("✖️ Herunterfahren abbrechen"), "cancel")])
    elif "reboot" in st.issues:
        rows.append([(tr("🔁 Neu starten"), "reboot")])
    if "services" in st.issues and st.restart_services:
        rows += service_buttons(st.restart_services)
    if "apply-failed" in st.issues:
        rows.append([(tr("✅ Behoben"), "resolved:apply-failed")])
    if st.update_offer:
        rows.append([(tr("⬆️ Einspielen"), f"update:{st.update_offer['target']}"), (tr("✖️ Verwerfen"), "dismiss")])
    general = [(tr("🔄 Jetzt prüfen"), "check")]
    if not details_shown and (st.last_plan.get("rows") or st.last_plan.get("problems")):
        general.append(("📋 Details", "details"))
    if st.issues:
        general.append((tr("⏰ 1 Tag später"), "later:24"))
    # Was der Bot gerade abweisen würde, gar nicht erst anbieten; es kommt wieder, sobald es frei ist
    shown = ([button for button in row if busy.allowed(button[1], st)] for row in [*rows, general])
    return [row for row in shown if row]


def _head(host: str, st: State, now: float, briefing: bool) -> str:
    host = f"<b>{escape(host)}</b>"
    if st.issues:
        count = trn("{n} offener Punkt", "{n} offene Punkte", len(st.issues))
        return f"{'☀️' if briefing else '⚠️'} {host}: {count}"
    if st.pending_text:
        return f"{'☀️' if briefing else '👀'} {host}: " + tr("Updates ausstehend (Beobachtungsmodus)")
    # Ohne Auskunft von zypper ist „nichts offen“ keine Entwarnung
    icon = "☀️" if briefing else "❔" if st.unverified else "✅"
    return f"{icon} {host}: " + (tr("alles in Ordnung") if briefing else tr("nichts offen"))


def _footer(cfg: Config, st: State, kernel: str, booted: float) -> str:
    info = [tr("Letzte Prüfung {when}", when=stamp(st.last_check_ok)) if st.last_check_ok else tr("Noch keine Prüfung")]
    if kernel:
        info.append(tr("Kernel {kernel} seit {when}", kernel=escape(kernel), when=stamp(booted)))
    if cfg.observe:
        info.append(tr("Beobachtungsmodus"))
    return " · ".join(info)


def render(cfg: Config, st: State, now: float, configured_mode: str = "", kernel: str = "", booted: float = 0.0,
           due: Sequence[Issue] = (), briefing: bool = False, view: str = "") -> Panel:
    """Die Zustandsnachricht; `due` sind die fälligen Erinnerungen, `view` z. B. "details:name:2"."""
    configured_mode = configured_mode or cfg.mode
    head = [_head(cfg.hostname, st, now, briefing)]
    if due:
        head.append("🔔 " + tr("Erinnerung: {titles}", titles=", ".join(title(i.kind) for i in due)))
    # Abschnitte als (vollständig, kurz); jede Zeile ist für sich abgeschlossenes HTML
    blocks: list[tuple[list[str], list[str]]] = []
    for issue in st.issues.values():
        if issue.kind == "reboot" and shutdown_planned(st):
            continue  # steht als „geplant“ unter dem, was läuft
        full = _issue_block(st, issue, now, cfg)
        blocks.append((full, [full[0], "<i>" + tr("(Text gekürzt, zu viele offene Punkte)") + "</i>"]))
    if st.pending_text:
        pending = ["👀 " + tr("Ausstehend: {plan}", plan=escape(clip(st.pending_text, 300)))]
        blocks.append((pending, pending))
    offer = _offer_lines(st)
    if offer:
        blocks.append((offer, offer[:1]))
    for block in (_running_lines(st), _hint_lines(cfg, st, configured_mode)):
        if block:
            blocks.append((block, [clip_line(entry) for entry in block[:4]]))
    footer = _footer(cfg, st, kernel, booted)
    body = _fit(head, blocks, TEXT_LIMIT - len(footer) - 60)
    buttons = _buttons(st)
    overview = _digest(body + footer, buttons)
    if view.startswith("details"):
        text, buttons = details_page(cfg.hostname, st, view.partition(":")[2])
        return Panel(f"{text}\n\n<i>{tr('Stand {when}', when=stamp(now))}</i>", buttons, _digest(text, buttons), overview)
    text = f"{body}\n\n<i>{footer} · {tr('Stand {when}', when=stamp(now))}</i>"
    return Panel(text, buttons, overview, overview)


def clip_line(entry: str, limit: int = 300) -> str:
    """Eine Zeile mit Auszeichnung verkürzen: als Klartext, damit kein Tag offen bleibt."""
    if len(entry) <= limit:
        return entry
    return clip_escaped(unescape(re.sub(r"<[^>]*>", "", entry)).replace("\n", " "), limit)


def _fit(head: list[str], blocks: list[tuple[list[str], list[str]]], limit: int) -> str:
    """Kopf und Abschnitte bis `limit` Zeichen, ohne HTML zu zerschneiden.

    Reicht der Platz nicht, werden zuerst die längsten Abschnitte durch ihre Kurzform ersetzt (Titel
    bleiben sichtbar); reicht auch das nicht, entfallen die letzten Abschnitte mit einem Hinweis.
    """
    chosen = [full for full, _ in blocks]

    def text(parts: list[list[str]], more: bool = False) -> str:
        lines = list(head)
        for part in parts:
            lines += ["", *part]
        if more:
            lines += ["", "<i>" + tr("… weitere Angaben passen nicht in die Nachricht") + "</i>"]
        return "\n".join(lines)

    for index in sorted(range(len(blocks)), key=lambda i: -sum(len(x) for x in blocks[i][0])):
        if len(text(chosen)) <= limit:
            return text(chosen)
        chosen[index] = blocks[index][1]
    if len(text(chosen)) <= limit:
        return text(chosen)
    while chosen and len(text(chosen, more=True)) > limit:
        chosen.pop()
    return text(chosen, more=True)


def details_page(host: str, st: State, arg: str = "") -> tuple[str, Buttons]:
    """Eine Seite der Paketliste in der Zustandsnachricht; `arg` ist "ansicht:seite"."""
    view, _, number = arg.partition(":")
    view = view if view in details.VIEWS else details.VIEWS[0]
    head = f"📋 <b>{escape(host)}</b>"
    # Was offen ist, bleibt auch über der Paketliste sichtbar
    if st.issues:
        head += " · " + escape(", ".join(title(kind) for kind in st.issues))
    back = [(tr("◀️ Zurück"), "back")]
    plan = st.last_plan
    if not plan:
        return f"{head}\n<pre>{escape(clip(st.last_plan_text or tr('Noch kein Probelauf.'), 3000))}</pre>", [back]
    all_pages = details.pages(plan, view)
    page = min(max(int(number) if number.isdigit() else 1, 1), len(all_pages))
    other, label = ("version", tr("🔢 Nach Version")) if view == "name" else ("name", tr("🔤 Nach Name"))
    nav = [("⬅️", f"details:{view}:{page - 1}")] if page > 1 else []
    if len(all_pages) > 1:
        nav.append((f"{page}/{len(all_pages)}", f"details:{view}:{page}"))
    if page < len(all_pages):
        nav.append(("➡️", f"details:{view}:{page + 1}"))
    # Ohne Pakete (Solver-Konflikt) gibt es nichts umzuschalten oder als Datei zu schicken
    first = [(label, f"details:{other}:1"), (tr("📄 Als Datei"), "file")] if plan.get("rows") else []
    return f"{head}\n{all_pages[page - 1]}", [*([first] if first else []), *([nav] if nav else []), back]
