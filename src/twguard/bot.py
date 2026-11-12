# SPDX-License-Identifier: GPL-3.0-or-later
"""Telegram-Bot (Dienst): beantwortet Befehle, schickt Erinnerungen und hält den Chat aufgeräumt.

Grundsatz: eine Zustandsnachricht je Server (`panel`), die einzige mit Schaltflächen, dazu kurze
Ereignisse ohne Schaltflächen. Der Bot ist der einzige Sender: Prüflauf und Update schreiben nur in
den Zustand, `Bot.pump` stellt Ereignisse zu und führt die Zustandsnachricht nach. Befehle und
kurzlebige Antworten werden wieder gelöscht.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from html import escape
from pathlib import Path

from . import COMMIT, busy, details, messages, ota, panel, restarts, state, zypper
from .config import MODES, Config
from .i18n import EN, LANGUAGES, current, set_language, tr
from .notify import adopt_migration, clip, due_issues, fmt_age, lingering_line, issue_identity, minutes_of_day, mode_value, note_reminded, title
from .telegram import Buttons, Telegram, TelegramError

log = logging.getLogger(__name__)

CHECK_UNIT = busy.CHECK_UNIT
# Die rpm-Datenbank je nach Format: sqlite (ältere Installationen) oder ndb (Tumbleweed-Vorgabe, Packages.db)
RPMDB_PATHS = tuple(Path(d, f) for f in ("rpmdb.sqlite", "Packages.db") for d in ("/usr/lib/sysimage/rpm", "/var/lib/rpm"))
# Ältere Befehle (z. B. nach Ausfall oder Neustart) werden nicht nachgeholt
MAX_COMMAND_AGE = 600
TICK_SECONDS = 60
REBOOT_CHECK_SECONDS = 1800
# Ereignisse eines laufenden Vorgangs (Prüflauf, Dienst-Neustart) warten auf dessen Ende, höchstens so lange,
# damit sie zusammen in einer Nachricht kommen
EVENT_HOLD = 300
# So lange zeigt die Zustandsnachricht Details, danach wieder die Übersicht
VIEW_MAX = 900
# Ein angestoßener Prüflauf, der sich so lange nicht gemeldet hat, ist nicht gestartet
QUEUED_MAX = 180
# So lange gilt ein mit /update angebotener Stand
OFFER_MAX = 3600
# So oft wird eine ersetzte Nachricht noch zu entfernen versucht
STALE_TRIES = 3
# Themen bis 0.9: Sie gehen beim ersten Start mit der Zustandsnachricht
LEGACY_TOPICS = ("status", "briefing", "reboot", "observed", "update")
# Schaltflächen, die auch auf einer älteren Nachricht gelten; alle anderen nur auf der Zustandsnachricht
ANYWHERE = ("status", "file", "dismiss")
# So lange wird höchstens auf das Ende eingereihter Dienst-Neustarts gewartet
RESTART_TIMEOUT = 900
# Ein gerade gestarteter Dienst wird so viele Mindestlaufzeiten lang abgewartet (restarts.young)
SETTLE_ROUNDS = 3
# Nach einem Fehler bei getUpdates: erst kurz, dann länger warten
RETRY_SECONDS = (2, 5, 15, 30)
# Nach einem gescheiterten Abruf erst lesen, dann senden; so lange höchstens, falls nur das Abrufen gestört ist
UNREAD_MAX = 300
# Briefing wird nachgeholt, wenn der Bot zur Uhrzeit nicht lief, aber nur in diesem Zeitfenster
BRIEFING_WINDOW_MINUTES = 6 * 60
# Lebensdauer kurzlebiger Antworten in Sekunden
SHORT = 120
LONG = 300

HELP = """Befehle. In der Gruppe als Antwort auf eine Nachricht dieses Bots oder mit @botname:
/status – Zustandsnachricht nach unten holen (ohne @botname antworten alle Server)
/details – Paketliste des letzten Probelaufs in der Zustandsnachricht
/approve – gemeldetes Update freigeben und einspielen
/check – sofort prüfen
/later 1d – Erinnerungen aufschieben (z. B. 12h, 2d)
/reboot – Neustart in 1 Minute
/cancel – geplanten Neustart abbrechen
/update – neuen Stand von twguard holen (ohne @botname: alle Server)
/version – Version und Modus
/language en – Sprache bis zum nächsten Neustart des Bots ändern (ohne @botname: alle Server)
/mode auto – Modus bis zum nächsten Neustart des Bots ändern (auto oder observe; nur gezielt)
/help – diese Hilfe (ohne @botname antwortet nur ein Server, gezielt jeder)
/keyboard – feste Tastatur einblenden (/keyboard off: ausblenden)

