# SPDX-License-Identifier: GPL-3.0-or-later
"""twguard selbst aktualisieren (OTA): neuer Stand aus einer Git-Arbeitskopie auf dem Server.

Ablauf: Der Bot holt per `git fetch` den Stand vom Git-Server und zeigt die neuen Commits.
Nach Bestätigung startet er `twguard self-update <commit>` als eigene systemd-Unit, damit
der Neustart des Bots das Update nicht abbricht. Dort: Fast-Forward, Tests, `install.sh`,
dann warten, bis der neue Bot läuft; sonst die alte Version zurückspielen.
"""

from __future__ import annotations

import logging
import os
import pwd
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from html import escape
from pathlib import Path

from . import COMMIT, messages, state
from .i18n import set_language, tr
from .config import Config, Update
from .notify import clip
from .telegram import Telegram

log = logging.getLogger(__name__)

UNIT = "twguard-update"
BOT_UNIT = "twguard-bot.service"
INSTALLED = Path("/usr/local/sbin/twguard")
PREVIOUS = Path("/usr/local/sbin/twguard.prev")
# install.sh ersetzt auch die Units; eine kaputte neue Unit hielte sonst die alte Version auf
UNIT_DIR = Path("/etc/systemd/system")
UNITS = ("twguard-check.service", "twguard-check.timer", "twguard-bot.service")
# So lange darf der neue Bot zum Starten brauchen, bevor die alte Version zurückkommt
START_TIMEOUT = 90
# Ein Update, das so lange keine Rückmeldung gibt, gilt als hängengeblieben
STUCK_AFTER = 900


class UpdateError(Exception):
    pass


@dataclass
class Pending:
    head: str
    target: str
    commits: list[str]


def _env() -> dict[str, str]:
    # Als systemd-Dienst ohne User= fehlt HOME; ssh und git sollen nie nachfragen
    home = pwd.getpwuid(os.getuid()).pw_dir
    return dict(os.environ, HOME=os.environ.get("HOME") or home, GIT_TERMINAL_PROMPT="0",
                GIT_SSH_COMMAND="ssh -o BatchMode=yes")


def _git(repo: str, *args: str, timeout: int = 120) -> str:
    try:
        p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                           env=_env(), timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise UpdateError(tr("{what}: keine Antwort nach {n} s", what=f"git {args[0]}", n=timeout)) from e
    if p.returncode:
        raise UpdateError(f"git {args[0]}: {(p.stderr or p.stdout).strip()}")
    return p.stdout.strip()


def _is_ancestor(repo: str, older: str, newer: str) -> bool:
    p = subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", older, newer], env=_env())
    return p.returncode == 0


def check_remote(upd: Update) -> Pending:
    """Neuen Stand holen und die Commits zwischen Arbeitskopie und origin/<branch> liefern."""
    if not upd.repo:
        raise UpdateError(tr("OTA ist nicht eingerichtet: [update] repo fehlt in der Konfiguration."))
    if _git(upd.repo, "status", "--porcelain", "--untracked-files=no"):
        raise UpdateError(tr("{repo} hat lokale Änderungen, bitte per SSH klären.", repo=upd.repo))
    _git(upd.repo, "fetch", "--quiet", "origin", upd.branch)
    head = _git(upd.repo, "rev-parse", "HEAD")
    target = _git(upd.repo, "rev-parse", f"origin/{upd.branch}")
    if head == target:
        return Pending(head, target, [])
    if not _is_ancestor(upd.repo, head, target):
        raise UpdateError(tr("{repo} ist von origin/{branch} abgewichen, bitte per SSH klären.", repo=upd.repo, branch=upd.branch))
    commits = _git(upd.repo, "log", "--oneline", "--no-decorate", f"{head}..{target}").splitlines()
    return Pending(head, target, commits)


def start(commit: str) -> str:
    """Update als eigene Unit starten. Gibt eine Fehlermeldung zurück, leer heißt gestartet."""
    p = subprocess.run(
        ["systemd-run", f"--unit={UNIT}", "--collect", "--quiet",
         "--description=tw-update-guard: OTA-Update", str(INSTALLED), "self-update", commit],
        capture_output=True, text=True,
    )
    return (p.stderr or p.stdout).strip() if p.returncode else ""


def started_with(commit: str, bot_commit: str) -> bool:
    """Läuft der Bot mit diesem Commit? (Build-Kennung: gekürzter Hash, evtl. mit Zusatz)"""
    return bool(bot_commit) and commit.startswith(bot_commit.split("+", 1)[0])


def _run(cmd: list[str], cwd: str, timeout: int, env: dict[str, str] | None = None) -> None:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env or _env())
    except subprocess.TimeoutExpired as e:
        raise UpdateError(tr("{what}: keine Antwort nach {n} s", what=cmd[0], n=timeout)) from e
    if p.returncode:
        tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-15:])
        raise UpdateError(tr("{what} endete mit Exit-Code {rc}:\n{tail}", what=" ".join(cmd), rc=p.returncode, tail=tail))


def _systemctl(*args: str) -> int:
    return subprocess.run(["systemctl", *args], check=False).returncode


def _restart_bot() -> None:
    subprocess.run(["systemctl", "restart", "--no-block", BOT_UNIT], check=False)


