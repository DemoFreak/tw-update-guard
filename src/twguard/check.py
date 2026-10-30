# SPDX-License-Identifier: GPL-3.0-or-later
"""Prüflauf (Timer): Repos auffrischen, Plan einstufen, Routine-Updates einspielen."""

from __future__ import annotations

import logging
import time
from html import escape
from pathlib import Path

from . import details, messages, state, zypper
from .config import Config
from .i18n import tr
from .notify import (adopt_migration, format_issue, format_observed, format_reasons, format_report, issue_buttons,
                     observed_buttons, send_due, use_language)
from .rules import classify, describe, plan_summary
from .telegram import Buttons, Telegram, TelegramError

log = logging.getLogger(__name__)

KEEP_PLANS = 20
# Ältere Anfragen von /check oder /approve werden nicht mehr beantwortet
REQUEST_MAX_AGE = 3600


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
        messages.delete(tg, request)
        request = {"chat_id": request.get("chat_id")}
    shown = messages.edit_or_send(tg, request, text, buttons)
    if shown and topic:
        with state.locked(cfg.state_dir) as st:
            old = st.messages.get(topic)
            messages.claim(st, topic, shown)
            if old and old["message_id"] != shown["message_id"]:
                messages.retire(tg, old)
    return shown is not None


def _fail(cfg: Config, tg: Telegram, kind: str, detail: str) -> int:
    log.error("%s", detail)
    finish(cfg, tg, f"❌ <b>{escape(cfg.hostname)}</b>: {escape(detail.splitlines()[0])}\n"
                    + tr("Details in der Meldung oder per journalctl -u twguard-check."))
    with state.locked(cfg.state_dir) as st:
        st.open_issue(kind, detail)
        send_due(cfg, st, tg)
    return 1


def run(cfg: Config, dry_run: bool = False) -> int:
    tg = Telegram(cfg.token, cfg.chat_id)
    use_language(cfg)
    try:
        return _run(cfg, tg, dry_run)
    finally:
        if tg.migration and not dry_run:
            with state.locked(cfg.state_dir) as st:
                adopt_migration(cfg, st, tg, *tg.migration)


def _run(cfg: Config, tg: Telegram, dry_run: bool) -> int:

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
        finish(cfg, tg, f"✅ <b>{escape(cfg.hostname)}</b>: " + tr("Prüfung fertig, keine Updates."))
        return 0

    if not (verdict.auto or approved):
        log.info(tr("Freigabe nötig: %s"), "; ".join(verdict.reasons))
        detail = format_reasons(verdict.reasons)
        if expired:
            detail = tr("Die Freigabe galt einem älteren Stand und ist verfallen.") + "\n\n" + detail
        with state.locked(cfg.state_dir) as st:
            issue, _ = st.open_issue("manual", detail, fingerprint=fingerprint, approvable=verdict.approvable)
            now = time.time()
            text, buttons = format_issue(cfg.hostname, issue, now, cfg.escalation), issue_buttons(issue)
        # Auf Anfrage sofort und vollständig antworten, auch in der Ruhezeit
        if finish(cfg, tg, text, buttons, topic="issue:manual"):
            with state.locked(cfg.state_dir) as st:
                if "manual" in st.issues and not st.issues["manual"].last_notified:
                    st.issues["manual"].last_notified = now
                    st.issues["manual"].notify_count += 1
        with state.locked(cfg.state_dir) as st:
            send_due(cfg, st, tg)
        return 0

    if cfg.observe and not approved:
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

    log.info(tr("spiele %d Pakete ein (%s)"), plan.packages_to_change, tr("freigegeben") if approved else tr("Routine"))
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
    services = zypper.services_needing_restart()
    with state.locked(cfg.state_dir) as st:
        st.resolve("manual")
        st.resolve("apply-failed")
        st.approved_fingerprint = ""
        if reboot and "reboot" not in st.issues:
            st.open_issue("reboot", tr("Nach dem Update ist ein Neustart nötig (Kernel oder Systembibliotheken)."))
        send_due(cfg, st, tg)
    report = format_report(cfg.hostname, plan.packages_to_change, approved, reboot, services, deviation, unverified)
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