Die Tastatur „📊 Status“, „🔄 Alle prüfen“, „⬆️ Update“, „🏷️ Version“, „❓ Hilfe“ gilt für alle Server.
Die Schaltflächen stehen in der Zustandsnachricht, der jeweils neuesten Nachricht mit Schaltflächen."""

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
    ("mode", "Modus bis zum Neustart ändern"),
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
        # /mode ändert cfg.mode nur im laufenden Bot; das hier steht in der Konfiguration
        self.configured_mode = cfg.mode
        self.tg = tg or Telegram(cfg.token, cfg.chat_id, cfg.max_errors_per_hour)
        self.me = me
        self.offset = 0
        self.fetch_failed_since = 0.0
        self.failures = 0
        # Die Zustandsnachricht, die beim Abholen des laufenden Abrufs registriert war (siehe _on_panel)
        self.fetched_panel: dict = {}
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
            "mode": (self.cmd_mode, False),
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
        while True:
            self.handle_batch(self.fetch())
            self.watch_restart()
            if time.time() - self.last_tick >= TICK_SECONDS:
                self.tick()
            self.pump()

    def fetch(self) -> list[dict]:
        """Kurzes Long-Polling (10 s): Antworten, die unterwegs verloren gehen, kosten so wenig Zeit,
        und was Prüflauf oder Update in den Zustand schreiben, erscheint nach höchstens 10 s."""
        try:
            updates = self.tg.get_updates(self.offset)
        except TelegramError as e:
            log.debug("getUpdates: %s", e)
            self.fetch_failed_since = self.fetch_failed_since or time.time()
            time.sleep(RETRY_SECONDS[min(self.failures, len(RETRY_SECONDS) - 1)])
            self.failures += 1
            return []
        self.failures, self.fetch_failed_since = 0, 0.0
        return updates

    def handle_batch(self, updates: list[dict]) -> None:
        """Alle Updates eines Abrufs. Ihre Knopfdrücke geschahen vor dem Abruf, also auf der Nachricht, die
        da Zustandsnachricht war; das gilt auch, wenn ein früherer Druck desselben Abrufs sie schon ersetzt hat."""
        if not updates:
            return
        with state.locked(self.cfg.state_dir) as st:
            self.fetched_panel = dict(st.messages.get(panel.TOPIC) or {})
        try:
            for update in updates:
                self.process(update)
        finally:
            self.fetched_panel = {}

    def _unread(self) -> bool:
        """Der letzte Abruf scheiterte: Knopfdrücke aus der Störung können noch ausstehen.

        Sie gelten der Zustandsnachricht, auf der sie gemacht wurden; eine vorher neu gesendete machte sie
        „veraltet“. Ist nur das Abrufen gestört (z. B. 409, ein zweiter Bot mit demselben Token), sendet
        der Bot nach UNREAD_MAX trotzdem wieder.
        """
        return bool(self.fetch_failed_since) and time.time() - self.fetch_failed_since < UNREAD_MAX

    def startup(self) -> None:
        self._set_menu()
        with state.locked(self.cfg.state_dir) as st:
            # Eine mit /language gewählte Sprache und ein mit /mode gewählter Modus gelten nur bis zum Neustart
            st.language_override = ""
            st.mode_override = ""
            self.offset = st.tg_offset
            st.first_seen = st.first_seen or time.time()
            current = state.boot_id()
            # Bis 0.9.4 gab es keine Boot-ID im Zustand; dann zählt nur ein selbst ausgelöster Neustart
            booted = (st.boot_id != current if st.boot_id
                      else bool(st.reboot_requested_at) and boot_time() > st.reboot_requested_at)
            st.boot_id = current
            if booted:
                self.after_boot(st)
            st.bot_commit = COMMIT
            self.park_legacy_waiting(st)
            ota.startup_report(self.cfg, st)
        self.observe_system()
        self.check_reboot()
        self.pump()
        self.migrate_legacy()

    def after_boot(self, st: state.State) -> None:
        """Einmal nach jedem Boot (nicht bei einem bloßen Neustart des Bots).

        Neustart und Dienste von vorher sind damit erledigt; ob wieder etwas ansteht, sagt erst zypper, und
        das kann nach einem Kernel-Wechsel minutenlang gesperrt sein (purge-kernels). Bis dahin zeigt die
        Zustandsnachricht „noch nicht abgefragt“ statt Entwarnung. Rückmeldungen gehen als Ereignis über
        die Warteschlange, damit ein Telegram-Fehler beim Start nichts verliert.
        """
        for kind in ("reboot", "services"):
            st.resolve(kind)
        st.restart_services = []
        st.lingering = {}
        st.unverified = ["reboot", "services"]
        st.reboot_planning = {}
        busy.note_shutdown(st, busy.Shutdown("none"))
        if st.restart_request:
            request, st.restart_request = st.restart_request, {}
            panel.event(st, panel.line(self.cfg.hostname, "⚠️", tr(
                "Der Neustart von {services} wurde durch den Neustart des Servers unterbrochen; "
                "das Ergebnis ist unbekannt.", services=escape(", ".join(request.get("services", []))))), loud=True)
        if st.reboot_requested_at:
            st.reboot_requested_at = 0.0
            panel.event(st, tr("🟢 <b>{host}</b> ist wieder da (Kernel {kernel}).", host=self.host,
                               kernel=escape(os.uname().release)))

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
        freed = self.observe_system()
        with state.locked(self.cfg.state_dir) as st:
            rpmdb_changed = bool(st.rpmdb_mtime) and mtime != st.rpmdb_mtime
            st.rpmdb_mtime = mtime
            pending = state.pending_plan(st) is not None or "apply-failed" in st.issues
        if rpmdb_changed:
            # Jemand hat Pakete geändert, z. B. per SSH selbst `zypper dup` ausgeführt
            self.check_reboot()
            if pending:
                self.start_check()
        elif freed or now - self.last_reboot_check >= REBOOT_CHECK_SECONDS:
            # Nach purge-kernels antwortet zypper wieder: Neustart und Dienste nicht erst in 30 min abgleichen
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
            ota.startup_report(self.cfg, st, now)
            due = messages.expired(st, now)
        for message in due:
            messages.delete(self.tg, message)
        self.maybe_briefing(now)
        self.retry_stale()
        self.migrate_legacy()

    def observe_system(self) -> bool:
        """logind und den Besitzer der zypp-Sperre für die Anzeige merken.

        Rückgabe: Die Paketverwaltung ist seit dem letzten Aufruf frei geworden.
        """
        shutdown = busy.shutdown()
        holder = zypper.lock_holder()
        with state.locked(self.cfg.state_dir) as st:
            busy.note_shutdown(st, shutdown)
            freed = bool(st.zypp_holder) and not holder
            st.zypp_holder = holder
        return freed

    def maybe_briefing(self, now: float) -> None:
        """Einmal am Tag die Zustandsnachricht neu senden, mit Ton, wenn etwas offen ist (Lebenszeichen)."""
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
        self.pump(briefing=today, now=now)

    # --- Zustandsnachricht und Ereignisse ---------------------------------

    def pump(self, resend: bool = False, briefing: str = "", view: str | None = None,
             now: float | None = None) -> bool:
        """Ereignisse zustellen und die Zustandsnachricht nachführen; Rückgabe: sie ist auf dem Stand.

        Der einzige Weg, auf dem Zustand nach Telegram geht. Bearbeitet wird lautlos; neu gesendet wird
        auf Anfrage (`resend`, z. B. /status), zum Briefing (`briefing` ist der Tag), nach Ereignissen
        (still, damit sie wieder darunter steht) und mit Ton, wenn eine Erinnerung fällig ist. Gesendet
        wird außerhalb der Sperre; was gezeigt ist, steht als Digest im Zustand. Ein neuerer Zustand
        ergibt einen anderen Digest und wird beim nächsten Aufruf nachgezogen.
        """
        now = now or time.time()
        if self._paused() or self._unread():
            return False
        if self.send_events(now):
            resend = True
        if self._paused():
            return False  # Telegram verlangt eine Pause; der Rest wartet bis zum nächsten Aufruf
        explicit = view is not None
        with state.locked(self.cfg.state_dir) as st:
            self._prune(st, now)
            due = due_issues(st, now, self.cfg.escalation)
            # Was gesendet wird; quittiert wird nur, was danach noch genauso besteht
            reminded = [(issue.kind, issue_identity(st, issue)) for issue in due]
            old = dict(st.messages.get(panel.TOPIC) or {})
            meta = dict(st.panel)
            fresh = resend or bool(briefing) or bool(due) or not old
            if due:
                view = ""  # Eine Erinnerung zeigt die Übersicht
            elif view is None:
                view = "" if fresh or now - meta.get("view_at", 0) > VIEW_MAX else meta.get("view", "")
            shown = self._render(st, now, due, bool(briefing), view)
            if view and not explicit and shown.overview != meta.get("overview"):
                # Etwas hat sich geändert: zurück zur Übersicht, statt es hinter der Paketliste zu verstecken
                view, shown = "", self._render(st, now, due, bool(briefing), "")
            loud = bool(due) or bool(briefing) and bool(st.issues or not self.cfg.briefing.silent)
            plan = state.pending_plan(st)
            fingerprint = plan.fingerprint if plan and plan.approvable else ""
        if not fresh and shown.digest == meta.get("digest"):
            return True
        target = old if not fresh and messages.edit(self.tg, old, shown.text, shown.buttons) else None
        if target is None and self._paused():
            return False  # Bearbeiten scheiterte an der Wartezeit: nicht stattdessen neu senden
        if target is None:
            try:
                target = messages.ref(self.tg.send(shown.text, silent=not loud, buttons=shown.buttons))
            except TelegramError as e:
                self._pause(e)
                log.debug(tr("Zustandsnachricht nicht gesendet: %s"), e)
                return False
        with state.locked(self.cfg.state_dir) as st:
            st.messages[panel.TOPIC] = target
            st.panel = {"digest": shown.digest, "overview": shown.overview, "view": view,
                        "view_at": (now if explicit else meta.get("view_at", now)) if view else 0}
            if not view and fingerprint:
                # /approve gilt nur für den Plan, den eine zugestellte Nachricht gezeigt hat
                st.manual_shown = fingerprint
            for kind, identity in reminded:
                # Erinnert gilt erst, wenn die Nachricht mit Ton angekommen ist, und nur für den Stand,
                # den sie zeigte; ein inzwischen geänderter Punkt bleibt fällig
                current = st.issues.get(kind)
                if current is not None and issue_identity(st, current) == identity:
                    note_reminded(current, now)
            if briefing:
                st.last_briefing_day = briefing
        if old and not messages.same(old, target) and not messages.retire(self.tg, old):
            with state.locked(self.cfg.state_dir) as st:
                st.stale_messages.append(old)
        return True

    def _render(self, st: state.State, now: float, due: list, briefing: bool, view: str) -> panel.Panel:
        return panel.render(self.cfg, st, now, self.configured_mode, os.uname().release, boot_time(), due,
                            briefing, view)

    def _prune(self, st: state.State, now: float) -> None:
        """Was nicht mehr läuft, nicht mehr als laufend zeigen."""
        check = st.activity.get("check")
        if check and (not state.alive(check) if check.get("pid") else now - check.get("at", 0) > QUEUED_MAX):
            del st.activity["check"]
        if st.update_offer and now - st.update_offer.get("at", 0) > OFFER_MAX:
            st.update_offer = {}

    def send_events(self, now: float) -> bool:
        """Vorgemerkte Ereignisse zustellen; Rückgabe: es wurde etwas gesendet.

        Solange ein eigener Vorgang läuft, warten seine Ereignisse (höchstens EVENT_HOLD) und kommen
        dann zusammen; eines mit Ton (etwas ging schief) nimmt alle sofort mit. Eine Nachricht nimmt so
        viele ganze Ereignisse auf, wie hineinpassen; aus der Warteschlange geht erst, was angekommen
        ist. Der Rest folgt in weiteren Nachrichten oder beim nächsten Aufruf.
        """
        with state.locked(self.cfg.state_dir) as st:
            batch = list(st.outbox)
            running = bool(st.restart_request) or state.alive(st.applying) or "check" in st.activity
        # Was schiefging, kommt sofort
        held = running and not any(e.get("loud") for e in batch)
        if not batch or held and now - min(e.get("at", now) for e in batch) < EVENT_HOLD:
            return False
        sent_any = False
        for chunk in self._chunks(batch):
            try:
                self.tg.send("\n".join(e["text"] for e in chunk), silent=not any(e.get("loud") for e in chunk))
            except TelegramError as e:
                self._pause(e)
                log.debug(tr("Nachricht nicht gesendet: %s"), e)
                break
            sent_any = True
            sent = {self._event_key(e) for e in chunk}
            with state.locked(self.cfg.state_dir) as st:
                st.outbox = [e for e in st.outbox if self._event_key(e) not in sent]
            for e in chunk:
                if e.get("message_id"):
                    # Bis 0.9 gehörte zum Ergebnis eine Wartemeldung
                    messages.retire(self.tg, e)
        return sent_any

    @staticmethod
    def _chunks(batch: list[dict]) -> list[list[dict]]:
        """Ganze Ereignisse in Nachrichten bis TEXT_LIMIT; keins wird geteilt (`panel.event` begrenzt jedes)."""
        chunks: list[list[dict]] = []
        size = 0
        for e in batch:
            length = len(e["text"]) + 1
            if chunks and size + length <= panel.TEXT_LIMIT:
                chunks[-1].append(e)
                size += length
            else:
                chunks.append([e])
                size = length
        return chunks

    @staticmethod
    def _event_key(e: dict) -> str:
        return e.get("id") or f"{e.get('at')}:{e.get('text')}"

    def _paused(self) -> bool:
        """Telegram verlangt nach „Too Many Requests“ eine Wartezeit; der Client hält sie fest."""
        return time.time() < self.tg.blocked_until

    def _pause(self, e: TelegramError) -> None:
        if e.retry_after:
            self.tg.blocked_until = max(self.tg.blocked_until, time.time() + e.retry_after)
            log.warning(tr("Telegram: zu viele Nachrichten, warte %d s"), e.retry_after)

    def retry_stale(self) -> None:
        """Ersetzte Nachrichten, die sich nicht entfernen ließen, erneut entfernen (höchstens STALE_TRIES).

        Eine Wartezeit nach 429 ist kein Fehlschlag: Ab da bleiben die übrigen ohne verbrauchten Versuch stehen.
        """
        if self._paused():
            return
        with state.locked(self.cfg.state_dir) as st:
            pending, st.stale_messages = st.stale_messages, []
        kept = []
        for index, message in enumerate(pending):
            if messages.retire(self.tg, message):
                continue
            if self._paused():
                # Diese und alle noch nicht versuchten mit unverändertem Zähler zurückstellen
                kept += pending[index:]
                break
            if message.get("tries", 0) + 1 < STALE_TRIES:
                kept.append({**message, "tries": message.get("tries", 0) + 1})
        if kept:
            with state.locked(self.cfg.state_dir) as st:
                st.stale_messages += kept

    def migrate_legacy(self) -> None:
        """Nachrichten der Themen bis 0.9 abräumen, sobald die Zustandsnachricht steht.

        Wiederholbar: Was sich nicht entfernen lässt, versucht `retry_stale` erneut; laufende Aufträge und
        die Warteschlange bleiben, nur ihre Wartemeldungen gehen.
        """
        with state.locked(self.cfg.state_dir) as st:
            if panel.TOPIC not in st.messages:
                return
            legacy = [t for t in st.messages if t in LEGACY_TOPICS or t.startswith(("issue:", "wait:"))]
            old = [st.messages.pop(t) for t in legacy]
            st.stale_messages += old
        if old:
            self.retry_stale()

    @staticmethod
    def park_legacy_waiting(st: state.State) -> None:
        """Wartemeldungen laufender Aufträge aus 0.9 als Themen `wait:*` merken, bevor jemand den Auftrag beendet.

        Das Ergebnis eines Updates trägt `ota.startup_report` gleich beim Start ein und leert dabei den Auftrag;
        ohne diesen Schritt bliebe dessen „Das Ergebnis erscheint hier“ stehen. Abgeräumt wird wie die übrigen
        alten Themen erst, wenn die Zustandsnachricht steht.
        """
        for name, request in (("check", st.check_request), ("restart", st.restart_request),
                              ("update", st.update_request)):
            if request.get("message_id"):
                st.messages[f"wait:{name}"] = {"chat_id": request.get("chat_id"), "message_id": request["message_id"],
                                               "at": request.get("at", 0)}
            request.pop("message_id", None)
            request.pop("chat_id", None)

    # --- Abfragen ---------------------------------------------------------

    def check_reboot(self) -> None:
        self.last_reboot_check = time.time()
        self.check_services()
        self.update_reboot()

    def update_reboot(self) -> None:
        """Offenen Punkt „Neustart“ nach `zypper needs-rebooting` öffnen oder schließen."""
        needed = zypper.needs_rebooting()
        if needed is None:
            return
        with state.locked(self.cfg.state_dir) as st:
            if "reboot" in st.unverified:
                st.unverified.remove("reboot")
            if needed and "reboot" not in st.issues:
                st.open_issue("reboot", tr("zypper needs-rebooting meldet einen ausstehenden Neustart."))
            elif not needed:
                st.resolve("reboot")

    def check_services(self) -> None:
        # Während twguard selbst einspielt, ändert sich die Liste noch; der Prüflauf gleicht sie am Ende ab
        if busy.check_running():
            return
        found = restarts.survey()
        if found is None:
            return
        with state.locked(self.cfg.state_dir) as st:
            if "services" in st.unverified:
                st.unverified.remove("services")
            restarts.note(st, found, self.cfg.services.exclude)

    def watch_restart(self) -> None:
        """Ergebnis eingereihter Dienst-Neustarts ermitteln, sobald systemd sie abgearbeitet hat."""
        with state.locked(self.cfg.state_dir) as st:
            request = dict(st.restart_request)
        if not request:
            return
        names, errors = request["services"], request.get("errors", {})
        waiting = [name for name in names if name not in errors]
        timed_out = time.time() - request["at"] >= RESTART_TIMEOUT
        jobs = restarts.pending(waiting)
        if jobs:
            if timed_out and not request.get("overdue"):
                self._report_overdue(jobs)
            return
        if jobs is None and not timed_out:
            return
        current = restarts.states(waiting) if waiting else {}
        active = [name for name, s in current.items() if s == "active"]
        fresh = restarts.young(active)
        if fresh is None and not timed_out:
            return  # Startzeit unbekannt: weiter nachfragen, an der Grenze melden, was ist
        if fresh and self._settling():
            return  # Gerade erst gestartet: Scheitert das Programm gleich danach, wäre „neu gestartet“ falsch
        # Am Ende des Wartens: Wer die Mindestlaufzeit nicht nachweislich erreicht hat, gilt nicht als neu gestartet
        unconfirmed = active if fresh is None else fresh
        found = restarts.survey()
        # Unbekannt ist kein Ergebnis: weiter nachfragen, erst an der Grenze ehrlich melden
        if (found is None or "unknown" in current.values()) and not timed_out:
            return
        # Ohne Auskunft über die Aufträge kann auch ein aktiver Dienst noch auf seinen Neustart warten
        unsettled = waiting if jobs is None else [name for name, s in current.items() if s == "unknown"]
        unconfirmed = [name for name in unconfirmed if name not in unsettled]
        current = {name: s for name, s in current.items() if name not in unsettled and name not in unconfirmed}
        text, loud = self.restart_result(current, found, errors, unsettled, unconfirmed)
        with state.locked(self.cfg.state_dir) as st:
            st.restart_request = {}
            if found is not None:
                restarts.note(st, found, self.cfg.services.exclude)
            panel.event(st, text, loud=loud)
        self.pump()

    def _settling(self) -> bool:
        """Junge Dienste abwarten, auch nach RESTART_TIMEOUT (ein Auftrag kann spät enden).

        Höchstens SETTLE_ROUNDS × SETTLE_SECONDS ab dem Ende der Aufträge, im Auftrag gespeichert (übersteht
        den Neustart des Bots): Ein Dienst in einer Neustartschleife ist bei jeder Abfrage jung.
        """
        with state.locked(self.cfg.state_dir) as st:
            if not st.restart_request:
                return False
            since = st.restart_request.setdefault("settling_since", time.time())
        return time.time() - since < SETTLE_ROUNDS * restarts.SETTLE_SECONDS

    def _report_overdue(self, overdue: list[str]) -> None:
        """Einmal warnen und weiter beobachten: Der Auftrag besteht noch und kann auch noch scheitern."""
        text = panel.line(self.cfg.hostname, "⏳", tr(
            "Neustart nach {minutes} Minuten nicht abgeschlossen, systemd arbeitet noch daran: {services}. "
            "Das Ergebnis folgt.", minutes=RESTART_TIMEOUT // 60, services=escape(", ".join(overdue))))
        with state.locked(self.cfg.state_dir) as st:
            if st.restart_request:
                st.restart_request["overdue"] = True
            panel.event(st, text, loud=True)
        self.pump()

    def restart_result(self, current: dict[str, str], found: restarts.Survey | None, errors: dict[str, str],
                       unsettled: list[str] = (), unconfirmed: list[str] = ()) -> tuple[str, bool]:
        """Text des Ergebnisses und ob es mit Ton kommen muss."""
        running = [name for name, s in current.items() if s == "active"]
        stopped = [f"{name} ({s})" for name, s in current.items() if s != "active"]
        rejected = [f"{name} ({clip(e, 300)})" for name, e in errors.items()]

        def line(icon: str, text: str) -> str:
            return panel.line(self.cfg.hostname, icon, text)

        def join(names: list[str]) -> str:
            return escape(", ".join(names))

        lines = []
        if found is None:
            if running:
                lines.append(line("⚠️", tr("läuft, aber zypper ps gab keine Auskunft über alte Dateien: {services}",
                                           services=join(running))))
        else:
            # Übrig gebliebene Prozesse beendet kein Neustart; der Dienst selbst ist erneuert
            old = [name for name in running if name in found.services and name not in found.lingering]
            renewed = [name for name in running if name not in old and name not in found.lingering]
            if renewed:
                lines.append(line("✅", tr("neu gestartet: {services}", services=join(renewed))))
            lines += [lingering_line(name, [asdict(proc) for proc in found.lingering[name]])
                      for name in running if name in found.lingering]
            if old:
                lines.append(line("⚠️", tr("nutzt weiter alte Dateien: {services}", services=join(old))))
        if stopped:
            lines.append(line("🔴", tr("nach dem Neustart nicht aktiv: {services}", services=join(stopped))))
        if rejected:
            lines.append(line("🔴", tr("Neustart abgewiesen: {services}", services=join(rejected))))
        if unsettled:
            lines.append(line("⚠️", tr("Ergebnis des Neustarts unbekannt, systemd gab keine Auskunft: {services}",
                                       services=join(list(unsettled)))))
        if unconfirmed:
            lines.append(line("⚠️", tr("aktiv, aber nicht nachweislich {seconds} s am Stück (Neustartschleife oder "
                                       "Startzeit unbekannt), Ergebnis nicht bestätigt: {services}",
                                       seconds=restarts.SETTLE_SECONDS, services=join(list(unconfirmed)))))
        return "\n".join(lines), bool(stopped or rejected or unsettled or unconfirmed) or found is None

    def start_check(self, requested: bool = False) -> None:
        """Prüflauf anstoßen; mit `requested` kommt sein Ergebnis als Ereignis, auch wenn nichts zu tun war."""
        # Der Takt fragt nur alle 30 min; ein ausstehender Neustart soll schon im Ergebnis stehen
        self.update_reboot()
        with state.locked(self.cfg.state_dir) as st:
            # Ein schon laufender Prüflauf bleibt, was er ist (systemctl start startet ihn nicht ein zweites Mal)
            if not state.alive(st.activity.get("check", {})):
                st.activity["check"] = {"phase": "queued", "detail": "", "at": time.time()}
            if requested:
                st.check_request = {"at": time.time()}
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

    def cmd_status(self, args: list[str], addressed: bool = False) -> None:
        self.show_current()

    def show_current(self, view: str | None = None) -> None:
        """Zustandsnachricht nach unten holen; vorher abfragen, was der Takt nur alle 30 min fragt.

        Die Übersicht gleicht auch die Dienste ab (z. B. an der Konsole neu gestartet); die Paketliste
        braucht das nicht und bleibt bei der kurzen Abfrage des Neustarts.
        """
        self.observe_system()
        if view == "details":
            self.update_reboot()
        else:
            self.check_reboot()
        self.pump(resend=True, view=view)

    def cmd_details(self, args: list[str], addressed: bool = False) -> None:
        self.show_current(view="details")

    def cmd_approve(self, args: list[str], addressed: bool = False) -> Out | None:
        error = self.approve()
        if error:
            with state.locked(self.cfg.state_dir) as st:
                plan = state.pending_plan(st)
                unseen = plan is not None and plan.approvable and plan.fingerprint != st.manual_shown
            if unseen:
                # Einen noch nicht gezeigten Plan sofort zeigen, auch in der Ruhezeit: es wird gerade gefragt
                self.pump(resend=True)
            return Out(error, ttl=SHORT)
        self.start_check(requested=True)
        self.pump(resend=True)
        return None

    def approve(self, fingerprint: str = "") -> str:
        """Freigabe erteilen; Rückgabe: Fehlertext oder leer. Mit Fingerabdruck nur für genau diesen Stand."""
        refused = self.refused("approve")
        if refused:
            return refused
        with state.locked(self.cfg.state_dir) as st:
            # Freigabe und „Diesmal einspielen“ im Beobachtungsmodus sind derselbe Vorgang
            plan = state.pending_plan(st)
            if plan is None:
                return tr("Es liegt kein Update zur Freigabe vor.")
            if not plan.approvable:
                return tr("Das lässt sich nicht per Telegram freigeben, bitte per SSH lösen.")
            if fingerprint and fingerprint != plan.fingerprint:
                return tr("Diese Meldung gilt einem älteren Stand. /status zeigt den aktuellen.")
            if not fingerprint and plan.fingerprint != st.manual_shown:
                # /approve nennt keinen Stand; er darf nur gelten, was zuletzt zu sehen war
                return tr("Der Plan hat sich geändert, nichts freigegeben. Bitte die aktuelle Meldung prüfen.")
            st.approved_fingerprint = plan.fingerprint
        return ""

    def cmd_check(self, args: list[str], addressed: bool = False) -> Out | None:
        verdict, refused = self.decide("start-check")
        if verdict == "deny":
            return Out(refused, ttl=SHORT)
        if verdict == "noop":
            return None  # Prüft schon; das steht in der Zustandsnachricht, ein Hinweis wäre nur Rauschen
        self.check_services()  # vor dem Prüflauf: währenddessen setzt der Abgleich aus
        self.start_check(requested=True)
        self.pump(resend=True)
        return None

    def refused(self, action: str) -> str:
        """Ablehnung nach `busy` mit Servername, leer heißt frei. Nur Vorabprüfung, reserviert nichts."""
        return self.decide(action)[1]

    def decide(self, action: str) -> tuple[str, str]:
        """`busy.decide` mit Servername in der Ablehnung: ("allow"|"noop"|"deny", Text)."""
        with state.locked(self.cfg.state_dir) as st:
            verdict, text = busy.decide(action, st)
        return verdict, f"⏸️ <b>{self.host}</b>: {escape(text)}" if text else ""

    def cmd_later(self, args: list[str], addressed: bool = False) -> Out:
        hours = parse_duration(args[0]) if args else 24
        if hours is None:
            return Out(tr("Format: /later 12h oder /later 2d"), ttl=SHORT)
        text = self.snooze(hours)
        self.pump()
        return Out(text, ttl=SHORT)

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
        """Neustart in einer Minute planen; Rückgabe: Fehlertext oder leer.

        Prüfen und Reservieren unter derselben Sperre wie das Einspielen: Ein Prüflauf, der dazwischen
        reservieren will, sieht `reboot_planning` und spielt nichts ein; danach sieht er den Plan in logind.
        """
        with state.locked(self.cfg.state_dir) as st:
            text = busy.refusal("reboot", st)
            if not text:
                st.reboot_planning = mine = state.holder()
        if text:
            return f"⏸️ <b>{self.host}</b>: {escape(text)}"
        try:
            p = subprocess.run(["shutdown", "-r", "+1", "twguard: Neustart per Telegram"],
                               capture_output=True, text=True)
        except OSError as e:
            p = subprocess.CompletedProcess([], 1, "", str(e))
        shutdown = busy.shutdown()
        with state.locked(self.cfg.state_dir) as st:
            if st.reboot_planning == mine:
                st.reboot_planning = {}
            busy.note_shutdown(st, shutdown)
            if not p.returncode:
                st.reboot_requested_at = time.time()
                panel.event(st, panel.line(self.cfg.hostname, "🔁", tr("Neustart per Telegram ausgelöst ({planned}).",
                                                                       planned=escape(busy.planned_text(shutdown)))))
        if p.returncode:
            return tr("Neustart fehlgeschlagen: {error}", error=escape(p.stderr.strip()))
        return ""

    def cmd_reboot(self, args: list[str], addressed: bool = False) -> Out | None:
        error = self.reboot()
        if error:
            return Out(error, ttl=SHORT)
        self.pump(resend=True)
        return None

    def cancel(self) -> str:
        """Geplanten Neustart abbrechen; Rückgabe: Fehlertext oder leer."""
        p = subprocess.run(["shutdown", "-c"], capture_output=True, text=True)
        shutdown = busy.shutdown()
        # Nur ein tatsächlich aufgehobener Plan gilt als abgebrochen
        failed = bool(p.returncode) or shutdown.state == "planned"
        with state.locked(self.cfg.state_dir) as st:
            busy.note_shutdown(st, shutdown)
            if shutdown.state != "planned":
                st.reboot_requested_at = 0.0
            if not failed:
                panel.event(st, panel.line(self.cfg.hostname, "✖️", tr("Neustart abgebrochen.")))
        if failed:
            return tr("Abbrechen fehlgeschlagen: {error}", error=escape(p.stderr.strip() or busy.planned_text(shutdown)))
        return ""

    def cmd_cancel(self, args: list[str], addressed: bool = False) -> Out | None:
        error = self.cancel()
        self.pump(resend=not error)
        return Out(error, ttl=SHORT) if error else None

    def cmd_update(self, args: list[str], addressed: bool = False) -> Out | None:
        """Neuen Stand holen und in der Zustandsnachricht zur Bestätigung anbieten."""
        refused = self.refused("update")
        if refused:
            return Out(refused, ttl=SHORT)
        try:
            pending = ota.check_remote(self.cfg.update)
        except ota.UpdateError as e:
            return Out(f"<b>{self.host}</b>: {escape(str(e))}", ttl=LONG)
        if not pending.commits:
            return Out(tr("<b>{host}</b>: twguard ist aktuell ({commit}).", host=self.host, commit=escape(ota.running())), ttl=SHORT)
        with state.locked(self.cfg.state_dir) as st:
            st.update_offer = {"target": pending.target, "label": ota.label(pending.target, pending.version),
                               "running": ota.running(), "commits": pending.commits, "at": time.time()}
        self.pump(resend=True)
        return None

    def update_go(self, commit: str) -> str:
        """Bestätigtes Update starten; das Ergebnis meldet der neue Bot oder das Zurückrollen.

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
            refused = busy.refusal("update", st)
            if refused:
                return f"⏸️ <b>{self.host}</b>: {escape(refused)}"
            st.update_offer = {}
            st.update_request = {"commit": commit, "label": ota.label(commit, pending.version), "at": time.time(),
                                 "wait": ota.START_TIMEOUT}
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
        if self.cfg.mode != self.configured_mode:
            mode += " " + tr("(bis zum Neustart)")
        return f"🖥 <b>{self.host}</b> (@{escape(self.me['username'])}) · twguard {escape(ota.running())} · {mode}"

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
        self.pump()
        if language == configured:
            return Out(tr("🌐 <b>{host}</b>: Sprache {language}, wie in der Konfiguration.",
                          host=self.host, language=language), topic="language")
        return Out(tr("🌐 <b>{host}</b>: Sprache {language} bis zum nächsten Neustart des Bots. Die Konfiguration "
                      "enthält {configured}. Damit es dauerhaft gilt, in /etc/twguard/config.toml unter [general] "
                      "<code>language = \"{language}\"</code> eintragen.",
                      host=self.host, language=language, configured=configured), topic="language")

    def cmd_mode(self, args: list[str], addressed: bool = False) -> Out | None:
        """Modus zur Laufzeit ändern, nur gezielt; die Konfiguration bleibt unberührt, ein Neustart gilt wieder sie."""
        configured = self.configured_mode
        if not addressed:
            # Nicht versehentlich alle Server umschalten; den Hinweis gibt nur einer, wie bei /help
            if not self.cfg.help_full:
                return None
            return Out(tr("/mode gilt nur gezielt: mit @botname oder als Antwort auf eine Nachricht des Servers."),
                       ttl=SHORT)
        if not args:
            return Out(tr("⚙️ <b>{host}</b>: Modus {mode}, in der Konfiguration {configured}. "
                          "Ändern: /mode auto oder /mode observe", host=self.host, mode=mode_value(self.cfg.mode),
                          configured=mode_value(configured)), ttl=LONG)
        mode = args[0].lower()
        if mode not in MODES:
            return Out(tr("Format: /mode auto oder /mode observe"), ttl=SHORT)
        self.cfg.mode = mode
        with state.locked(self.cfg.state_dir) as st:
            st.mode_override = "" if mode == configured else mode
            # Der Prüflauf entscheidet unter derselben Sperre; was schon eingespielt wird, läuft zu Ende
            applying = state.alive(st.applying)
        note = ("\n" + tr("Ein Update wird gerade eingespielt und läuft noch zu Ende.")
                if applying and mode == "observe" else "")
        self.pump()
        if mode == configured:
            return Out(tr("⚙️ <b>{host}</b>: Modus {mode}, wie in der Konfiguration.", host=self.host,
                          mode=mode_value(mode))
                       + note, topic="mode")
        text = tr("⚙️ <b>{host}</b>: Modus {mode} bis zum nächsten Neustart des Bots (auch durch /update oder "
                  "einen Reboot). Die Konfiguration enthält {configured}. Damit es dauerhaft gilt, in "
                  "/etc/twguard/config.toml unter [general] <code>mode = \"{value}\"</code> eintragen.",
                  host=self.host, mode=mode_value(mode), value=mode, configured=mode_value(configured))
        if mode == "auto":
            text += "\n" + tr("Routine-Updates spielt der nächste Prüflauf ein, sofort: /check")
        return Out(text + note, topic="mode")

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

        Aktionen gelten nur auf der aktuellen Zustandsnachricht; eine ältere zeigt danach den aktuellen
        Stand. Der Druck wird mit einem Hinweis beantwortet, das Ergebnis zeigt die Zustandsnachricht.
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
            "restart": self.press_restart,
            "resolved": self.press_resolved,
            "dismiss": self.press_dismiss,
        }.get(action)
        if handler is None:
            self._answer(cq, tr("Unbekannte Schaltfläche."))
            return
        if action not in ANYWHERE and not self._on_panel(pressed):
            self._outdated(cq, pressed)
            return
        handler(cq, pressed, arg)

    def _on_panel(self, pressed: dict) -> bool:
        """Gedrückt auf der Zustandsnachricht, die beim Abholen des Drucks galt (oder auf der jetzigen)."""
        if self.fetched_panel and messages.same(self.fetched_panel, pressed):
            return True
        with state.locked(self.cfg.state_dir) as st:
            return messages.same(st.messages.get(panel.TOPIC, {}), pressed)

    def _outdated(self, cq: dict, pressed: dict) -> None:
        """Knopf auf einer älteren Nachricht: nichts ausführen, sie abräumen und den aktuellen Stand zeigen."""
        self._answer(cq, tr("Diese Nachricht ist veraltet, nichts ausgeführt. Der aktuelle Stand steht jetzt "
                            "ganz unten."), alert=True)
        if not messages.retire(self.tg, pressed):
            with state.locked(self.cfg.state_dir) as st:
                st.stale_messages.append(pressed)
        self.show_current()

    def press_approve(self, cq: dict, pressed: dict, fingerprint: str) -> None:
        error = self.approve(fingerprint)
        if error:
            self._answer(cq, plain(error), alert=True)
            return
        self._answer(cq, tr("Freigegeben"))
        self.start_check(requested=True)
        self.pump()

    def press_details(self, cq: dict, pressed: dict, arg: str) -> None:
        self._answer(cq)
        self.pump(view=f"details:{arg}" if arg else "details")

    def press_back(self, cq: dict, pressed: dict, arg: str) -> None:
        self._answer(cq)
        self.pump(view="")

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

    def press_later(self, cq: dict, pressed: dict, arg: str) -> None:
        hours_text, _, kind = arg.partition(":")
        self._answer(cq, self.snooze(float(hours_text or 24), [kind] if kind else None))
        self.pump()

    def press_check(self, cq: dict, pressed: dict, arg: str) -> None:
        verdict, refused = self.decide("start-check")
        if verdict == "deny":
            self._answer(cq, plain(refused), alert=True)
            return
        if verdict == "noop":
            self._answer(cq)  # Prüft schon (Knopf von kurz vorher): nichts tun, wie bei /check
            return
        self._answer(cq, tr("Prüfung läuft …"))
        # Vor dem Prüflauf, währenddessen setzt der Abgleich aus; den Neustart fragt start_check selbst
        self.check_services()
        self.start_check(requested=True)
        self.pump()

    def press_status(self, cq: dict, pressed: dict, arg: str) -> None:
        self._answer(cq)
        self.show_current()

    def press_reboot(self, cq: dict, pressed: dict, arg: str) -> None:
        error = self.reboot()
        if error:
            self._answer(cq, plain(error), alert=True)
            return
        self._answer(cq, tr("Neustart in 1 Minute"))
        self.pump()

    def press_cancel(self, cq: dict, pressed: dict, arg: str) -> None:
        error = self.cancel()
        self._answer(cq, plain(error) or tr("Neustart abgebrochen."), alert=bool(error))
        self.pump()

    def press_update(self, cq: dict, pressed: dict, commit: str) -> None:
        error = self.update_go(commit)
        if error:
            self._answer(cq, plain(error), alert=True)
            return
        self._answer(cq, tr("Update läuft …"))
        self.pump()

    def press_restart(self, cq: dict, pressed: dict, arg: str) -> None:
        """Einen Dienst oder (`*:<Kennung>`) genau die gezeigte Liste neu starten."""
        refused = self.refused("restart")
        if refused:
            self._answer(cq, plain(refused), alert=True)
            return
        with state.locked(self.cfg.state_dir) as st:
            listed = list(st.restart_services)
        found = restarts.survey()
        if found is None:
            self._answer(cq, tr("zypper ps gab keine Auskunft, nichts neu gestartet."), alert=True)
            return
        if arg.startswith("*:"):
            names = listed if arg == f"*:{restarts.digest(listed)}" and set(listed) <= set(found.services) else []
        else:
            names = [arg] if arg in listed and arg in found.services else []
        if not names:
            # Die Liste hat sich geändert: nichts neu starten, die Zustandsnachricht zeigt die jetzige
            self._answer(cq, tr("Die Liste hat sich geändert, nichts neu gestartet."), alert=True)
            with state.locked(self.cfg.state_dir) as st:
                restarts.note(st, found, self.cfg.services.exclude)
            self.pump()
            return
        # Reservieren, bevor etwas angezeigt wird: Ein Prüflauf, der inzwischen einspielt, hat Vorrang.
        # Unter derselben Sperre prüft der Prüflauf restart_request, einer von beiden gewinnt.
        with state.locked(self.cfg.state_dir) as st:
            refused = busy.refusal("restart", st)
            if not refused:
                st.restart_request = {"at": time.time(), "services": names}
                st.resolve("services")
                st.restart_services = []
        if refused:
            self._answer(cq, plain(refused), alert=True)
            return
        # Erst alles anzeigen und festhalten: Ist der Bot selbst dabei, wird er gleich beendet
        self._answer(cq, tr("Neustart läuft …"))
        self.pump()
        # Abgewiesene merken, bevor sich der Bot selbst einreiht: Das kann ihn sofort beenden
        self._note_restart_errors(restarts.start([n for n in names if n != restarts.BOT_SERVICE]))
        if restarts.BOT_SERVICE in names:
            self._note_restart_errors(restarts.start([restarts.BOT_SERVICE]))

    def _note_restart_errors(self, errors: dict[str, str]) -> None:
        if errors:
            with state.locked(self.cfg.state_dir) as st:
                st.restart_request["errors"] = {**st.restart_request.get("errors", {}), **errors}

    def press_resolved(self, cq: dict, pressed: dict, kind: str) -> None:
        """Fehlgeschlagenes Einspielen als von Hand behoben quittieren."""
        if kind != "apply-failed":
            self._answer(cq, tr("Unbekannte Schaltfläche."))
            return
        self._answer(cq)
        with state.locked(self.cfg.state_dir) as st:
            if st.resolve(kind):
                panel.event(st, panel.line(self.cfg.hostname, "✅", tr("{title}: als behoben quittiert.",
                                                                       title=title(kind))))
        self.pump()

    def press_dismiss(self, cq: dict, pressed: dict, arg: str) -> None:
        """Update-Angebot verwerfen; auf einer älteren Nachricht (bis 0.9) diese entfernen."""
        self._answer(cq)
        if self._on_panel(pressed):
            with state.locked(self.cfg.state_dir) as st:
                st.update_offer = {}
            self.pump()
        elif not messages.delete(self.tg, pressed):
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
        except TelegramError as e:
            log.debug(tr("Antwort nicht gesendet: %s"), e)

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

    @staticmethod
    def _rpmdb_mtime() -> float:
        for path in RPMDB_PATHS:
            try:
                return path.stat().st_mtime
            except FileNotFoundError:
                continue
        return 0.0
