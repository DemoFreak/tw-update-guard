# SPDX-License-Identifier: GPL-3.0-or-later
"""Prüflauf (Timer): Repos auffrischen, Plan einstufen, Routine-Updates einspielen."""

from __future__ import annotations

import logging
import time
from html import escape
from pathlib import Path

from . import busy, details, messages, restarts, state, zypper
from .config import MODES, Config
from .i18n import tr
from .notify import (adopt_migration, format_issue, format_observed, format_reasons, format_report, format_status,
                     issue_buttons, observed_buttons, send_due, status_buttons, use_language, use_mode)
from .rules import Verdict, classify, describe, plan_summary
from .telegram import Buttons, Telegram, TelegramError

log = logging.getLogger(__name__)

KEEP_PLANS = 20
# Ältere Anfragen von /check oder /approve werden nicht mehr beantwortet
REQUEST_MAX_AGE = 3600
# So lange wartet das Einspielen auf einen laufenden Dienst-Neustart (dessen Grenze sind 15 min)
RESTART_WAIT = 20 * 60
RESTART_POLL = 10
# So lange wartet der Prüflauf auf eine belegte Paketverwaltung (purge-kernels nach dem Boot braucht Minuten).
# Danach beginnt er von vorn: Der fremde Vorgang kann Repos, Pakete und damit den Plan geändert haben
LOCK_WAIT = 30 * 60
LOCK_POLL = 15


def save_plan(state_dir: Path, plan: zypper.Plan, fingerprint: str, keep: int = KEEP_PLANS) -> Path | None:
    """XML des Probelaufs unter plans/ sichern (je Fingerabdruck einmal, die letzten `keep`)."""
    if not plan.raw:
        return None
    plans = state_dir / "plans"
    plans.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(plans.glob(f"dup-*-{fingerprint}.xml")):
        return None
    path = plans / time.strftime(f"dup-%Y%m%d-%H%M-{fingerprint}.xml")
    path.write_text(plan.raw)
    for old in sorted(plans.glob("dup-*.xml"))[:-keep]:
        old.unlink()
    return path


def finish(cfg: Config, tg: Telegram, text: str, buttons: Buttons | None = None, topic: str = "",
           loud: bool = False) -> bool:
    """Ergebnis in die Wartemeldung von /check oder einer Freigabe schreiben, falls es eine gibt.

    Mit `topic` gehört die Nachricht danach zu diesem Thema (z. B. "issue:manual"); eine ältere
    Nachricht zum selben Thema wird entfernt. Mit `loud` kommt das Ergebnis als neue Nachricht
    mit Ton (Bearbeiten ist lautlos); die Wartemeldung verschwindet.
    """
    with state.locked(cfg.state_dir) as st:
        request, st.check_request = st.check_request, {}
    if not request or time.time() - request.get("at", 0) > REQUEST_MAX_AGE:
        return False
    if loud:
        if request.get("message_id"):
            messages.delete(tg, request)
        request = {"chat_id": request.get("chat_id")}
    shown = messages.edit_or_send(tg, request, text, buttons)
    if shown and topic:
        with state.locked(cfg.state_dir) as st:
            old = st.messages.get(topic)
            messages.claim(st, topic, shown)
            if old and not messages.same(old, shown):
                messages.retire(tg, old)
    return shown is not None


def progress(cfg: Config, tg: Telegram, text: str) -> None:
    """Zwischenstand in die Wartemeldung schreiben; `finish` ersetzt ihn durch das Ergebnis."""
    with state.locked(cfg.state_dir) as st:
        request = dict(st.check_request)
    if request.get("message_id") and time.time() - request.get("at", 0) <= REQUEST_MAX_AGE:
        messages.edit(tg, request, text)


def _fail(cfg: Config, tg: Telegram, kind: str, detail: str) -> int:
    log.error("%s", detail)
    finish(cfg, tg, f"❌ <b>{escape(cfg.hostname)}</b>: {escape(detail.splitlines()[0])}\n"
                    + tr("Details in der Meldung oder per journalctl -u twguard-check."))
    with state.locked(cfg.state_dir) as st:
        st.open_issue(kind, detail)
        send_due(cfg, st, tg)
    return 1


