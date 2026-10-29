# SPDX-License-Identifier: GPL-3.0-or-later
"""Kommandozeile: twguard check | bot | status | classify | telegram-ids | test-message | self-update."""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from html import escape
from pathlib import Path

from . import COMMIT, __version__, config, state
from .notify import TITLES, fmt_age
from .rules import classify, describe
from .telegram import Telegram, TelegramError
from .zypper import parse_plan


def _config(path: str | None, required: bool = True) -> config.Config:
    try:
        cfg = config.load(path)
    except FileNotFoundError:
        if required:
            raise SystemExit(f"Keine Konfiguration gefunden ({path or config.DEFAULT_PATH})")
        return config.Config()
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
            "chat_id %s in der Konfiguration ist veraltet, nutze %s (bitte eintragen)",
            cfg.chat_id, migration["to"])
        cfg.chat_id = migration["to"]


def cmd_classify(cfg: config.Config, file: str, exit_code: int) -> int:
    plan = parse_plan(Path(file).read_text(), exit_code)
    verdict = classify(plan, cfg.rules)
    print(describe(plan))
    print()
    print("Einstufung:", "Routine" if verdict.auto else "Freigabe nötig")
    for reason in verdict.reasons:
        print("  -", reason)
    print("Fingerabdruck:", plan.fingerprint())
    return 0


def cmd_status(cfg: config.Config) -> int:
    st = state.load(cfg.state_dir / "state.json")
    now = time.time()
    last = f"vor {fmt_age(now - st.last_check_ok)}" if st.last_check_ok else "nie"
    print(f"{cfg.hostname}: letzte erfolgreiche Prüfung {last}")
    if not st.issues:
        print("Nichts offen.")
    for issue in st.issues.values():
        print(f"\n[{issue.kind}] {TITLES.get(issue.kind, issue.kind)}, seit {fmt_age(now - issue.since)}, "
              f"{issue.notify_count} Meldungen")
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
        print(BOT_RUNNING, file=sys.stderr)
        return 1
    tg = Telegram(cfg.token, cfg.chat_id)
    for attempt in range(1, attempts + 1):
        try:
            updates = tg.get_updates(0, timeout=0, slack=15)
            break
        except TelegramError as e:
            if "Conflict" in str(e):
                print(BOT_RUNNING, file=sys.stderr)
                return 1
            if attempt == attempts:
                raise
            print(f"Versuch {attempt} gescheitert ({e}), neuer Versuch …", file=sys.stderr)
    if not updates:
        print("Keine Nachrichten. Schreib dem Bot (in der Gruppe: /start@botname) und versuch es erneut.")
    for update in updates:
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        user = msg.get("from") or {}
        print(f"Chat {chat.get('id')} ({chat.get('type')}, {chat.get('title') or chat.get('username', '')}) "
              f"– Benutzer {user.get('id')} ({user.get('username') or user.get('first_name', '')}): "
              f"{(msg.get('text') or '')[:40]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="twguard",
        description="Tumbleweed-Updates: Routine einspielen, Kritisches melden und nachhaken.",
    )
    p.add_argument("--config", help=f"Konfigurationsdatei (Standard: {config.DEFAULT_PATH})")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"{__version__} ({COMMIT})")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help="Repos auffrischen, einstufen, Routine-Updates einspielen (Timer)")
    c.add_argument("--dry-run", action="store_true", help="nur einstufen und ausgeben; nichts einspielen, nichts senden")
    sub.add_parser("bot", help="Telegram-Bot und Erinnerungen (Dienst)")
    sub.add_parser("status", help="offene Punkte anzeigen")
    cl = sub.add_parser("classify", help="gespeicherte Ausgabe von 'zypper --xmlout dup --dry-run --details' einstufen")
    cl.add_argument("file")
    cl.add_argument("--exit-code", type=int, default=0, help="Exit-Code des zypper-Laufs")
    sub.add_parser("telegram-ids", help="Chat- und Benutzer-IDs aus den letzten Nachrichten an den Bot zeigen")
    sub.add_parser("test-message", help="Testnachricht an den konfigurierten Chat senden")
    su = sub.add_parser("self-update", help="twguard aus der Git-Arbeitskopie aktualisieren (startet /update)")
    su.add_argument("commit", help="Ziel-Commit (vollständiger Hash)")
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
        from .ota import self_update
        return self_update(cfg, args.commit)
    if args.command == "test-message":
        Telegram(cfg.token, cfg.chat_id).send(f"👋 Testnachricht von <b>{escape(cfg.hostname)}</b>")
        print("gesendet")
        return 0
    return 2
