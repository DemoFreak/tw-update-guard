# SPDX-License-Identifier: GPL-3.0-or-later
"""Prüflauf (Timer): Repos auffrischen, Plan einstufen, Routine-Updates einspielen."""

from __future__ import annotations

import logging
import time
from html import escape
from pathlib import Path

from . import busy, details, panel, restarts, state, zypper
from .config import MODES, Config
from .i18n import tr, trn
from .notify import format_reasons, format_report, use_language, use_mode
from .rules import Verdict, classify, describe, plan_summary

log = logging.getLogger(__name__)

KEEP_PLANS = 20
# Ältere Anfragen von /check oder /approve werden nicht mehr als Ereignis beantwortet
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


def phase(cfg: Config, name: str, detail: str = "") -> None:
    """Was der Prüflauf gerade tut, für die Zustandsnachricht; mit PID, damit ein Absturz erkennbar ist."""
    with state.locked(cfg.state_dir) as st:
        st.activity["check"] = {**state.holder(), "phase": name, "detail": detail}


def _done(cfg: Config) -> None:
    with state.locked(cfg.state_dir) as st:
        if st.activity.get("check", {}).get("pid") == state.holder()["pid"]:
            del st.activity["check"]


def note(cfg: Config, text: str, loud: bool = False) -> None:
    with state.locked(cfg.state_dir) as st:
        panel.event(st, text, loud=loud)


def _fail(cfg: Config, kind: str, detail: str, requested: bool) -> int:
    """Fehler sind offene Punkte; auf Anfrage kommt zusätzlich die erste Zeile als Ereignis."""
    log.error("%s", detail)
    with state.locked(cfg.state_dir) as st:
        st.open_issue(kind, detail)
        if requested:
            panel.event(st, panel.line(cfg.hostname, "❌", escape(detail.splitlines()[0])))
    return 1


