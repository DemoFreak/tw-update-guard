# SPDX-License-Identifier: GPL-3.0-or-later
"""Kommandozeile: twguard check | bot | status | classify | telegram-ids | config-diff | test-message | self-update."""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from html import escape
from pathlib import Path

from . import COMMIT, __version__, config, state
from .i18n import set_language, tr, trn
from .notify import fmt_age, title
from .rules import classify, describe
from .telegram import Telegram, TelegramError
from .zypper import parse_plan


def _config(path: str | None, required: bool = True) -> config.Config:
    try:
        cfg = config.load(path)
    except FileNotFoundError:
        if required:
            raise SystemExit(tr("Keine Konfiguration gefunden ({path})", path=path or config.DEFAULT_PATH))
        return config.Config()
    set_language(cfg.language)
    apply_chat_migration(cfg)
    return cfg


def apply_chat_migration(cfg: config.Config) -> None:
    """Gruppe ist zur Supergruppe geworden: neue ID nutzen, solange config.toml die alte enthält."""
    try:
        migration = state.load(cfg.state_dir / "state.json").chat_migration
    except (OSError, ValueError):
        return  # z. B. ohne root kein Zugriff auf den Zustand
    if migration and migration.get("from") == cfg.chat_id:
        logging.getLogger(__name__).warning(
            tr("chat_id %s in der Konfiguration ist veraltet, nutze %s (bitte eintragen)"),
            cfg.chat_id, migration["to"])
        cfg.chat_id = migration["to"]


def cmd_classify(cfg: config.Config, file: str, exit_code: int) -> int:
    plan = parse_plan(Path(file).read_text(), exit_code)
    verdict = classify(plan, cfg.rules)
    print(describe(plan))
    print()
    print(tr("Einstufung:"), tr("Routine") if verdict.auto else tr("Freigabe nötig"))
    for reason in verdict.reasons:
        print("  -", reason)
    print(tr("Fingerabdruck:"), plan.fingerprint())
    return 0


def cmd_status(cfg: config.Config) -> int:
    st = state.load(cfg.state_dir / "state.json")
    now = time.time()
    last = tr("vor {age}", age=fmt_age(now - st.last_check_ok)) if st.last_check_ok else tr("nie")
    print(tr("{host}: letzte erfolgreiche Prüfung {last}", host=cfg.hostname, last=last))
    if not st.issues and not st.pending_text:
        print(tr("Nichts offen."))
    elif not st.issues:
        print(tr("Keine offenen Punkte."))
    if st.pending_text:
        print(tr("Ausstehend, nicht eingespielt (Beobachtungsmodus): {plan}", plan=st.pending_text))
    for issue in st.issues.values():
        print(f"\n[{issue.kind}] " + tr("{title}, seit {age}", title=title(issue.kind), age=fmt_age(now - issue.since))
              + ", " + trn("{n} Meldung", "{n} Meldungen", issue.notify_count))
        if issue.detail:
            print(issue.detail)
    return 0


BOT_RUNNING = ("Der Bot-Dienst läuft und holt die Nachrichten selbst ab. Erst anhalten:\n"
               "  systemctl stop twguard-bot\n"
               "dann in der Gruppe erneut /start@botname schreiben und twguard telegram-ids wiederholen.")


def bot_running() -> bool:
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", "twguard-bot.service"]).returncode == 0
    except OSError:
        return False


