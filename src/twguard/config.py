# SPDX-License-Identifier: GPL-3.0-or-later
"""Konfiguration aus /etc/twguard/config.toml."""

from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .rules import Rules

DEFAULT_PATH = Path("/etc/twguard/config.toml")
DEFAULT_STATE_DIR = Path("/var/lib/twguard")


@dataclass
class Escalation:
    # Keine Erinnerungen in diesem Zeitraum (Ortszeit); leer = immer
    quiet_hours: str = "22:00-07:00"
    # Stufen [ab Alter in Stunden, Abstand in Stunden]
    ladder: list[list[float]] = field(default_factory=lambda: [[0, 24], [48, 8], [120, 3], [240, 1]])
    # Für vorübergehende Fehler (Repo nicht erreichbar): erst nach einem Tag melden
    ladder_error: list[list[float]] = field(default_factory=lambda: [[24, 24], [72, 6]])
    max_snoozes: int = 2
    max_snooze_hours: int = 72
    stale_after_hours: int = 72


@dataclass
class Update:
    # Git-Arbeitskopie auf dem Server, aus der /update den neuen Stand holt; leer = abgeschaltet
    repo: str = ""
    branch: str = "main"


@dataclass
class Briefing:
    # Uhrzeit (Ortszeit, bis 18:00) der täglichen Zusammenfassung; leer = keine
    at: str = "08:00"
    # Ohne Ton senden, solange nichts offen ist
    silent: bool = True


MODES = ("observe", "auto")


@dataclass
class Config:
    token: str = ""
    chat_id: int = 0
    allowed_user_ids: list[int] = field(default_factory=list)
    # /help an alle: nur dieser Bot schickt die ganze Befehlsliste, die übrigen eine Zeile
    help_full: bool = False
    # Warnung im Journal, wenn mehr Abruf- und Sendefehler je Stunde auftreten; 0 = nie
    max_errors_per_hour: int = 10
    hostname: str = field(default_factory=socket.gethostname)
    dup_args: list[str] = field(default_factory=list)
    report_routine: bool = True
    # observe: nur prüfen und melden; auto: Routine-Updates einspielen
    mode: str = "observe"
    state_dir: Path = DEFAULT_STATE_DIR
    rules: Rules = field(default_factory=Rules)
    escalation: Escalation = field(default_factory=Escalation)
    update: Update = field(default_factory=Update)
    briefing: Briefing = field(default_factory=Briefing)

    @property
    def observe(self) -> bool:
        return self.mode != "auto"


def load(path: str | Path | None = None) -> Config:
    path = Path(path or os.environ.get("TWGUARD_CONFIG") or DEFAULT_PATH)
    with open(path, "rb") as f:
        data = tomllib.load(f)
    tg = data.get("telegram", {})
    general = data.get("general", {})
    try:
        cfg = Config(
            token=tg.get("token", ""),
            chat_id=int(tg.get("chat_id", 0)),
            allowed_user_ids=[int(x) for x in tg.get("allowed_user_ids", [])],
            help_full=bool(tg.get("help_full", False)),
            max_errors_per_hour=int(tg.get("max_errors_per_hour", Config.max_errors_per_hour)),
            hostname=general.get("hostname") or socket.gethostname(),
            dup_args=list(general.get("dup_args", [])),
            report_routine=bool(general.get("report_routine", True)),
            mode=general.get("mode", "observe"),
            state_dir=Path(os.environ.get("TWGUARD_STATE_DIR") or general.get("state_dir") or DEFAULT_STATE_DIR),
            rules=Rules(**data.get("rules", {})),
            escalation=Escalation(**data.get("escalation", {})),
            update=Update(**data.get("update", {})),
            briefing=Briefing(**data.get("briefing", {})),
        )
    except TypeError as e:
        raise SystemExit(f"{path}: unbekannter Eintrag ({e})") from e
    if cfg.mode not in MODES:
        raise SystemExit(f"{path}: mode muss einer von {', '.join(MODES)} sein, nicht {cfg.mode!r}")
    return cfg