def _locked(cfg: Config, holder: str) -> int:
    """Sperre nach LOCK_WAIT noch belegt: kein Fehler, der nächste Prüflauf versucht es wieder."""
    log.warning(tr("Paketverwaltung seit %d min belegt durch %s, nichts eingespielt"), LOCK_WAIT // 60, holder)
    note(cfg, panel.line(cfg.hostname, "⏸️", tr(
        "Paketverwaltung seit {minutes} min belegt durch {holder}, nichts eingespielt; der nächste Prüflauf "
        "versucht es erneut.", minutes=LOCK_WAIT // 60, holder=escape(holder))), loud=True)
    return 0


def run(cfg: Config, dry_run: bool = False) -> int:
    """Ein Prüflauf. Er sendet nichts selbst: Ergebnisse stehen im Zustand, der Bot stellt sie zu."""
    use_language(cfg)
    configured = cfg.mode
    use_mode(cfg)
    requested = False
    if not dry_run:
        with state.locked(cfg.state_dir) as st:
            requested = time.time() - st.check_request.get("at", 0) <= REQUEST_MAX_AGE
            st.check_request = {}
            st.activity["check"] = {**state.holder(), "phase": "refresh", "detail": ""}
    deadline = time.monotonic() + LOCK_WAIT
    shown = ""
    try:
        while True:
            try:
                return _run(cfg, dry_run, configured, requested)
            except zypper.Locked as e:
                # Nichts eingespielt (zypper bricht bei der Sperre vor jeder Änderung ab)
                if time.monotonic() >= deadline:
                    if dry_run:
                        print(tr("Paketverwaltung belegt: {holder}", holder=e.holder))
                        return 1
                    return _locked(cfg, e.holder)
                if e.holder != shown:
                    shown = e.holder
                    log.info(tr("Paketverwaltung belegt durch %s, warte"), e.holder)
                    if dry_run:
                        print(tr("Paketverwaltung belegt: {holder}", holder=e.holder))
                # Jeder neue Anlauf setzt die Phase zurück; während des Wartens zeigt sie das Warten
                if not dry_run:
                    phase(cfg, "wait-zypp", e.holder)
                time.sleep(LOCK_POLL)
    finally:
        if not dry_run:
            _done(cfg)


def _reserve(cfg: Config, configured: str, approved: bool) -> tuple[str, dict]:
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
            # Auch ohne Anfrage (Timer) melden, sonst bliebe z. B. ein stummes logind unbemerkt; nur der
            # andere Prüflauf meldet sein Ergebnis selbst
            if kind != busy.APPLY:
                note(cfg, panel.line(cfg.hostname, "⏸️", tr(
                    "Nichts eingespielt: {reason}. Der nächste Prüflauf versucht es erneut.", reason=escape(reason))),
                    loud=True)
            return "busy", {}
        if time.monotonic() >= deadline:
            break
        if not waiting:
            waiting = True
            log.info(tr("Dienste werden neu gestartet, das Einspielen wartet"))
            phase(cfg, "wait-restart")
        time.sleep(RESTART_POLL)
    log.warning(tr("Neustart von Diensten läuft noch, nichts eingespielt"))
    note(cfg, panel.line(cfg.hostname, "⏸️", tr(
        "Der Neustart von Diensten läuft noch, das Update wurde nicht eingespielt. Der nächste Prüflauf "
        "versucht es erneut.")), loud=True)
    return "busy", {}


def _release(cfg: Config, mine: dict) -> None:
    """Nur die eigene Reservierung lösen, nie die eines anderen Prüflaufs."""
    with state.locked(cfg.state_dir) as st:
        if mine and st.applying == mine:
            st.applying = {}


def _run(cfg: Config, dry_run: bool, configured: str | None = None, requested: bool = False) -> int:
    host = cfg.hostname
    # Steht ein Neustart bevor, bräche er die Prüfung ab und ihr Ergebnis käme nie an
    with state.locked(cfg.state_dir) as st:
        refused = busy.refusal("check", st)
        if refused and requested and not dry_run:
            panel.event(st, panel.line(host, "⏸️", escape(refused)))
    if refused:
        log.info("%s", refused)
        if dry_run:
            print(refused)
            return 1
        return 0

    if not dry_run:
        phase(cfg, "refresh")
    errors = zypper.refresh()
    if errors:
        detail = tr("Repos lassen sich nicht auffrischen:") + "\n" + "\n".join(errors)
        if dry_run:
            print(detail)
            return 1
        return _fail(cfg, "error", detail, requested)

    if not dry_run:
        phase(cfg, "dry-run")
    plan = zypper.dry_run(cfg.dup_args)
    if plan.failed:
        detail = tr("zypper dup --dry-run schlägt fehl:") + "\n" + "\n".join(plan.errors)
        if dry_run:
            print(detail)
            return 1
        return _fail(cfg, "error", detail, requested)

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
            if requested:
                panel.event(st, panel.line(host, "🔄", tr("Prüfung fertig, keine Updates.")))
    if plan.empty:
        log.info(tr("nichts zu tun"))
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
            if changed:
                # Ein anderer Plan ist ein neues Angebot: sofort melden, ein Aufschub galt dem alten
                issue.last_notified = issue.snoozed_until = 0.0
                issue.notify_count = issue.snooze_count = 0
            if requested or expired:
                panel.event(st, panel.line(host, "🔄", tr("Prüfung fertig: {summary}, Freigabe nötig.",
                                                          summary=escape(plan_summary(plan)))
                                           + (" " + tr("Die vorige Freigabe ist verfallen.") if expired else "")))
        return 0

    decision, mine = _reserve(cfg, configured or cfg.mode, approved)
    if decision == "busy":
        return 0
    if decision == "apply":
        try:
            return _apply(cfg, plan, verdict, fingerprint, approved, requested)
        finally:
            _release(cfg, mine)

    log.info(tr("Beobachtungsmodus: würde %d Pakete einspielen"), plan.packages_to_change)
    with state.locked(cfg.state_dir) as st:
        st.resolve("manual")
        st.pending_text = plan_summary(plan)
        # Ein neuer Stand einmal als Ereignis, danach steht er nur noch in der Zustandsnachricht
        if requested or st.observed_fingerprint != fingerprint:
            text = trn("{n} Paket wäre Routine und würde eingespielt, im Beobachtungsmodus nicht.",
                       "{n} Pakete wären Routine und würden eingespielt, im Beobachtungsmodus nicht.",
                       plan.packages_to_change)
            if verdict.reboot_expected:
                text += " " + tr("Danach wäre ein Neustart nötig.")
            panel.event(st, panel.line(host, "👀", text))
        st.observed_fingerprint = fingerprint
    return 0


def _apply(cfg: Config, plan: zypper.Plan, verdict: Verdict, fingerprint: str, approved: bool,
           requested: bool = False) -> int:
    log.info(tr("spiele %d Pakete ein (%s)"), plan.packages_to_change, tr("freigegeben") if approved else tr("Routine"))
    # Das Einspielen dauert Minuten; die Zustandsnachricht zeigt, dass es läuft und was kommt
    summary = plan_summary(plan)
    if verdict.reboot_expected:
        summary += ", " + tr("danach Neustart nötig")
    phase(cfg, "apply", summary)
    rc, output, applied = zypper.apply(cfg.dup_args)
    log.info(tr("zypper dup: Exit-Code %s\n%s"), rc, "\n".join(output.splitlines()[-200:]))
    if rc not in zypper.OK_CODES:
        tail = "\n".join(output.strip().splitlines()[-15:])
        return _fail(cfg, "apply-failed", tr("zypper dup endete mit Exit-Code {rc}:\n{tail}", rc=rc, tail=tail),
                     requested)
    deviation, unverified = [], applied.empty
    if unverified:
        log.warning(tr("Plan des Einspielens nicht lesbar, Abgleich mit dem Probelauf entfällt"))
    elif applied.fingerprint() != fingerprint:
        deviation = zypper.diff_plans(plan, applied) or [tr("(Unterschied nicht darstellbar, siehe Journal)")]
        log.warning(tr("eingespielter Plan weicht vom geprüften ab:\n%s"), "\n".join(deviation))
    warn = bool(deviation) or unverified

    reboot = rc == zypper.EXIT_REBOOT_NEEDED or zypper.needs_rebooting() is True
    found = restarts.survey()
    # Die übrigen Dienste bietet die Zustandsnachricht zum Neustart an
    skipped = restarts.split(found.services if found else [], cfg.services.exclude)[1]
    report = format_report(cfg.hostname, plan.packages_to_change, approved, skipped, deviation, unverified)
    with state.locked(cfg.state_dir) as st:
        st.resolve("manual")
        st.resolve("apply-failed")
        st.approved_fingerprint = ""
        if reboot and "reboot" not in st.issues:
            st.open_issue("reboot", tr("Nach dem Update ist ein Neustart nötig (Kernel oder Systembibliotheken)."))
        if found is not None:
            restarts.note(st, found, cfg.services.exclude)
        if cfg.report_routine or approved or warn or requested:
            panel.event(st, report, loud=warn)
    return 0