def cmd_telegram_ids(cfg: config.Config, attempts: int = 3) -> int:
    # Ein laufender Bot bestätigt die Nachrichten sofort; danach liefert getUpdates hier nichts mehr
    if bot_running():
        print(tr(BOT_RUNNING), file=sys.stderr)
        return 1
    tg = Telegram(cfg.token, cfg.chat_id)
    for attempt in range(1, attempts + 1):
        try:
            updates = tg.get_updates(0, timeout=0, slack=15)
            break
        except TelegramError as e:
            if "Conflict" in str(e):
                print(tr(BOT_RUNNING), file=sys.stderr)
                return 1
            if attempt == attempts:
                raise
            print(tr("Versuch {n} gescheitert ({error}), neuer Versuch …", n=attempt, error=e), file=sys.stderr)
    if not updates:
        print(tr("Keine Nachrichten. Schreib dem Bot (in der Gruppe: /start@botname) und versuch es erneut."))
    for update in updates:
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        user = msg.get("from") or {}
        print(f"Chat {chat.get('id')} ({chat.get('type')}, {chat.get('title') or chat.get('username', '')}) – "
              + tr("Benutzer") + f" {user.get('id')} ({user.get('username') or user.get('first_name', '')}): "
              f"{(msg.get('text') or '')[:40]}")
    return 0


def _early_language(argv: list[str] | None) -> None:
    """Sprache schon für --help setzen; ohne lesbare Konfiguration (kein root) bleibt die Vorgabe."""
    early = argparse.ArgumentParser(add_help=False)
    early.add_argument("--config")
    try:
        set_language(config.load(early.parse_known_args(argv)[0].config).language)
    except (OSError, ValueError, SystemExit):
        pass


def main(argv: list[str] | None = None) -> int:
    _early_language(argv)
    p = argparse.ArgumentParser(
        prog="twguard",
        description=tr("Tumbleweed-Updates: Routine einspielen, Kritisches melden und nachhaken."),
    )
    p.add_argument("--config", help=tr("Konfigurationsdatei (Standard: {path})", path=config.DEFAULT_PATH))
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"{__version__} ({COMMIT})")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help=tr("Repos auffrischen, einstufen, Routine-Updates einspielen (Timer)"))
    c.add_argument("--dry-run", action="store_true", help=tr("nur einstufen und ausgeben; nichts einspielen, nichts senden"))
    sub.add_parser("bot", help=tr("Telegram-Bot und Erinnerungen (Dienst)"))
    sub.add_parser("status", help=tr("offene Punkte anzeigen"))
    cl = sub.add_parser("classify", help=tr("gespeicherte Ausgabe von 'zypper --xmlout dup --dry-run --details' einstufen"))
    cl.add_argument("file", metavar="<file>")
    cl.add_argument("--exit-code", type=int, default=0, help=tr("Exit-Code des zypper-Laufs"))
    sub.add_parser("telegram-ids", help=tr("Chat- und Benutzer-IDs aus den letzten Nachrichten an den Bot zeigen"))
    sub.add_parser("config-diff", help=tr("nur die Werte der Konfiguration zeigen, die von den Vorgaben abweichen"))
    sub.add_parser("test-message", help=tr("Testnachricht an den konfigurierten Chat senden"))
    su = sub.add_parser("self-update", help=tr("twguard aus der Git-Arbeitskopie aktualisieren"))
    su.add_argument("commit", nargs="?", default="", metavar="<commit>", help=tr("Ziel-Commit (Standard: neuester Stand)"))
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    if args.command == "classify":
        return cmd_classify(_config(args.config, required=False), args.file, args.exit_code)
    if args.command == "check":
        from .check import run
        return run(_config(args.config, required=not args.dry_run), dry_run=args.dry_run)

    try:
        return _run_command(args)
    except TelegramError as e:
        print(f"Telegram: {e}", file=sys.stderr)
        return 1


def _run_command(args: argparse.Namespace) -> int:
    cfg = _config(args.config)
    if args.command == "bot":
        from .bot import Bot
        Bot(cfg).run()
        return 0
    if args.command == "status":
        return cmd_status(cfg)
    if args.command == "telegram-ids":
        return cmd_telegram_ids(cfg)
    if args.command == "self-update":
        from .ota import command
        return command(cfg, args.commit)
    if args.command == "config-diff":
        print(config.to_toml(config.changed_values(args.config)), end="")
        return 0
    if args.command == "test-message":
        Telegram(cfg.token, cfg.chat_id).send(tr("👋 Testnachricht von <b>{host}</b>", host=escape(cfg.hostname)))
        print(tr("gesendet"))
        return 0
    return 2
