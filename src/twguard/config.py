# SPDX-License-Identifier: GPL-3.0-or-later
"""Konfiguration aus /etc/twguard/config.toml."""

from __future__ import annotations

import json
import os
import socket
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .i18n import LANGUAGES, tr
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
    # Sprache der Telegram-Ausgabe (i18n.LANGUAGES)
    language: str = "en"
    state_dir: Path = DEFAULT_STATE_DIR
    rules: Rules = field(default_factory=Rules)
    escalation: Escalation = field(default_factory=Escalation)
    update: Update = field(default_factory=Update)
    briefing: Briefing = field(default_factory=Briefing)

    @property
    def observe(self) -> bool:
        return self.mode != "auto"


def resolve_path(path: str | Path | None = None) -> Path:
    return Path(path or os.environ.get("TWGUARD_CONFIG") or DEFAULT_PATH)


def _defaults() -> dict[str, dict]:
    """Die Vorgaben im Programm, gegliedert wie die Konfigurationsdatei."""
    cfg = Config()
    return {
        "telegram": {"token": cfg.token, "chat_id": cfg.chat_id, "allowed_user_ids": cfg.allowed_user_ids,
                     "help_full": cfg.help_full, "max_errors_per_hour": cfg.max_errors_per_hour},
        "general": {"hostname": cfg.hostname, "dup_args": cfg.dup_args, "report_routine": cfg.report_routine,
                    "mode": cfg.mode, "language": cfg.language, "state_dir": str(cfg.state_dir)},
        "rules": asdict(cfg.rules),
        "escalation": asdict(cfg.escalation),
        "update": asdict(cfg.update),
        "briefing": asdict(cfg.briefing),
    }


def changed_values(path: str | Path | None = None) -> dict[str, dict]:
    """Was in der Datei steht und von den Vorgaben abweicht; alles Übrige kann entfallen."""
    with open(resolve_path(path), "rb") as f:
        data = tomllib.load(f)
    defaults = _defaults()
    changed = {}
    for section, values in data.items():
        known = defaults.get(section, {})
        different = {k: v for k, v in values.items() if k not in known or v != known[k]}
        if different:
            changed[section] = different
    return changed


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def to_toml(sections: dict[str, dict]) -> str:
    blocks = []
    for section, values in sections.items():
        lines = [f"[{section}]", *(f"{key} = {_toml_value(value)}" for key, value in values.items())]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n" if blocks else ""


def load(path: str | Path | None = None) -> Config:
    path = resolve_path(path)
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
            language=general.get("language", "en"),
            state_dir=Path(os.environ.get("TWGUARD_STATE_DIR") or general.get("state_dir") or DEFAULT_STATE_DIR),
            rules=Rules(**data.get("rules", {})),
            escalation=Escalation(**data.get("escalation", {})),
            update=Update(**data.get("update", {})),
            briefing=Briefing(**data.get("briefing", {})),
        )
    except TypeError as e:
        raise SystemExit(tr("{path}: unbekannter Eintrag ({error})", path=path, error=e)) from e
    if cfg.mode not in MODES:
        raise SystemExit(tr("{path}: {key} muss einer von {allowed} sein, nicht {value}", path=path, key="mode",
                            allowed=", ".join(MODES), value=repr(cfg.mode)))
    if cfg.language not in LANGUAGES:
        raise SystemExit(tr("{path}: {key} muss einer von {allowed} sein, nicht {value}", path=path, key="language",
                            allowed=", ".join(LANGUAGES), value=repr(cfg.language)))
    return cfg