def _locked(cfg: Config, tg: Telegram, holder: str) -> int:
    """Sperre nach LOCK_WAIT noch belegt: kein Fehler, der nächste Prüflauf versucht es wieder."""
    log.warning(tr("Paketverwaltung seit %d min belegt durch %s, nichts eingespielt"), LOCK_WAIT // 60, holder)
    finish(cfg, tg, f"⏸️ <b>{escape(cfg.hostname)}</b>: " + tr(
        "Die Paketverwaltung ist seit {minutes} min belegt durch {holder}, nichts eingespielt. Der nächste "
        "Prüflauf versucht es erneut (sofort: /check).", minutes=LOCK_WAIT // 60, holder=escape(holder)), loud=True)
    return 0


def run(cfg: Config, dry_run: bool = False) -> int:
    tg = Telegram(cfg.token, cfg.chat_id)
    use_language(cfg)
    configured = cfg.mode
    use_mode(cfg)
    deadline = time.monotonic() + LOCK_WAIT
    shown = ""
    try:
        while True:
            try:
                return _run(cfg, tg, dry_run, configured)
            except zypper.Locked as e:
                # Nichts eingespielt (zypper bricht bei der Sperre vor jeder Änderung ab)
                if time.monotonic() >= deadline:
                    if dry_run:
                        print(tr("Paketverwaltung belegt: {holder}", holder=e.holder))
                        return 1
                    return _locked(cfg, tg, e.holder)
                if e.holder != shown:
                    shown = e.holder
                    log.info(tr("Paketverwaltung belegt durch %s, warte"), e.holder)
                    if not dry_run:
                        progress(cfg, tg, tr("⏳ <b>{host}</b>: Die Paketverwaltung ist belegt durch {holder}, "
                                             "twguard wartet darauf.", host=escape(cfg.hostname),
                                             holder=escape(e.holder)))
                time.sleep(LOCK_POLL)
    finally:
        if tg.migration and not dry_run:
            with state.locked(cfg.state_dir) as st:
                adopt_migration(cfg, st, tg, *tg.migration)


def _reserve(cfg: Config, tg: Telegram, configured: str, approved: bool) -> tuple[str, dict]:
    """Unmittelbar vor dem Einspielen entscheiden: "observe", "apply" (mit Reservierung) oder "busy".

    Modus und Reservierung stehen unter derselben Sperre wie /mode und der Neustart-Knopf: Ein
    bestätigtes /mode observe oder ein schon eingereihter Dienst-Neustart gilt also sicher. Die
    lebende Reservierung eines anderen Prüflaufs wird nie übernommen, nur eine verwaiste ersetzt.
    """
    deadline = time.monotonic() + RESTART_WAIT
    waiting = False
    while True:
        with state.locked(cfg.state_dir) as st:
            cfg.mode = st.mode_override if st.mode_override in MODES else configured
            if cfg.observe and not approved:
                return "observe", {}
            found = busy.blockers("apply", st)
            if not found:
                st.applying = state.holder()
                return "apply", dict(st.applying)
        # Auf einen Dienst-Neustart lohnt das Warten, er endet in Minuten; alles andere weist ab
        kinds = {kind for kind, _ in found}
        if kinds != {busy.RESTART}:
            kind, reason = next((k, text) for k, text in found if k != busy.RESTART)
            log.warning(tr("nichts eingespielt: %s"), reason)
            text = f"⏸️ <b>{escape(cfg.hostname)}</b>: " + tr(
                "Nichts eingespielt: {reason}. Der nächste Prüflauf versucht es erneut.", reason=escape(reason))
            # Auch ohne Anfrage (Timer) melden, sonst bliebe z. B. ein stummes logind unbemerkt; nur der
            # andere Prüflauf meldet sein Ergebnis selbst
            if not finish(cfg, tg, text, loud=True) and kind != busy.APPLY:
                try:
                    tg.send(text)
                except TelegramError as e:
                    log.warning(tr("Meldung nicht gesendet: %s"), e)
            return "busy", {}
        if time.monotonic() >= deadline:
            break
        if not waiting:
            waiting = True
            log.info(tr("Dienste werden neu gestartet, das Einspielen wartet"))
            progress(cfg, tg, tr("⏳ <b>{host}</b>: Dienste werden gerade neu gestartet, das Update wartet darauf.",
                                 host=escape(cfg.hostname)))
        time.sleep(RESTART_POLL)
    log.warning(tr("Neustart von Diensten läuft noch, nichts eingespielt"))
    finish(cfg, tg, f"⏸️ <b>{escape(cfg.hostname)}</b>: " + tr(
        "Der Neustart von Diensten läuft noch, das Update wurde nicht eingespielt. Der nächste Prüflauf "
        "versucht es erneut (sofort: /check)."), loud=True)
    return "busy", {}


def _release(cfg: Config, mine: dict) -> None:
    """Nur die eigene Reservierung lösen, nie die eines anderen Prüflaufs."""
    with state.locked(cfg.state_dir) as st:
        if mine and st.applying == mine:
            st.applying = {}


def _run(cfg: Config, tg: Telegram, dry_run: bool, configured: str | None = None) -> int:
    # Steht ein Neustart bevor, bräche er die Prüfung ab und ihr Ergebnis käme nie an
    with state.locked(cfg.state_dir) as st:
        refused = busy.refusal("check", st)
    if refused:
        log.info("%s", refused)
        if dry_run:
            print(refused)
            return 1
        finish(cfg, tg, f"⏸️ <b>{escape(cfg.hostname)}</b>: " + escape(refused))
        return 0

    errors = zypper.refresh()
    if errors:
        detail = tr("Repos lassen sich nicht auffrischen:") + "\n" + "\n".join(errors)
        if dry_run:
            print(detail)
            return 1
        return _fail(cfg, tg, "error", detail)

    plan = zypper.dry_run(cfg.dup_args)
    if plan.failed:
        detail = tr("zypper dup --dry-run schlägt fehl:") + "\n" + "\n".join(plan.errors)
        if dry_run:
            print(detail)
            return 1
        return _fail(cfg, tg, "error", detail)

    verdict = classify(plan, cfg.rules)
    fingerprint = plan.fingerprint()
    if dry_run:
        print(describe(plan))
        print()
        print(tr("Modus:"), tr("Beobachtung (spielt nichts ein)") if cfg.observe else tr("automatisch"))
        print(tr("Einstufung:"), tr("Routine (würde eingespielt)") if verdict.auto else tr("Freigabe nötig"))
        for reason in verdict.reasons:
            print("  -", reason)
        if verdict.reboot_expected:
            print(tr("Neustart zu erwarten"))
        return 0

    if not plan.empty:
        save_plan(cfg.state_dir, plan, fingerprint)

    with state.locked(cfg.state_dir) as st:
        st.resolve("error")
        st.last_check_ok = time.time()
        st.last_plan_text = describe(plan)
        st.last_plan = details.snapshot(plan)
        st.pending_text = ""
        approved = bool(st.approved_fingerprint) and st.approved_fingerprint == fingerprint
        expired = bool(st.approved_fingerprint) and not approved
        if not approved:
            st.approved_fingerprint = ""
        if plan.empty:
            st.resolve("manual")
            send_due(cfg, st, tg)
    if plan.empty:
        log.info(tr("nichts zu tun"))
        text, buttons = f"✅ <b>{escape(cfg.hostname)}</b>: " + tr("Prüfung fertig, keine Updates."), None
        with state.locked(cfg.state_dir) as st:
            # Die Antwort ersetzt oft die Statusnachricht: offene Punkte (z. B. Neustart) bleiben bedienbar
            if st.issues:
                text += "\n\n" + format_status(cfg.hostname, st, time.time())
                buttons = status_buttons(st)
        finish(cfg, tg, text, buttons)
        return 0

    if not (verdict.auto or approved):
        log.info(tr("Freigabe nötig: %s"), "; ".join(verdict.reasons))
        for problem in plan.problems:
            log.info("%s", problem)
        detail = format_reasons(verdict.reasons)
        if expired:
            detail = tr("Die Freigabe galt einem älteren Stand und ist verfallen.") + "\n\n" + detail
        with state.locked(cfg.state_dir) as st:
            previous = st.issues.get("manual")
            changed = previous is not None and previous.fingerprint != fingerprint
            issue, _ = st.open_issue("manual", detail, fingerprint=fingerprint, approvable=verdict.approvable)
            now = time.time()
            if changed:
                # Ein anderer Plan ist ein neues Angebot: sofort melden, ein Aufschub galt dem alten
                issue.last_notified = issue.snoozed_until = 0.0
                issue.notify_count = issue.snooze_count = 0
            text, buttons = format_issue(cfg.hostname, issue, now, cfg.escalation), issue_buttons(issue)
        # Auf Anfrage sofort und vollständig antworten, auch in der Ruhezeit
        if finish(cfg, tg, text, buttons, topic="issue:manual"):
            with state.locked(cfg.state_dir) as st:
                st.manual_shown = fingerprint
                if "manual" in st.issues and not st.issues["manual"].last_notified:
                    st.issues["manual"].last_notified = now
                    st.issues["manual"].notify_count += 1
        with state.locked(cfg.state_dir) as st:
            send_due(cfg, st, tg)
        return 0

    decision, mine = _reserve(cfg, tg, configured or cfg.mode, approved)
    if decision == "busy":
        return 0
    if decision == "apply":
        try:
            return _apply(cfg, tg, plan, verdict, fingerprint, approved)
        finally:
            _release(cfg, mine)

    log.info(tr("Beobachtungsmodus: würde %d Pakete einspielen"), plan.packages_to_change)
    text = format_observed(cfg.hostname, plan.packages_to_change, verdict.reboot_expected)
    answered = finish(cfg, tg, text, observed_buttons(fingerprint), topic="observed")
    with state.locked(cfg.state_dir) as st:
        st.resolve("manual")
        st.pending_text = plan_summary(plan)
        if answered:
            st.observed_fingerprint = fingerprint
        elif st.observed_fingerprint != fingerprint:
            try:
                messages.replace(tg, st, "observed", text, observed_buttons(fingerprint), silent=True)
                st.observed_fingerprint = fingerprint
            except TelegramError as e:
                log.warning(tr("Meldung nicht gesendet: %s"), e)
        send_due(cfg, st, tg)
    return 0


def _apply(cfg: Config, tg: Telegram, plan: zypper.Plan, verdict: Verdict, fingerprint: str, approved: bool) -> int:
    log.info(tr("spiele %d Pakete ein (%s)"), plan.packages_to_change, tr("freigegeben") if approved else tr("Routine"))
    # Das Einspielen dauert Minuten; nach /check soll sichtbar sein, dass es läuft und was kommt
    text = tr("⏳ <b>{host}</b> spielt das Update ein: {summary}. Das Ergebnis erscheint hier.",
              host=escape(cfg.hostname), summary=escape(plan_summary(plan)))
    if verdict.reboot_expected:
        text += "\n" + tr("Danach ist ein Neustart nötig.")
    progress(cfg, tg, text)
    rc, output, applied = zypper.apply(cfg.dup_args)
    log.info(tr("zypper dup: Exit-Code %s\n%s"), rc, "\n".join(output.splitlines()[-200:]))
    if rc not in zypper.OK_CODES:
        tail = "\n".join(output.strip().splitlines()[-15:])
        return _fail(cfg, tg, "apply-failed", tr("zypper dup endete mit Exit-Code {rc}:\n{tail}", rc=rc, tail=tail))
    deviation, unverified = [], applied.empty
    if unverified:
        log.warning(tr("Plan des Einspielens nicht lesbar, Abgleich mit dem Probelauf entfällt"))
    elif applied.fingerprint() != fingerprint:
        deviation = zypper.diff_plans(plan, applied) or [tr("(Unterschied nicht darstellbar, siehe Journal)")]
        log.warning(tr("eingespielter Plan weicht vom geprüften ab:\n%s"), "\n".join(deviation))
    warn = bool(deviation) or unverified

    reboot = rc == zypper.EXIT_REBOOT_NEEDED or zypper.needs_rebooting() is True
    found = zypper.services_needing_restart()
    with state.locked(cfg.state_dir) as st:
        st.resolve("manual")
        st.resolve("apply-failed")
        st.approved_fingerprint = ""
        if reboot and "reboot" not in st.issues:
            st.open_issue("reboot", tr("Nach dem Update ist ein Neustart nötig (Kernel oder Systembibliotheken)."))
        if found is not None:
            restarts.note(st, found, cfg.services.exclude)
        send_due(cfg, st, tg)
    # Die übrigen Dienste bietet die Meldung des offenen Punkts zum Neustart an
    skipped = restarts.split(found or [], cfg.services.exclude)[1]
    report = format_report(cfg.hostname, plan.packages_to_change, approved, reboot, skipped, deviation, unverified)
    # Berichte bleiben bis zum nächsten Briefing stehen; das fasst sie zusammen und löscht sie
    entry = {"at": time.time(), "packages": plan.packages_to_change}
    if not finish(cfg, tg, report, loud=warn) and (cfg.report_routine or approved or warn):
        try:
            # Routine kommt still, damit der Ton für Wichtiges reserviert bleibt
            silent = not (approved or warn)
            entry = {**messages.ref(tg.send(report, silent=silent)), "packages": plan.packages_to_change}
        except TelegramError as e:
            log.warning(tr("Bericht nicht gesendet: %s"), e)
    with state.locked(cfg.state_dir) as st:
        st.reports.append(entry)
    return 0
