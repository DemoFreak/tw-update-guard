# SPDX-License-Identifier: GPL-3.0-or-later
"""Telegram-Bot (Dienst): beantwortet Befehle, schickt Erinnerungen und hält den Chat aufgeräumt.

Grundsatz: je Thema eine Nachricht. Antworten ersetzen ihre Vorgänger, Schaltflächen bearbeiten
die Nachricht, an der sie hängen, Befehle und kurzlebige Antworten werden wieder gelöscht.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from html import escape
from pathlib import Path

from . import COMMIT, __version__, details, messages, ota, state, zypper
from .config import Config
from .i18n import EN, LANGUAGES, current, set_language, tr
from .notify import (adopt_migration, clip, fmt_age, format_briefing, format_issue, format_status, issue_buttons,
                     mark_resolved, minutes_of_day, send_due, stamp, title)
from .telegram import Buttons, Telegram, TelegramError

log = logging.getLogger(__name__)

CHECK_UNIT = "twguard-check.service"
RPMDB_PATHS = (Path("/usr/lib/sysimage/rpm/rpmdb.sqlite"), Path("/var/lib/rpm/rpmdb.sqlite"))
# Ältere Befehle (z. B. nach Ausfall oder Neustart) werden nicht nachgeholt
MAX_COMMAND_AGE = 600
TICK_SECONDS = 60
REBOOT_CHECK_SECONDS = 1800
# Nach einem Fehler bei getUpdates: erst kurz, dann länger warten
RETRY_SECONDS = (2, 5, 15, 30)
# Briefing wird nachgeholt, wenn der Bot zur Uhrzeit nicht lief, aber nur in diesem Zeitfenster
BRIEFING_WINDOW_MINUTES = 6 * 60
# Lebensdauer kurzlebiger Antworten in Sekunden
SHORT = 120
LONG = 300

HELP = """Befehle. In der Gruppe als Antwort auf eine Nachricht dieses Bots oder mit @botname:
/status – offene Punkte (ohne @botname antworten alle Server)
/details – Paketliste des letzten Probelaufs
/approve – gemeldetes Update freigeben und einspielen
/check – sofort prüfen
/later 1d – Erinnerungen aufschieben (z. B. 12h, 2d)
/reboot – Neustart in 1 Minute
/cancel – geplanten Neustart abbrechen
/update – neuen Stand von twguard holen (ohne @botname: alle Server)
/version – Version und Modus
/language en – Sprache bis zum nächsten Neustart des Bots ändern (ohne @botname: alle Server)
/help – diese Hilfe (ohne @botname antwortet nur ein Server, gezielt jeder)
/keyboard – feste Tastatur einblenden (/keyboard off: ausblenden)

