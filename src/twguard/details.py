# SPDX-License-Identifier: GPL-3.0-or-later
"""Paketliste für /details: nach Name oder nach Versionsänderung, auf Seiten oder als HTML-Datei."""

from __future__ import annotations

import re
import time
from html import escape

from .rules import SECTION_TITLES
from .zypper import Plan

VIEWS = ("name", "version")
# Telegram erlaubt 4096 Zeichen; HTML-Auszeichnungen zählen hier mit, das lässt Reserve
PAGE_LIMIT = 3500
# Hält auch sehr große Gruppen unter PAGE_LIMIT, sie gehen dann als weitere Gruppe weiter
GROUP_NAMES = 40
SEPARATORS = ".-:+~_"


def snapshot(plan: Plan) -> dict:
    """Was die Anzeige braucht, als JSON-taugliche Daten für den Zustand."""
    return {
        "summary": f"{plan.packages_to_change} Pakete, Download {plan.download_size / 1e6:.0f} MB",
        "rows": [[section, s.name, s.edition_old, s.edition, s.repository] for section in SECTION_TITLES for s in plan.get(section)],
        "problems": plan.problems,
    }


def _split_change(old: str, new: str) -> tuple[str, str, str]:
    """Gemeinsamer Anfang und die beiden geänderten Enden."""
    common = 0
    for a, b in zip(old, new):
        if a != b:
            break
        common += 1
    # Nicht mitten in einer Zahl trennen (20 → 21 statt 2|0 → 2|1)
    while common and old[common - 1] not in SEPARATORS:
        common -= 1
    return new[:common], old[common:], new[common:]


def version_change(old: str, new: str) -> str:
    """Gemeinsamer Anfang einmal, der geänderte Teil fett: 1.23.<b>20</b> → <b>21</b>."""
    if not old or old == new:
        return escape(new)
    same, before, after = _split_change(old, new)
    return f"{escape(same)}<b>{escape(before)}</b> → <b>{escape(after)}</b>"


def _is_rebuild(old: str, new: str) -> bool:
    return bool(old) and old.rsplit("-", 1)[0] == new.rsplit("-", 1)[0]


def _by_name(rows: list[list[str]]) -> list[str]:
    return [f"{escape(name)}\n  {version_change(old, new)}" for name, old, new in sorted(rows)]


def _by_version(rows: list[list[str]]) -> list[str]:
    groups: dict[tuple[str, str], list[str]] = {}
    for name, old, new in sorted(rows):
        groups.setdefault((old, new), []).append(name)
    # Echte Versionssprünge zuerst, reine Neubauten danach
    ordered = sorted(groups.items(), key=lambda g: (_is_rebuild(*g[0]), g[1][0]))
    blocks, labelled = [], False
    for (old, new), names in ordered:
        for start in range(0, len(names), GROUP_NAMES):
            part = ", ".join(escape(n) for n in names[start:start + GROUP_NAMES])
            label = ""
            if _is_rebuild(old, new) and not labelled:
                label, labelled = "<i>nur neu gebaut:</i>\n", True
            blocks.append(f"{label}{version_change(old, new)}\n  {part}")
    return blocks


def pages(plan: dict, view: str) -> list[str]:
    """Die Liste als Seiten; Blöcke werden nie getrennt, die Überschrift steht auf jeder Seite neu."""
    build = _by_version if view == "version" else _by_name
    result, current, heading = [], plan.get("summary", ""), ""
    rows = plan.get("rows", [])
    for section, title in SECTION_TITLES.items():
        items = [row[1:4] for row in rows if row[0] == section]
        if not items:
            continue
        heading = f"<b>{escape(title)} ({len(items)})</b>"
        current += f"\n\n{heading}"
        for block in build(items):
            if len(current) + len(block) + 2 > PAGE_LIMIT:
                # Keine Überschrift allein am Seitenende
                result.append(current.removesuffix(f"\n\n{heading}"))
                current = heading
            current += f"\n{block}" if view == "name" else f"\n\n{block}"
    for problem in plan.get("problems", []):
        block = escape(problem[:1000])
        if len(current) + len(block) + 2 > PAGE_LIMIT:
            result.append(current)
            current = ""
        current += f"\n\n{block}"
    result.append(current.strip())
    return result


