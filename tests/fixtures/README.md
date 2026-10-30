# Testdaten

Echte Ausgaben von zypper (Tumbleweed, Oktober 2026), erzeugt mit `--xmlout -n --no-refresh … --dry-run --details`:

| Datei | Befehl | Inhalt |
|---|---|---|
| `empty-real.xml` | `dup` | System aktuell, nichts zu tun |
| `in-real.xml` | `in postgresql17-server` | Format von `<to-install>` |
| `rm-real.xml` | `rm --clean-deps nano` | Format von `<to-remove>` |
| `conflict-real.xml` | `in postfix exim` (Exit-Code 4) | Solver-Konflikt als `<prompt>` |
| `upgrade-real.xml` | `dup` | Format von `<to-upgrade>` (`edition-old`, `arch-old`), mit Paketen, Muster und Produkt; auf wenige Einträge gekürzt, `packages-to-change` entsprechend angepasst |