Die Tastatur „📊 Status“, „🔄 Alle prüfen“, „⬆️ Update“, „🏷️ Version“, „❓ Hilfe“ gilt für alle Server.
Meldungen haben dafür auch Schaltflächen."""

# Befehlsmenü neben der Eingabezeile
MENU = [
    ("status", "Offene Punkte (alle Server)"),
    ("check", "Sofort prüfen"),
    ("details", "Paketliste des letzten Probelaufs"),
    ("approve", "Gemeldetes Update freigeben"),
    ("later", "Erinnerungen aufschieben, z. B. 12h"),
    ("reboot", "Neustart in 1 Minute"),
    ("cancel", "Geplanten Neustart abbrechen"),
    ("update", "Neuen Stand von twguard holen"),
    ("version", "Version und Modus"),
    ("language", "Sprache bis zum Neustart ändern"),
    ("help", "Hilfe"),
    ("keyboard", "Feste Tastatur einblenden"),
]
# Feste Tastatur: Text des Knopfs → Befehl an alle Server
# Eine Taste sendet nur ihre Beschriftung; der Bot übersetzt sie hier in den Befehl.
# Eine neue Taste versteht ein Bot deshalb erst, wenn er selbst die Version mit ihr hat.
KEY_LABELS = {"📊 Status": "status", "🔄 Alle prüfen": "checkall",
              "⬆️ Update": "update", "🏷️ Version": "version", "❓ Hilfe": "help"}
# In einer Gruppe können Server verschiedener Sprachen laufen, die Tastatur stammt aber nur von
# einem Bot: Jeder versteht deshalb die Beschriftungen aller Sprachen
KEYS = {**KEY_LABELS, **{EN.get(label, label): command for label, command in KEY_LABELS.items()}}
# Die Tasten hängen an der Nachricht, die sie eingeblendet hat: wird sie gelöscht, verschwinden sie.
# Zwei Reihen, damit die Beschriftungen auch auf dem Telefon lesbar bleiben
def keyboard_markup() -> dict:
    labels = [tr(label) for label in KEY_LABELS]
    return {"keyboard": [[{"text": t} for t in row] for row in (labels[:2], labels[2:])],
            "is_persistent": True, "resize_keyboard": True}


@dataclass
class Out:
    """Antwort auf einen Befehl und wie mit ihr umzugehen ist."""

    text: str
    buttons: Buttons | None = None
    topic: str = ""  # ersetzt die vorige Nachricht zu diesem Thema
    ttl: float = 0  # löscht sich nach so vielen Sekunden
    start_check: bool = False  # Prüflauf starten; er schreibt sein Ergebnis in diese Nachricht
    markup: dict | None = None  # statt Schaltflächen, z. B. feste Tastatur


def parse_duration(text: str) -> float | None:
    """Dauer in Stunden aus "12h" oder "2d" (ohne Einheit: Tage)."""
    m = re.fullmatch(r"(\d+)\s*([hd]?)", text.strip().lower())
    if not m:
        return None
    return int(m.group(1)) * (1 if m.group(2) == "h" else 24)


def boot_time() -> float:
    with open("/proc/uptime") as f:
        return time.time() - float(f.read().split()[0])


def plain(html: str) -> str:
    """HTML-Text für Hinweisfenster von Schaltflächen (die kennen keine Formatierung)."""
    return re.sub(r"<[^>]+>", "", html)


class Bot:
    def __init__(self, cfg: Config, tg: Telegram | None = None, me: dict | None = None):
        set_language(cfg.language)
        self.cfg = cfg
        self.tg = tg or Telegram(cfg.token, cfg.chat_id, cfg.max_errors_per_hour)
        self.me = me
        self.offset = 0
        self.last_tick = 0.0
        self.last_reboot_check = 0.0
        self.delete_warned = False
        # Name → (Funktion(args, addressed), nur wenn gezielt angesprochen)
        self.commands = {
            "status": (self.cmd_status, False),
            "checkall": (self.cmd_check, False),
            "help": (self.cmd_help, False),
            "update": (self.cmd_update, False),
            "version": (self.cmd_version, False),
            "language": (self.cmd_language, False),
            "details": (self.cmd_details, True),
            "approve": (self.cmd_approve, True),
            "check": (self.cmd_check, True),
            "later": (self.cmd_later, True),
            "reboot": (self.cmd_reboot, True),
            "cancel": (self.cmd_cancel, True),
            "keyboard": (self.cmd_keyboard, True),
            "start": (self.cmd_help, True),
        }

    @property
    def host(self) -> str:
        return escape(self.cfg.hostname)

    # --- Ablauf -----------------------------------------------------------

    def run(self) -> None:
        while self.me is None:
            try:
                self.me = self.tg.get_me()
            except TelegramError as e:
                log.warning(tr("Telegram nicht erreichbar: %s"), e)
                time.sleep(30)
        log.info(tr("angemeldet als @%s"), self.me["username"])
        self.startup()
        failures = 0
        while True:
            # Kurzes Long-Polling (10 s): Antworten, die unterwegs verloren gehen, kosten so wenig Zeit
            try:
                updates = self.tg.get_updates(self.offset)
                failures = 0
            except TelegramError as e:
                log.debug("getUpdates: %s", e)
                time.sleep(RETRY_SECONDS[min(failures, len(RETRY_SECONDS) - 1)])
                failures += 1
                updates = []
            for update in updates:
                self.process(update)
            if time.time() - self.last_tick >= TICK_SECONDS:
                self.tick()

    def startup(self) -> None:
        self._set_menu()
        with state.locked(self.cfg.state_dir) as st:
            # Eine mit /language gewählte Sprache gilt nur bis zum Neustart
            st.language_override = ""
            self.offset = st.tg_offset
            st.first_seen = st.first_seen or time.time()
            if st.reboot_requested_at and boot_time() > st.reboot_requested_at:
                st.reboot_requested_at = 0.0
                text = tr("🟢 <b>{host}</b> ist wieder da (Kernel {kernel}).", host=self.host, kernel=escape(os.uname().release))
                previous = st.messages.pop("reboot", None)
                if previous:
                    messages.edit_or_send(self.tg, previous, text)
                else:
                    self._send(text)
            st.bot_commit = COMMIT
            self._report_update(st)
        self.check_reboot()

    def process(self, update: dict) -> None:
        # Offset vor der Ausführung sichern, damit z. B. /reboot nach dem Neustart nicht erneut greift
        self.offset = update["update_id"] + 1
        with state.locked(self.cfg.state_dir) as st:
            st.tg_offset = self.offset
        try:
            if "callback_query" in update:
                self.handle_callback(update["callback_query"])
            else:
                self.handle(update.get("message") or {})
        except Exception:
            log.exception(tr("Befehl fehlgeschlagen"))
        self.sync_migration()

    def sync_migration(self) -> None:
        """Einen beim Senden bemerkten Umzug zur Supergruppe übernehmen."""
        if self.tg.migration:
            old, new = self.tg.migration
            self.tg.migration = None
            self.migrate(old, new)

    def migrate(self, old: int, new: int) -> None:
        with state.locked(self.cfg.state_dir) as st:
            adopt_migration(self.cfg, st, self.tg, old, new)

    def tick(self) -> None:
        now = self.last_tick = time.time()
        mtime = self._rpmdb_mtime()
        with state.locked(self.cfg.state_dir) as st:
            rpmdb_changed = bool(st.rpmdb_mtime) and mtime != st.rpmdb_mtime
            st.rpmdb_mtime = mtime
            pending = bool(st.pending_text) or any(k in st.issues for k in ("manual", "apply-failed"))
        if rpmdb_changed:
            # Jemand hat Pakete geändert, z. B. per SSH selbst `zypper dup` ausgeführt
            self.check_reboot()
            if pending:
                self.start_check()
        elif now - self.last_reboot_check >= REBOOT_CHECK_SECONDS:
            self.check_reboot()

        self.sync_migration()
        with state.locked(self.cfg.state_dir) as st:
            base = st.last_check_ok or st.first_seen
            stale = base and now - base > self.cfg.escalation.stale_after_hours * 3600
            if stale and "stale" not in st.issues and "error" not in st.issues:
                st.open_issue("stale", tr("Seit {age} keine erfolgreiche Prüfung. Läuft twguard-check.timer?",
                                          age=fmt_age(now - base)), now=now)
            elif not stale:
                st.resolve("stale")
            self._report_update(st, now)
            send_due(self.cfg, st, self.tg, now)
            messages.cleanup(self.tg, st, now)
        self.maybe_briefing(now)

    def maybe_briefing(self, now: float) -> None:
        if not self.cfg.briefing.at:
            return
        t = time.localtime(now)
        since_start = t.tm_hour * 60 + t.tm_min - minutes_of_day(self.cfg.briefing.at)
        if not 0 <= since_start < BRIEFING_WINDOW_MINUTES:
            return
        today = time.strftime("%Y-%m-%d", t)
        with state.locked(self.cfg.state_dir) as st:
            if st.last_briefing_day == today:
                return
            text = format_briefing(self.cfg.hostname, st, now, self.cfg.observe,
                                   os.uname().release, now - boot_time())
            try:
                messages.replace(self.tg, st, "briefing", text,
                                 buttons=[[("📊 Status", "status"), (tr("🔄 Jetzt prüfen"), "check")]],
                                 silent=self.cfg.briefing.silent and not st.issues)
            except TelegramError as e:
                log.debug(tr("Briefing nicht gesendet: %s"), e)
                return
            st.last_briefing_day = today
            # Die Update-Berichte stehen jetzt im Briefing
            for report in st.reports:
                if report.get("message_id"):
                    messages.retire(self.tg, report, tr("✅ eingespielt, siehe Briefing"))
            st.reports = []

    def check_reboot(self) -> None:
        self.last_reboot_check = time.time()
        needed = zypper.needs_rebooting()
        if needed is None:
            return
        with state.locked(self.cfg.state_dir) as st:
            if needed and "reboot" not in st.issues:
                st.open_issue("reboot", tr("zypper needs-rebooting meldet einen ausstehenden Neustart."))
            elif not needed and st.resolve("reboot"):
                mark_resolved(self.cfg, st, self.tg)

    def start_check(self) -> None:
        subprocess.run(["systemctl", "start", "--no-block", CHECK_UNIT], check=False)

    # --- Befehle ----------------------------------------------------------

    def handle(self, msg: dict) -> None:
        chat = msg.get("chat") or {}
        # Systemnachrichten beim Umzug zur Supergruppe (in der alten bzw. der neuen Gruppe)
        if msg.get("migrate_to_chat_id") and chat.get("id") == self.cfg.chat_id:
            self.migrate(self.cfg.chat_id, msg["migrate_to_chat_id"])
            return
        if msg.get("migrate_from_chat_id") and msg["migrate_from_chat_id"] == self.cfg.chat_id:
            self.migrate(self.cfg.chat_id, chat.get("id"))
            return
        text = (msg.get("text") or "").strip()
        # Tastatur-Tasten schickt Telegram in Gruppen als Antwort auf die Tastatur-Nachricht;
        # sie gelten trotzdem für alle Server
        key = text in KEYS
        if key:
            text = "/" + KEYS[text]
        if not text.startswith("/"):
            return
        if msg.get("date", 0) < time.time() - MAX_COMMAND_AGE:
            log.info(tr("veralteten Befehl ignoriert: %s"), text)
            return
        user = (msg.get("from") or {}).get("id")
        if user not in self.cfg.allowed_user_ids or chat.get("id") not in (self.cfg.chat_id, user):
            log.warning(tr("Befehl von %s in Chat %s ignoriert (nicht freigegeben)"), user, chat.get("id"))
            if user in self.cfg.allowed_user_ids and chat.get("type") == "supergroup":
                log.warning(tr("Falls die Gruppe zur Supergruppe wurde: chat_id = %s in config.toml eintragen "
                            "(bisher %s)"), chat.get("id"), self.cfg.chat_id)
            return

        word, *args = text.split()
        cmd, _, target = word[1:].partition("@")
        if target and target.lower() != self.me["username"].lower():
            return
        reply_from = {} if key else (msg.get("reply_to_message") or {}).get("from") or {}
        if not target and reply_from.get("is_bot") and reply_from.get("id") != self.me["id"]:
            return  # Antwort an einen anderen Server
        addressed = bool(target) or chat.get("type") == "private" or reply_from.get("id") == self.me["id"]

        entry = self.commands.get(cmd.lower())
        if entry is None:
            if addressed:
                self._deliver(msg, Out(tr("Unbekannter Befehl. /help zeigt die Befehle."), ttl=SHORT))
                self._delete_command(msg)
            return
        func, needs_address = entry
        if needs_address and not addressed:
            return
        self._typing(chat["id"])
        self._deliver(msg, func(args, addressed))
        self._delete_command(msg)

    def cmd_status(self, args: list[str], addressed: bool = False) -> Out:
        text, buttons = self.status_view()
        return Out(text, buttons, topic="status")

    def status_view(self) -> tuple[str, Buttons]:
        with state.locked(self.cfg.state_dir) as st:
            text = format_status(self.cfg.hostname, st, time.time())
        if self.cfg.observe:
            text += f"\n<i>{tr('Beobachtungsmodus: Updates werden nur gemeldet.')}</i>"
        return text, [[(tr("🔄 Jetzt prüfen"), "check"), ("📋 Details", "details")]]

    def details_view(self, arg: str = "", back: bool = False) -> tuple[str, Buttons]:
        """Eine Seite der Paketliste; `arg` ist "ansicht:seite" aus der Schaltfläche."""
        view, _, number = arg.partition(":")
        view = view if view in details.VIEWS else details.VIEWS[0]
        with state.locked(self.cfg.state_dir) as st:
            plan, text = st.last_plan, st.last_plan_text
        back_row = [[(tr("◀️ Zurück"), "back")]] if back else []
        if not plan:
            # Noch kein Prüflauf seit dem Update auf diese Version
            text = text or tr("Noch kein Probelauf.")
            return f"<b>{self.host}</b>\n<pre>{escape(clip(text, 3500))}</pre>", back_row
        all_pages = details.pages(plan, view)
        page = min(max(int(number) if number.isdigit() else 1, 1), len(all_pages))
        other, label = ("version", tr("🔢 Nach Version")) if view == "name" else ("name", tr("🔤 Nach Name"))
        nav = [("⬅️", f"details:{view}:{page - 1}")] if page > 1 else []
        if len(all_pages) > 1:
            nav.append((f"{page}/{len(all_pages)}", f"details:{view}:{page}"))
        if page < len(all_pages):
            nav.append(("➡️", f"details:{view}:{page + 1}"))
        first = [(label, f"details:{other}:1")]
        if plan.get("rows"):
            first.append((tr("📄 Als Datei"), "file"))
        rows = [first, *([nav] if nav else []), *back_row]
        return f"<b>{self.host}</b>\n{all_pages[page - 1]}", rows

    def cmd_details(self, args: list[str], addressed: bool = False) -> Out:
        text, buttons = self.details_view()
        return Out(text, buttons, ttl=LONG)

    def cmd_approve(self, args: list[str], addressed: bool = False) -> Out:
        error = self.approve()
        if error:
            return Out(error, ttl=SHORT)
        return Out(self.approved_text(), topic="status", start_check=True)

    def approved_text(self) -> str:
        return tr("⏳ Freigegeben. <b>{host}</b> spielt das Update ein; das Ergebnis erscheint hier.", host=self.host)

    def approve(self, fingerprint: str = "") -> str:
        """Freigabe erteilen; Rückgabe: Fehlertext oder leer. Mit Fingerabdruck nur für genau diesen Stand."""
        with state.locked(self.cfg.state_dir) as st:
            issue = st.issues.get("manual")
            if issue is None and fingerprint and fingerprint == st.observed_fingerprint:
                # Beobachtungsmodus: einzelnes Routine-Update trotzdem einspielen
                st.approved_fingerprint = fingerprint
            elif issue is None:
                return tr("Es liegt kein Update zur Freigabe vor.")
            elif not issue.approvable or not issue.fingerprint:
                return tr("Das lässt sich nicht per Telegram freigeben, bitte per SSH lösen.")
            elif fingerprint and fingerprint != issue.fingerprint:
                return tr("Diese Meldung gilt einem älteren Stand. /status zeigt den aktuellen.")
            else:
                st.approved_fingerprint = issue.fingerprint
        return ""

    def checking_text(self) -> str:
        return tr("🔄 <b>{host}</b>: Prüfung läuft (Repos auffrischen, Probelauf); "
                  "das Ergebnis erscheint hier, meist nach ein bis zwei Minuten.", host=self.host)

    def cmd_check(self, args: list[str], addressed: bool = False) -> Out:
        return Out(self.checking_text(), topic="status", start_check=True)

    def cmd_later(self, args: list[str], addressed: bool = False) -> Out:
        hours = parse_duration(args[0]) if args else 24
        if hours is None:
            return Out(tr("Format: /later 12h oder /later 2d"), ttl=SHORT)
        return Out(self.snooze(hours), ttl=SHORT)

    def snooze(self, hours: float, kinds: list[str] | None = None) -> str:
        esc = self.cfg.escalation
        hours = min(hours, esc.max_snooze_hours)
        now = time.time()
        done, refused = [], []
        with state.locked(self.cfg.state_dir) as st:
            issues = [i for i in st.issues.values() if kinds is None or i.kind in kinds]
            if not issues:
                return tr("Es ist nichts offen.")
            for issue in issues:
                if issue.snooze_count >= esc.max_snoozes:
                    refused.append(title(issue.kind))
                    continue
                issue.snoozed_until = now + hours * 3600
                issue.snooze_count += 1
                done.append(title(issue.kind))
        lines = []
        if done:
            lines.append(tr("Aufgeschoben um {hours} Stunden: {titles}", hours=f"{hours:g}", titles=", ".join(done)))
        if refused:
            lines.append(tr("Nicht weiter aufschiebbar: {titles}", titles=", ".join(refused)))
        return "\n".join(lines)

    def reboot(self) -> str:
        """Neustart in einer Minute planen; Rückgabe: Fehlertext oder leer."""
        p = subprocess.run(["shutdown", "-r", "+1", "twguard: Neustart per Telegram"],
                           capture_output=True, text=True)
        if p.returncode:
            return tr("Neustart fehlgeschlagen: {error}", error=escape(p.stderr.strip()))
        with state.locked(self.cfg.state_dir) as st:
            st.reboot_requested_at = time.time()
        return ""

    def reboot_view(self) -> tuple[str, Buttons]:
        return (tr("🔁 <b>{host}</b> startet in 1 Minute neu; danach steht das Ergebnis hier.", host=self.host),
                [[(tr("✖️ Neustart abbrechen"), "cancel")]])

    def cmd_reboot(self, args: list[str], addressed: bool = False) -> Out:
        error = self.reboot()
        if error:
            return Out(error, ttl=SHORT)
        text, buttons = self.reboot_view()
        return Out(text, buttons, topic="reboot")

    def cancel(self) -> str:
        """Geplanten Neustart abbrechen; Rückgabe: Text für die Neustart-Nachricht."""
        p = subprocess.run(["shutdown", "-c"], capture_output=True, text=True)
        with state.locked(self.cfg.state_dir) as st:
            st.reboot_requested_at = 0.0
        if p.returncode:
            return tr("Abbrechen fehlgeschlagen: {error}", error=escape(p.stderr.strip()))
        return tr("✖️ <b>{host}</b>: Neustart abgebrochen.", host=self.host)

    def cmd_cancel(self, args: list[str], addressed: bool = False) -> Out | None:
        text = self.cancel()
        with state.locked(self.cfg.state_dir) as st:
            previous = st.messages.pop("reboot", None)
        if previous and messages.edit(self.tg, previous, text):
            return None
        return Out(text, ttl=SHORT)

    def cmd_update(self, args: list[str], addressed: bool = False) -> Out:
        """Neuen Stand holen und zur Bestätigung anbieten."""
        with state.locked(self.cfg.state_dir) as st:
            if st.update_request:
                return Out(tr("<b>{host}</b>: Ein Update läuft bereits.", host=self.host), ttl=SHORT)
        try:
            pending = ota.check_remote(self.cfg.update)
        except ota.UpdateError as e:
            return Out(f"<b>{self.host}</b>: {escape(str(e))}", ttl=LONG)
        if not pending.commits:
            return Out(tr("<b>{host}</b>: twguard ist aktuell ({commit}).", host=self.host, commit=escape(COMMIT)), ttl=SHORT)
        shown = "\n".join(pending.commits[:15])
        if len(pending.commits) > 15:
            shown += "\n" + tr("… und {n} weitere", n=len(pending.commits) - 15)
        text = tr("⬆️ <b>{host}</b>: {n} neue Commits für twguard (läuft mit {commit}):\n<pre>{commits}</pre>",
                  host=self.host, n=len(pending.commits), commit=escape(COMMIT), commits=escape(clip(shown, 2500)))
        return Out(text, [[(tr("⬆️ Einspielen"), f"update:{pending.target}"), (tr("✖️ Abbrechen"), "dismiss")]],
                   topic="update")

    def update_go(self, commit: str, pressed: dict) -> str:
        """Bestätigtes Update starten; die gedrückte Nachricht wird später zum Ergebnis.

        Rückgabe: Fehlertext oder leer.
        """
        try:
            pending = ota.check_remote(self.cfg.update)
        except ota.UpdateError as e:
            return f"<b>{self.host}</b>: {escape(str(e))}"
        if pending.target != commit:
            return tr("Inzwischen gibt es einen anderen Stand. Bitte /update erneut.")
        if not pending.commits:
            return tr("<b>{host}</b>: twguard ist schon aktuell.", host=self.host)
        with state.locked(self.cfg.state_dir) as st:
            if st.update_request:
                return tr("Ein Update läuft bereits.")
            messages.release(st, pressed["message_id"])
            st.update_request = {"commit": commit, "chat_id": pressed["chat_id"],
                                 "message_id": pressed["message_id"], "at": time.time()}
        error = ota.start(commit)
        if error:
            with state.locked(self.cfg.state_dir) as st:
                st.update_request = {}
            return tr("Update nicht gestartet: {error}", error=escape(error))
        return ""

    def cmd_version(self, args: list[str], addressed: bool = False) -> Out:
        return Out(self.version_line(), ttl=LONG)

    def version_line(self) -> str:
        mode = tr("Beobachtung") if self.cfg.observe else tr("automatisch")
        return (f"🖥 <b>{self.host}</b> (@{escape(self.me['username'])}) · twguard {escape(__version__)} "
                f"({escape(COMMIT)}) · {mode}")

    def cmd_language(self, args: list[str], addressed: bool = False) -> Out:
        """Sprache zur Laufzeit ändern; die Konfiguration bleibt unberührt, ein Neustart gilt wieder sie."""
        configured = self.cfg.language
        if not args:
            return Out(tr("🌐 <b>{host}</b>: Sprache {language}, in der Konfiguration {configured}. "
                          "Ändern: /language en oder /language de", host=self.host, language=current(),
                          configured=configured), ttl=LONG)
        language = args[0].lower()
        if language not in LANGUAGES:
            return Out(tr("Format: /language en oder /language de"), ttl=SHORT)
        set_language(language)
        with state.locked(self.cfg.state_dir) as st:
            st.language_override = "" if language == configured else language
        self._set_menu()
        self._renew_keyboard()
        if language == configured:
            return Out(tr("🌐 <b>{host}</b>: Sprache {language}, wie in der Konfiguration.",
                          host=self.host, language=language), topic="language")
        return Out(tr("🌐 <b>{host}</b>: Sprache {language} bis zum nächsten Neustart des Bots. Die Konfiguration "
                      "enthält {configured}. Damit es dauerhaft gilt, in /etc/twguard/config.toml unter [general] "
                      "<code>language = \"{language}\"</code> eintragen.",
                      host=self.host, language=language, configured=configured), topic="language")

    def cmd_help(self, args: list[str], addressed: bool = False) -> Out | None:
        # An alle antwortet nur der Bot mit help_full, sonst stünde die Liste mehrfach da;
        # welche Server erreichbar sind, zeigen /status und /version
        if not (addressed or self.cfg.help_full):
            return None
        return Out(self.version_line() + "\n\n" + escape(tr(HELP)), topic="help", ttl=LONG)

    def cmd_keyboard(self, args: list[str], addressed: bool = False) -> Out | None:
        """Tastatur ein- oder ausblenden; die Nachricht dazu ist die einzige, die stehen bleiben muss."""
        with state.locked(self.cfg.state_dir) as st:
            previous = st.messages.pop("keyboard", None)
        removed = bool(previous) and messages.delete(self.tg, previous)
        if args and args[0].lower() in ("off", "aus"):
            if removed:
                return None  # mit der Nachricht sind auch die Tasten weg
            if previous:
                return Out(tr("Die Tastatur-Nachricht ist älter als 48 Stunden; bitte von Hand löschen."), ttl=SHORT)
            return Out(tr("Dieser Bot hat keine Tastatur eingeblendet."), ttl=SHORT)
        return Out(self.keyboard_text(), markup=keyboard_markup(), topic="keyboard")

    def keyboard_text(self) -> str:
        return tr("⌨️ Tastatur für alle Server. Diese Nachricht nicht löschen, sonst verschwinden die Tasten "
                  "(/keyboard off blendet sie aus).")

    # --- Schaltflächen ----------------------------------------------------

    def handle_callback(self, cq: dict) -> None:
        """Druck auf eine Schaltfläche. Telegram stellt ihn nur dem Bot zu, der die Nachricht sandte.

        Die Antwort erscheint in der Nachricht selbst, statt eine neue zu senden.
        """
        msg = cq.get("message") or {}
        user = (cq.get("from") or {}).get("id")
        chat = msg.get("chat") or {}
        if user not in self.cfg.allowed_user_ids or chat.get("id") not in (self.cfg.chat_id, user):
            log.warning(tr("Schaltfläche von %s in Chat %s ignoriert (nicht freigegeben)"), user, chat.get("id"))
            self._answer(cq, tr("Nicht berechtigt."))
            return
        pressed = {"chat_id": chat.get("id"), "message_id": msg.get("message_id"), "at": msg.get("date", 0)}
        action, _, arg = (cq.get("data") or "").partition(":")
        handler = {
            "approve": self.press_approve,
            "details": self.press_details,
            "file": self.press_file,
            "back": self.press_back,
            "later": self.press_later,
            "check": self.press_check,
            "status": self.press_status,
            "reboot": self.press_reboot,
            "cancel": self.press_cancel,
            "update": self.press_update,
            "dismiss": self.press_dismiss,
        }.get(action)
        if handler is None:
            self._answer(cq, tr("Unbekannte Schaltfläche."))
            return
        handler(cq, pressed, arg)

    def press_approve(self, cq: dict, pressed: dict, fingerprint: str) -> None:
        error = self.approve(fingerprint)
        if error:
            self._answer(cq, plain(error), alert=True)
            return
        self._answer(cq, tr("Freigegeben"))
        self._take_over(pressed, self.approved_text(), request=True)
        self.start_check()

    def press_details(self, cq: dict, pressed: dict, arg: str) -> None:
        self._answer(cq)
        msg = cq.get("message") or {}
        with state.locked(self.cfg.state_dir) as st:
            # Ursprünglichen Inhalt merken, damit „Zurück“ ihn genau so wiederherstellt;
            # Umschalten und Blättern (mit `arg`) zeigen schon die Liste
            if not arg:
                st.collapsed[str(pressed["message_id"])] = {
                    "text": msg.get("text", ""),
                    "entities": msg.get("entities", []),
                    "reply_markup": msg.get("reply_markup", {}),
                    "at": time.time(),
                }
            back = str(pressed["message_id"]) in st.collapsed
        messages.edit(self.tg, pressed, *self.details_view(arg, back))

    def press_file(self, cq: dict, pressed: dict, arg: str) -> None:
        """Die ganze Paketliste als HTML-Datei; eine neuere ersetzt die vorige."""
        with state.locked(self.cfg.state_dir) as st:
            plan, at = st.last_plan, st.last_check_ok or time.time()
        if not plan.get("rows"):
            self._answer(cq, tr("Keine Paketliste vorhanden."))
            return
        html_text = details.file_html(plan, self.cfg.hostname, at)
        try:
            sent = self.tg.send_document(pressed["chat_id"], details.file_name(self.cfg.hostname, at),
                                         html_text.encode(), f"📄 <b>{self.host}</b>: {escape(plan['summary'])}")
        except TelegramError as e:
            log.debug(tr("Datei nicht gesendet: %s"), e)
            self._answer(cq, tr("Datei konnte nicht gesendet werden."), alert=True)
            return
        self._answer(cq)
        with state.locked(self.cfg.state_dir) as st:
            old = st.messages.get("file")
            messages.claim(st, "file", messages.ref(sent))
        if old:
            messages.delete(self.tg, old)

    def press_back(self, cq: dict, pressed: dict, arg: str) -> None:
        with state.locked(self.cfg.state_dir) as st:
            original = st.collapsed.pop(str(pressed["message_id"]), None)
        if original is None:
            self._answer(cq, tr("Nicht mehr verfügbar."))
            messages.edit(self.tg, pressed, self.details_view()[0])
            return
        self._answer(cq)
        try:
            self.tg.edit_raw(pressed["chat_id"], pressed["message_id"], original["text"],
                             original["entities"], original["reply_markup"])
        except TelegramError as e:
            log.warning(tr("Details nicht zurückgeklappt: %s"), e)

    def press_later(self, cq: dict, pressed: dict, arg: str) -> None:
        hours_text, _, kind = arg.partition(":")
        self._answer(cq, self.snooze(float(hours_text or 24), [kind] if kind else None))
        with state.locked(self.cfg.state_dir) as st:
            issue = st.issues.get(kind)
            if issue is None or issue.snoozed_until <= time.time():
                return
            text = format_issue(self.cfg.hostname, issue, time.time(), self.cfg.escalation)
            text += tr("\n\n⏰ Aufgeschoben bis {when}", when=stamp(issue.snoozed_until))
            buttons = issue_buttons(issue, later=False)
        messages.edit(self.tg, pressed, text, buttons)

    def press_check(self, cq: dict, pressed: dict, arg: str) -> None:
        self._answer(cq, tr("Prüfung läuft …"))
        with state.locked(self.cfg.state_dir) as st:
            on_status = st.messages.get("status", {}).get("message_id") == pressed["message_id"]
        if on_status:
            # Die Statusnachricht selbst wird zur Wartemeldung und dann zum Ergebnis
            self._take_over(pressed, self.checking_text(), request=True, topic="status")
            self.start_check()
        else:
            # Briefing und Meldungen bleiben stehen; das Ergebnis kommt in eine neue Statusnachricht
            self._deliver({"chat": {"id": pressed["chat_id"]}}, self.cmd_check([]))

    def press_status(self, cq: dict, pressed: dict, arg: str) -> None:
        self._answer(cq)
        text, buttons = self.status_view()
        with state.locked(self.cfg.state_dir) as st:
            on_status = st.messages.get("status", {}).get("message_id") == pressed["message_id"]
        if on_status:
            messages.edit(self.tg, pressed, text, buttons)
        else:
            self._deliver({"chat": {"id": pressed["chat_id"]}}, Out(text, buttons, topic="status"))

    def press_reboot(self, cq: dict, pressed: dict, arg: str) -> None:
        error = self.reboot()
        if error:
            self._answer(cq, plain(error), alert=True)
            return
        self._answer(cq, tr("Neustart in 1 Minute"))
        text, buttons = self.reboot_view()
        self._take_over(pressed, text, buttons, topic="reboot")

    def press_cancel(self, cq: dict, pressed: dict, arg: str) -> None:
        text = self.cancel()
        self._answer(cq, plain(text))
        with state.locked(self.cfg.state_dir) as st:
            messages.release(st, pressed["message_id"])
        messages.edit(self.tg, pressed, text)

    def press_update(self, cq: dict, pressed: dict, commit: str) -> None:
        error = self.update_go(commit, pressed)
        if error:
            self._answer(cq, plain(error), alert=True)
            messages.edit(self.tg, pressed, error)
            return
        self._answer(cq, tr("Update läuft …"))
        messages.edit(self.tg, pressed, tr("⏳ <b>{host}</b>: Update auf {commit} läuft, der Bot startet gleich neu; "
                                           "das Ergebnis erscheint hier.", host=self.host, commit=escape(commit[:12])))

    def press_dismiss(self, cq: dict, pressed: dict, arg: str) -> None:
        self._answer(cq)
        with state.locked(self.cfg.state_dir) as st:
            messages.release(st, pressed["message_id"])
        if not messages.delete(self.tg, pressed):
            messages.edit(self.tg, pressed, tr("✖️ verworfen"))

    # --- Hilfsfunktionen --------------------------------------------------

    def _set_menu(self) -> None:
        try:
            self.tg.set_commands([(command, tr(text)) for command, text in MENU])
        except TelegramError as e:
            log.warning(tr("Befehlsmenü nicht gesetzt: %s"), e)

    def _renew_keyboard(self) -> None:
        """Eine eingeblendete Tastatur in der neuen Sprache senden; die alte versteht der Bot weiterhin."""
        with state.locked(self.cfg.state_dir) as st:
            old = st.messages.get("keyboard")
            if not old:
                return
            try:
                messages.replace(self.tg, st, "keyboard", self.keyboard_text(), chat_id=old["chat_id"],
                                 markup=keyboard_markup())
            except TelegramError as e:
                log.debug(tr("Nachricht nicht gesendet: %s"), e)

    def _take_over(self, pressed: dict, text: str, buttons: Buttons | None = None,
                   request: bool = False, topic: str = "") -> None:
        """Gedrückte Nachricht umwidmen: neuer Inhalt, alte Themenzuordnung lösen, ggf. neue setzen."""
        with state.locked(self.cfg.state_dir) as st:
            old = st.messages.get(topic) if topic else None
            messages.release(st, pressed["message_id"])
            if topic:
                messages.claim(st, topic, pressed)
                if old and old["message_id"] != pressed["message_id"]:
                    messages.retire(self.tg, old)
            if request:
                st.check_request = {**pressed, "at": time.time()}
        messages.edit(self.tg, pressed, text, buttons)

    def _deliver(self, msg: dict, out: Out | None) -> None:
        if out is None:
            return
        chat_id = (msg.get("chat") or {}).get("id")
        try:
            with state.locked(self.cfg.state_dir) as st:
                if out.topic:
                    sent = messages.replace(self.tg, st, out.topic, out.text, out.buttons, chat_id=chat_id,
                                            markup=out.markup)
                else:
                    sent = self.tg.send(out.text, chat_id=chat_id, buttons=out.buttons, markup=out.markup)
                if out.ttl:
                    messages.expire(st, sent, out.ttl)
                if out.start_check:
                    st.check_request = messages.ref(sent)
        except TelegramError as e:
            log.debug(tr("Antwort nicht gesendet: %s"), e)
            return
        if out.start_check:
            self.start_check()

    def _delete_command(self, msg: dict) -> None:
        """Den Befehl nach der Antwort löschen; ohne Admin-Recht bleibt er stehen."""
        try:
            self.tg.delete(msg["chat"]["id"], msg["message_id"])
        except KeyError:
            pass
        except TelegramError as e:
            # Bei Befehlen an alle hat ein anderer Bot ihn vielleicht schon gelöscht
            if "not found" not in str(e) and not self.delete_warned:
                log.warning(tr("Befehle werden nicht gelöscht (Bot braucht das Admin-Recht "
                            "„Nachrichten löschen“): %s"), e)
                self.delete_warned = True

    def _send(self, text: str, silent: bool = False) -> None:
        try:
            self.tg.send(text, silent=silent)
        except TelegramError as e:
            log.debug(tr("Nachricht nicht gesendet: %s"), e)

    def _typing(self, chat_id: int) -> None:
        try:
            self.tg.typing(chat_id)
        except TelegramError as e:
            log.warning(tr("Tippanzeige nicht gesendet: %s"), e)

    def _answer(self, cq: dict, text: str = "", alert: bool = False) -> None:
        try:
            self.tg.answer_callback(cq["id"], text, alert=alert)
        except (TelegramError, KeyError) as e:
            log.warning(tr("Schaltfläche nicht bestätigt: %s"), e)

    def _report_update(self, st: state.State, now: float | None = None) -> None:
        result = ota.startup_report(self.cfg, st, now)
        if result:
            text, request = result
            messages.edit_or_send(self.tg, request, text)

    @staticmethod
    def _rpmdb_mtime() -> float:
        for path in RPMDB_PATHS:
            try:
                return path.stat().st_mtime
            except FileNotFoundError:
                continue
        return 0.0
