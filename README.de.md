# tw-update-guard (twguard)

🇬🇧 [English version](README.md) · **Installation:** Kurzanleitung in [INSTALL.de.md](INSTALL.de.md)

Ein Dienst für headless openSUSE-Tumbleweed-Server. Er spielt Routine-Updates (`zypper dup`) selbst ein und meldet alles Heikle über Telegram. Er hakt so lange nach, bis der Punkt erledigt ist. Ein „gelesen“ reicht dafür nicht.

## Ablauf

1. **Täglich** (`twguard-check.timer`, 03:30 ± 30 min): `zypper refresh`, dann `zypper dup --dry-run --details` mit XML-Ausgabe.
2. **Einstufung** nach festen Regeln (`[rules]` in der Konfiguration):

   | Fall | Einstufung |
   |---|---|
   | Normale Updates, Kernel, entfernte alte Bibliotheken | Routine: wird eingespielt |
   | Pakete aus `block` (Bootloader, SSH, Netzwerk) | Freigabe nötig |
   | Versionssprung bei Paketen aus `watch` (z. B. systemd 258 → 259) | Freigabe nötig |
   | Paket aus `watch` ohne bekannte Ausgangsversion | Freigabe nötig |
   | Neues oder entferntes Paket aus `watch` (z. B. `postgresql18-server`) | Freigabe nötig |
   | Unerwartete Entfernungen, mehr als `max_removals` Entfernungen | Freigabe nötig |
   | Downgrade, Anbieter- oder Architekturwechsel | Freigabe nötig |
   | Solver-Konflikt | nur per SSH lösbar |

3. **Routine** wird im Modus `auto` mit `zypper dup --no-refresh` eingespielt (gleicher Repo-Stand wie im Probelauf; snapper legt wie immer Snapshots an). Danach kommt eine *stille* Telegram-Nachricht. Ist ein Neustart nötig, wird daraus ein eigener offener Punkt.
4. **Offene Punkte** (Freigabe, Neustart, Fehler) bleiben bestehen, bis der Zustand behoben ist. Der Bot erinnert nach einer Eskalationsleiter:

   | Offen seit | Erinnerung |
   |---|---|
   | sofort | einmal, dann täglich |
   | 2 Tagen | alle 8 Stunden |
   | 5 Tagen | alle 3 Stunden |
   | 10 Tagen | stündlich |

   Nachts (22–7 Uhr) ist Ruhe. Mit `/later` lässt sich zweimal aufschieben, jeweils höchstens 72 Stunden. Vorübergehende Fehler (Repo nicht erreichbar) werden erst nach einem Tag gemeldet. Läuft drei Tage lang keine Prüfung erfolgreich, kommt ebenfalls eine Meldung.

**Beobachtungsmodus** (`mode = "observe"` in `[general]`, Standard nach der Installation): Der Timer prüft und meldet wie sonst, spielt aber nichts ein. Routine-Updates kommen als stille Meldung „würde eingespielt“ (je Stand einmal), mit einer Schaltfläche „Diesmal einspielen“. Freigaben wirken auch in diesem Modus. Erst mit `mode = "auto"` spielt twguard Routine-Updates selbst ein. Jede nicht leere zypper-Ausgabe wird unter `/var/lib/twguard/plans/` gesichert (die letzten 20).

Eine Freigabe gilt nur für genau den gemeldeten Stand (Fingerabdruck der Paketliste mit Ausgangs- und Zielversion, Architektur und Repository). Kommt vorher ein neuer Snapshot, verfällt sie und es wird neu gemeldet. zypper rechnet beim Einspielen neu; weicht der dabei ausgeführte Plan vom geprüften ab (etwa durch ein paralleles `zypper ref`), meldet der Bericht die Unterschiede mit Ton.

**Tägliches Briefing** (`[briefing]`, Standard 08:00): Jeder Server meldet sich einmal am Tag, auch wenn alles in Ordnung ist („☀️ server: alles in Ordnung“, mit letzter Prüfung, Kernel, Laufzeit und im Beobachtungsmodus den ausstehenden Paketen). Solange nichts offen ist, kommt es ohne Ton. Bleibt das Briefing eines Servers aus, ist er aus, ohne Netz oder der Bot läuft nicht.

Wer per SSH selbst `zypper dup` ausführt, braucht nichts weiter zu tun: Der Bot bemerkt die Änderung an der RPM-Datenbank und prüft sofort neu.

## Einrichtung

Schritt für Schritt in [INSTALL.de.md](INSTALL.de.md): Telegram-Bot anlegen, klonen, `./install.sh`, Token und IDs eintragen, Dienste starten. Kurz zu den Bausteinen:

- **Ein Bot pro Server**, alle Bots in einer gemeinsamen Gruppe. Beim @BotFather `/setprivacy` → **Disable**, damit ein einfaches `/status` alle Bots erreicht.
- Optional das Admin-Recht **Nachrichten löschen**: Dann räumen die Bots Befehle und alte Meldungen weg. Telegram macht dabei aus einer Gruppe oft eine **Supergruppe mit neuer ID** (`-100…`); die Bots übernehmen sie und melden sie einmal, danach als `chat_id` eintragen.
- `install.sh` baut `twguard` als einzelnes Python-Archiv (nur Standardbibliothek, Python ≥ 3.11) nach `/usr/local/sbin`, kopiert die systemd-Units und legt eine kurze `/etc/twguard/config.toml` an, falls noch keine da ist (nur die Pflichtwerte und den Pfad der Arbeitskopie unter `[update]`). Einzutragen ist nur, was abweicht: Alles andere kommt aus den Vorgaben im Programm, neue Vorgaben kommen also mit jedem Update. Alle Einstellungen erklärt `/etc/twguard/config.default.toml`, die jedes Update ersetzt. `twguard config-diff` zeigt von einer vorhandenen Konfiguration die Werte, die von den Vorgaben abweichen (auch den Token), damit sie sich kürzen lässt.
- Die Arbeitskopie (z. B. `/root/tw-update-guard`) ist zugleich die Quelle für `/update`. Ein öffentlicher Klon per HTTPS genügt; für ein eigenes privates Repo eignet sich ein Deploy-Schlüssel ohne Schreibzugriff.
- Ohne Git auf dem Server: `build/twguard.pyz` und die Dateien aus `systemd/` hinüberkopieren (dann ohne `/update`).

## Befehle in Telegram

In der Gruppe gelten Befehle für einen bestimmten Server nur, wenn sie **als Antwort auf eine Nachricht dieses Bots** kommen oder mit `@botname` versehen sind. Im privaten Chat mit dem Bot ist das nicht nötig.

| Befehl | Wirkung |
|---|---|
| `/status` | offene Punkte; ohne `@botname` antworten alle Server |
| `/details` | Paketliste des letzten Probelaufs |
| `/approve` | gemeldetes Update freigeben und einspielen |
| `/check` | sofort prüfen; eine Wartemeldung erscheint sofort und wird nach dem Prüflauf durch das Ergebnis ersetzt |
| `/later 12h`, `/later 2d` | Erinnerungen aufschieben (begrenzt) |
| `/reboot` | Neustart in 1 Minute; nach dem Start meldet sich der Server zurück |
| `/cancel` | geplanten Neustart abbrechen |
| `/update` | neuen Stand von twguard holen und zur Bestätigung anzeigen; ohne `@botname` antworten alle Server |
| `/version` | Version, Git-Stand und Modus |
| `/language en`, `/language de` | Sprache zur Laufzeit ändern; ohne `@botname` alle Server. Gilt bis zum nächsten Neustart des Bots, die Konfiguration bleibt unverändert |
| `/help` | alle Befehle; ohne `@botname` antwortet nur der Bot mit `help_full`. Verschwindet nach 5 Minuten |
| `/keyboard` | feste Tastatur einblenden: „📊 Status“, „🔄 Alle prüfen“, „⬆️ Update“, „🏷️ Version“, „❓ Hilfe“ (gelten für alle Server; ein Server versteht eine Taste erst ab der Version, die sie eingeführt hat). Die Tasten hängen an der Nachricht „⌨️ Tastatur …“; die bleibt deshalb stehen. `/keyboard off` löscht sie und blendet die Tasten damit aus |

Alle Befehle stehen außerdem im Menü neben der Eingabezeile.

**Sprache:** `language = "en"` (Standard) oder `"de"` in `[general]` bestimmt die Sprache der Telegram-Ausgabe je Server, einschließlich Schaltflächen, Befehlsmenü und HTML-Datei; danach `systemctl restart twguard-bot`. `/language` ändert sie zur Laufzeit und weist darauf hin; ein Neustart des Bots (auch durch `/update` oder einen Reboot) gilt wieder die Konfiguration, dauerhaft wird eine Änderung also erst durch den Eintrag dort. Die feste Tastatur versteht jeder Server in allen Sprachen, auch wenn sie von einem Bot mit anderer Sprache stammt. Bereits gesendete Meldungen und die Begründungen offener Punkte bleiben in der alten Sprache, bis sie neu erzeugt werden. Auch die Ausgaben von `twguard` auf der Kommandozeile und `--help` folgen dieser Einstellung, sobald die Konfiguration lesbar ist. Das Protokoll (Journal) ebenso.

Die Meldungen haben Schaltflächen für die wichtigsten Befehle (Freigeben, Details, 1 Tag später, Neu starten, Neustart abbrechen, Neu prüfen). Eine Schaltfläche geht nur an den Bot, der die Nachricht geschickt hat; folgenreiche Schaltflächen verschwinden nach dem ersten Druck. „Freigeben“ gilt nur für den Stand, der in dieser Nachricht gemeldet wurde.

## Update von twguard selbst (OTA)

`/update` holt per `git fetch` den Stand von `origin/main` in die Arbeitskopie (`[update] repo`) und zeigt die neuen Commits mit der Schaltfläche „Einspielen“. Danach läuft `twguard self-update` als eigene Unit `twguard-update`:

1. Fast-Forward auf genau den angezeigten Commit (lokale Änderungen oder abweichende Historie: Abbruch).
2. Unit-Tests der neuen Fassung; schlagen sie fehl, bleibt alles beim Alten.
3. Die laufende Version wird als `/usr/local/sbin/twguard.prev` gesichert, `install.sh` baut und installiert die neue und startet den Bot neu.
4. Meldet sich der neue Bot innerhalb von 90 Sekunden, schreibt er „läuft jetzt mit …“. Sonst kommen die alte Version, die alten systemd-Units und der alte Git-Stand zurück, mit einer Meldung (auch, wenn dabei etwas schiefging).

Protokoll: `journalctl -u twguard-update`.

Rückmeldung: Bei Befehlen zeigt Telegram sofort „schreibt …“, bei Schaltflächen kurz „Läuft …“. `/check`, „Jetzt prüfen“ und Freigaben senden sofort eine Wartemeldung, die der Prüflauf später durch sein Ergebnis ersetzt (auch „keine Updates“).

**Aufgeräumter Chat:** Je Server und Thema bleibt höchstens eine Nachricht stehen.
- Befehle werden nach der Antwort gelöscht (Admin-Recht nötig). Hilfe, Version, Details und Bestätigungen löschen sich nach 2 bis 5 Minuten selbst.
- `/status`, Briefing und Erinnerungen ersetzen ihren Vorgänger. Eine Erinnerung kommt also weiterhin neu (mit Ton), die vorige verschwindet.
- Schaltflächen bearbeiten die Nachricht, an der sie hängen: „Freigeben“ macht daraus die Wartemeldung und dann das Ergebnis, „Details“ klappt die Paketliste auf (nach Name, umschaltbar auf „nach Version“ mit gruppierten Paketen, bei langen Listen mit Seiten; „📄 Als Datei“ schickt alle Pakete als HTML-Datei; „◀️ Zurück“ wieder zu), „1 Tag später“ vermerkt die Aufschiebung, „Neu starten“ zeigt den Neustart und nach dem Start „wieder da“.
- Erledigte Punkte bleiben als „✅ … erledigt“ stehen, ohne Schaltflächen.
- Stille Update-Berichte bleiben bis zum nächsten Briefing; das fasst sie zusammen und löscht sie.
- Telegram erlaubt Löschen nur in den ersten 48 Stunden. Ältere Nachrichten kürzt der Bot stattdessen auf eine Zeile.

Befehle werden nur von den Benutzer-IDs in `allowed_user_ids` angenommen, nur aus dem konfigurierten Chat oder dem privaten Chat, und nur, wenn sie höchstens 10 Minuten alt sind. Es gibt keine Shell-Befehle über Telegram.

## Auf der Kommandozeile

```bash
twguard status                      # offene Punkte
twguard check --dry-run             # Einstufung ohne Wirkung
twguard classify datei.xml          # gespeicherte zypper-XML-Ausgabe einstufen
twguard config-diff                 # Werte der Konfiguration, die von den Vorgaben abweichen
twguard self-update                 # auf den neuesten Stand aktualisieren, wie /update
journalctl -u twguard-check -u twguard-bot
journalctl -u twguard-bot | grep -E 'langsam|Verbindung'   # Telegram-Aufrufe über 3 s, mit Dauer je Phase (language = "en": 'slow|connection')
journalctl -u twguard-bot | grep 'Fehler in einer Stunde'   # mehr Abruf- und Sendefehler als max_errors_per_hour (language = "en": 'errors in one hour')
```

## Grenzen

- Ein OTA-Update führt Code aus dem Git-Repo als root aus. Wer auf `main` des Repos pushen darf, aus dem die Server holen, steuert damit die Server. Grüne Tests schützen nicht vor absichtlich schädlichem Code. Wer dem Original-Repo nicht vertrauen will, lässt die Server aus einem eigenen Fork holen und übernimmt Änderungen nach Durchsicht.
- Ist der Server ganz aus oder ohne Netz, schweigt auch der Bot. Dafür braucht es eine Überwachung von außen (z. B. Uptime Kuma).
- Solver-Konflikte lassen sich nicht über Telegram lösen.
- Dienste mit alten Bibliotheken werden nur gemeldet, nicht neu gestartet.

## Sicherheit

Schwachstellen bitte nicht öffentlich melden, sondern wie in [SECURITY.md](SECURITY.md) beschrieben.

## Entstehung

twguard ist im Dialog mit KI-Assistenten entstanden: [Claude Code](https://claude.com/claude-code) (Anthropic) hat den größten Teil des Codes geschrieben, OpenAI Codex hat ihn mehrfach auf Fehler und Sicherheitsprobleme geprüft. Entwurf, Anforderungen, Tests auf echten Servern und alle Entscheidungen: DemoFreak.

## Lizenz

Copyright © 2026 DemoFreak

Dieses Programm ist freie Software: Sie können es unter den Bedingungen der GNU General Public License, Version 3 oder (nach Ihrer Wahl) jeder späteren Version, weitergeben und/oder verändern. Es wird in der Hoffnung verbreitet, dass es nützlich ist, aber **ohne jede Gewährleistung**. Den vollständigen Text enthält [LICENSE](LICENSE). SPDX: `GPL-3.0-or-later`.