def self_update(cfg: Config, commit: str) -> int:
    """Läuft als Unit `twguard-update`: einspielen, prüfen, bei Fehlern zurückrollen."""
    set_language(cfg.language)
    repo = cfg.update.repo
    tg = Telegram(cfg.token, cfg.chat_id)
    with state.locked(cfg.state_dir) as st:
        request = dict(st.update_request)

    def report(text: str) -> None:
        with state.locked(cfg.state_dir) as st:
            st.update_request = {}
        # Die Bestätigungsnachricht („Update läuft“) wird zum Ergebnis
        messages.edit_or_send(tg, request, text)

    host = escape(cfg.hostname)
    try:
        if not repo:
            raise UpdateError(tr("[update] repo fehlt in der Konfiguration."))
        old = _git(repo, "rev-parse", "HEAD")
    except UpdateError as e:
        report(tr("❌ <b>{host}</b>: Update abgebrochen.\n<pre>{error}</pre>", host=host, error=escape(clip(str(e), 2500))))
        return 1

    installed = False
    try:
        _git(repo, "merge", "--ff-only", "--quiet", commit)
        # Tests der neuen Fassung; schlagen sie fehl, bleibt die alte installiert
        env = dict(_env(), PYTHONPATH="src")
        _run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], repo, 300, env)
        if INSTALLED.exists():
            shutil.copy2(INSTALLED, PREVIOUS)
        _backup_units(cfg.state_dir / "units.prev")
        installed = True
        _run(["./install.sh"], repo, 300)
        _restart_bot()
    except UpdateError as e:
        log.error("%s", e)
        problems = _rollback(cfg, repo, old, installed)
        report(tr("❌ <b>{host}</b>: Update auf {commit} fehlgeschlagen, {result}\n<pre>{error}</pre>", host=host,
                  commit=escape(commit[:12]), result=_rollback_result(problems), error=escape(clip(str(e), 2500))))
        return 1

    deadline = time.time() + START_TIMEOUT
    while time.time() < deadline:
        time.sleep(3)
        with state.locked(cfg.state_dir) as st:
            if started_with(commit, st.bot_commit):
                return 0  # Erfolg meldet der neue Bot selbst
    log.error(tr("neuer Bot hat sich nicht gemeldet, spiele alte Version zurück"))
    problems = _rollback(cfg, repo, old, installed)
    report(tr("❌ <b>{host}</b>: Der Bot mit {commit} ist nicht gestartet, {result} Bitte journalctl -u twguard-bot ansehen.",
              host=host, commit=escape(commit[:12]), result=_rollback_result(problems)))
    return 1


def _rollback_result(problems: list[str]) -> str:
    if not problems:
        return tr("alte Version wieder eingespielt.")
    return tr("Zurückrollen unvollständig, bitte per SSH nachsehen:\n{problems}\n",
              problems=escape("\n".join(f"• {p}" for p in problems)))


def _backup_units(backup: Path) -> None:
    try:
        shutil.rmtree(backup, ignore_errors=True)
        backup.mkdir(parents=True, mode=0o700)
        for name in UNITS:
            if (UNIT_DIR / name).exists():
                shutil.copy2(UNIT_DIR / name, backup / name)
    except OSError as e:
        raise UpdateError(tr("Units nicht gesichert: {error}", error=e)) from e


def _rollback(cfg: Config, repo: str, old: str, installed: bool) -> list[str]:
    """Alten Stand wiederherstellen; Rückgabe: was dabei nicht geklappt hat."""
    problems = []
    try:
        _git(repo, "reset", "--keep", old)
    except UpdateError as e:
        problems.append(tr("Arbeitskopie nicht zurückgesetzt: {error}", error=e))
    if not installed:
        return problems
    try:
        if PREVIOUS.exists():
            shutil.copy2(PREVIOUS, INSTALLED)
        backup = cfg.state_dir / "units.prev"
        for name in UNITS:
            if (backup / name).exists():
                shutil.copy2(backup / name, UNIT_DIR / name)
    except OSError as e:
        problems.append(tr("Dateien nicht wiederhergestellt: {error}", error=e))
    if _systemctl("daemon-reload"):
        problems.append(tr("systemctl daemon-reload schlug fehl"))
    _restart_bot()
    for problem in problems:
        log.error(tr("Zurückrollen: %s"), problem)
    return problems


def startup_report(cfg: Config, st: state.State, now: float | None = None) -> tuple[str, dict] | None:
    """Beim Start des Bots: Ergebnis eines laufenden Updates (Text, Anfrage) oder None."""
    request = st.update_request
    if not request:
        return None
    host = escape(cfg.hostname)
    if started_with(request.get("commit", ""), COMMIT):
        st.update_request = {}
        return tr("✅ <b>{host}</b>: twguard läuft jetzt mit {commit}.", host=host, commit=escape(COMMIT)), request
    if (now or time.time()) - request.get("at", 0) > STUCK_AFTER:
        st.update_request = {}
        return tr("⚠️ <b>{host}</b>: Vom Update auf {commit} kam keine Rückmeldung. Läuft weiter mit {running}; "
                  "siehe journalctl -u {unit}.", host=host, commit=escape(request.get("commit", "")[:12]),
                  running=escape(COMMIT), unit=UNIT), request
    return None