FILE_STYLE = """
:root { color-scheme: light dark; --bg: #fff; --fg: #1a1a1a; --muted: #6b6b6b; --line: #ddd; --old: #c0262d;
        --new: #12813a; --jump: #fff3d6; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #17191c; --fg: #e8e8e8; --muted: #9a9a9a; --line: #33363a; --old: #ff7b72; --new: #56d364;
          --jump: #3a2f12; }
}
body { margin: 0; padding: 12px 16px 32px; background: var(--bg); color: var(--fg);
       font: 15px/1.35 system-ui, sans-serif; }
h1 { font-size: 19px; margin: 0; }
h2 { font-size: 16px; margin: 22px 0 4px; }
p { margin: 4px 0; color: var(--muted); font-size: 13px; }
.row { padding: 6px 4px; border-bottom: 1px solid var(--line); overflow-wrap: anywhere; }
.jump { background: var(--jump); }
.name { font-weight: 600; }
.repo { color: var(--muted); font-size: 12px; margin-left: 6px; }
.ver { font: 13px/1.4 ui-monospace, Menlo, Consolas, monospace; }
.ver span { display: inline-block; }
.old { color: var(--old); }
.new { color: var(--new); font-weight: 700; }
.rebuild .old, .rebuild .new { color: var(--muted); font-weight: 400; }
"""


def _file_row(name: str, old: str, new: str, repo: str) -> str:
    kind = "rebuild" if not old or _is_rebuild(old, new) else "jump"
    if old and old != new:
        same, before, after = _split_change(old, new)
        version = (f'<span>{escape(same)}<b class="old">{escape(before)}</b></span> → '
                   f'<span>{escape(same)}<b class="new">{escape(after)}</b></span>')
    else:
        version = f"<span>{escape(new)}</span>"
    source = f'<span class="repo">{escape(repo)}</span>' if repo else ""
    return (f'<div class="row {kind}"><div><span class="name">{escape(name)}</span>{source}</div>'
            f'<div class="ver">{version}</div></div>')


def file_html(plan: dict, host: str, at: float) -> str:
    """Eigenständige Seite im Stil von `zypper list-updates`: alle Pakete, ohne Skripte und Fremdinhalte."""
    rows = plan.get("rows", [])
    jumps = sum(1 for r in rows if r[2] and r[2] != r[3] and not _is_rebuild(r[2], r[3]))
    parts = [f"<h1>{escape(host)}</h1><p>{escape(plan.get('summary', ''))}, davon {jumps} mit neuer Version; "
             f"Stand {time.strftime('%d.%m.%Y %H:%M', time.localtime(at))}</p>"
             "<p>Hervorgehoben: neue Version. Grau: nur neu gebaut.</p>"]
    for section, title in SECTION_TITLES.items():
        items = sorted(r[1:] for r in rows if r[0] == section)
        if items:
            parts.append(f"<h2>{escape(title)} ({len(items)})</h2>")
            parts += [_file_row(*item) for item in items]
    parts += [f"<p>{escape(problem)}</p>" for problem in plan.get("problems", [])]
    return ('<!doctype html><html lang="de"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>Pakete {escape(host)}</title><style>{FILE_STYLE}</style></head><body>"
            + "\n".join(parts) + "</body></html>\n")


def file_name(host: str, at: float) -> str:
    safe = re.sub(r"[^A-Za-z0-9.-]+", "_", host)
    return f"pakete-{safe}-{time.strftime('%Y%m%d-%H%M', time.localtime(at))}.html"
