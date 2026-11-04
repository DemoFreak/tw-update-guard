# SPDX-License-Identifier: GPL-3.0-or-later
"""Sprache der Telegram-Ausgabe: Der deutsche Text im Code ist zugleich der Schlüssel der Tabellen.

Fehlt eine Übersetzung, erscheint der deutsche Text. Ein Test prüft, dass keine fehlt. Eine weitere
Sprache ist nur eine weitere Tabelle in TRANSLATIONS.
"""

from __future__ import annotations

EN = {
    # Zeitangaben
    "%d.%m. %H:%M": "%b %d, %H:%M",
    "%d.%m.%Y %H:%M": "%b %d %Y, %H:%M",
    "weniger als einer Stunde": "less than an hour",
    "einer Stunde": "an hour",
    "{n} Stunden": "{n} hours",
    "{n} Tagen": "{n} days",
    # Offene Punkte
    "Update braucht Freigabe": "Update needs approval",
    "Neustart ausstehend": "Reboot pending",
    "Update-Prüfung schlägt fehl": "Update check is failing",
    "Update ist fehlgeschlagen": "Update failed",
    "Keine Update-Prüfung mehr": "No more update checks",
    "… und {n} weitere (/details)": "… and {n} more (/details)",
    "Bitte per SSH lösen (zypper dup). „Details“ zeigt den Konflikt.":
        "Please resolve via SSH (zypper dup). “Details” shows the conflict.",
    "{title}, seit {age}": "{title}, for {age}",
    # Schaltflächen
    "⏰ 1 Tag später": "⏰ 1 day later",
    "✅ Freigeben": "✅ Approve",
    "🔁 Neu starten": "🔁 Reboot",
    "▶️ Diesmal einspielen": "▶️ Install this time",
    "🔄 Jetzt prüfen": "🔄 Check now",
    "◀️ Zurück": "◀️ Back",
    "🔢 Nach Version": "🔢 By version",
    "🔤 Nach Name": "🔤 By name",
    "📄 Als Datei": "📄 As a file",
    "✖️ Neustart abbrechen": "✖️ Cancel reboot",
    "⬆️ Einspielen": "⬆️ Install",
    # Feste Tastatur
    "🔄 Alle prüfen": "🔄 Check all",
    "❓ Hilfe": "❓ Help",
    # Briefing, Berichte
    "{n} offener Punkt": "{n} open issue",
    "{n} offene Punkte": "{n} open issues",
    "alles in Ordnung": "all good",
    "Beobachtungsmodus": "observation mode",
    "{n} Paket aktualisiert": "{n} package updated",
    "{n} Pakete aktualisiert": "{n} packages updated",
    " (freigegeben)": " (approved)",
    "⚠️ Den beim Einspielen ausgeführten Plan konnte twguard nicht lesen; ob genau der "
    "geprüfte eingespielt wurde, ist unbekannt. Bitte journalctl -u twguard-check ansehen.":
        "⚠️ twguard could not read the plan zypper executed; whether exactly the checked plan "
        "was installed is unknown. Please see journalctl -u twguard-check.",
    "… und {n} weitere": "… and {n} more",
    "⚠️ Eingespielt wurde nicht genau der geprüfte Plan (zwischendurch geändert?). Bitte ansehen:\n<pre>{diff}</pre>":
        "⚠️ What was installed is not exactly the checked plan (changed in between?). Please review:\n<pre>{diff}</pre>",
    "Dienste nutzen alte Dateien": "Services using old files",
    "{what} nicht ausführbar: {error}": "{what} cannot be run: {error}",
    "Der Plan hat sich geändert, nichts freigegeben. Bitte die aktuelle Meldung prüfen.": "The plan has changed, nothing approved. Please check the current message.",
    "✅ Behoben": "✅ Fixed",
    "läuft, aber zypper ps gab keine Auskunft über alte Dateien: {services}": "running, but zypper ps gave no information about old files: {services}",
    "Neustart abgewiesen: {services}": "restart rejected: {services}",
    "zypper ps gab keine Auskunft, nichts neu gestartet.": "zypper ps gave no information, nothing restarted.",
    "Die Liste hat sich geändert, nichts neu gestartet.": "The list has changed, nothing restarted.",
    "Diese Dienste nutzen noch alte Dateien:\n{services}": "These services still use old files:\n{services}",
    "Nicht angeboten (Ausschlussliste): {services}": "Not offered (exclude list): {services}",
    "🔁 Alle neu starten ({n})": "🔁 Restart all ({n})",
    "neu gestartet: {services}": "restarted: {services}",
    "{command} (PID {pid}, {user})": "{command} (PID {pid}, {user})",
    "{n} weitere": "{n} more",
    "Dienst {service} neu gestartet. Prozess {procs} nutzt bis zu seinem Ende weiter gelöschte Dateien.":
        "Service {service} restarted. Process {procs} keeps using deleted files until it ends.",
    "Dienst {service} neu gestartet. Prozesse {procs} nutzen bis zu ihrem Ende weiter gelöschte Dateien.":
        "Service {service} restarted. Processes {procs} keep using deleted files until they end.",
    "nutzt weiter alte Dateien: {services}": "still using old files: {services}",
    "nach dem Neustart nicht aktiv: {services}": "not active after the restart: {services}",
    "Ergebnis des Neustarts unbekannt, systemd gab keine Auskunft: {services}":
        "restart result unknown, systemd gave no answer: {services}",
    "aktiv, aber nicht nachweislich {seconds} s am Stück (Neustartschleife oder Startzeit unbekannt), "
    "Ergebnis nicht bestätigt: {services}":
        "active, but not shown to have run {seconds} s in a row (restart loop or start time unknown), "
        "result not confirmed: {services}",
    "Neustart nach {minutes} Minuten nicht abgeschlossen, systemd arbeitet noch daran: {services}. "
    "Das Ergebnis folgt.":
        "restart not finished after {minutes} minutes, systemd is still working on it: {services}. "
        "The result will follow.",
    "Neustart läuft …": "Restarting …",
    "Exit-Code {rc}": "exit code {rc}",
    "Danach wäre ein Neustart nötig.": "A reboot would be required afterwards.",
    "Die Gruppe ist jetzt eine Supergruppe mit neuer ID <code>{new}</code>. twguard nutzt sie ab sofort; "
    "bitte in /etc/twguard/config.toml <code>chat_id = {new}</code> eintragen.":
        "The group is now a supergroup with the new ID <code>{new}</code>. twguard uses it from now on; "
        "please set <code>chat_id = {new}</code> in /etc/twguard/config.toml.",
    # Prüflauf
    "Repos lassen sich nicht auffrischen:": "Repositories cannot be refreshed:",
    "zypper dup --dry-run schlägt fehl:": "zypper dup --dry-run fails:",
    "Prüfung fertig, keine Updates.": "Check finished, no updates.",
    "Die Freigabe galt einem älteren Stand und ist verfallen.": "The approval was for an older state and has expired.",
    "(Unterschied nicht darstellbar, siehe Journal)": "(difference cannot be shown, see the journal)",
    "zypper dup endete mit Exit-Code {rc}:\n{tail}": "zypper dup ended with exit code {rc}:\n{tail}",
    "Dienste werden neu gestartet, das Einspielen wartet": "services are being restarted, installation is waiting",
    "Neustart von Diensten läuft noch, nichts eingespielt": "service restart still running, nothing installed",
    "Paketverwaltung belegt durch %s, warte": "package management locked by %s, waiting",
    "Gerade nicht möglich: {reason}. Bitte danach erneut.": "Not possible right now: {reason}. Please try again afterwards.",
    "Nichts eingespielt: {reason}. Der nächste Prüflauf versucht es erneut.":
        "Nothing installed: {reason}. The next check will try again.",
    "nichts eingespielt: %s": "nothing installed: %s",
    "Neustart wird gerade geplant": "a reboot is being scheduled",
    "Neustart geplant um {when}": "reboot scheduled for {when}",
    "Neustart läuft": "reboot in progress",
    "Herunterfahren geplant um {when}": "shutdown scheduled for {when}",
    "Herunterfahren läuft": "shutdown in progress",
    "logind gibt keine Auskunft, ob ein Neustart geplant ist": "logind does not say whether a reboot is scheduled",
    "logind nicht erreichbar: %s": "logind not reachable: %s",
    "logind: unerwartete Antwort: %s": "logind: unexpected answer: %s",
    "Paketverwaltung belegt durch {holder}": "package management locked by {holder}",
    "twguard spielt gerade ein": "twguard is installing",
    "Dienste werden gerade neu gestartet": "services are being restarted",
    "ein Update von twguard läuft": "a twguard update is running",
    "ein Prüflauf läuft": "a check is running",
    "✖️ Herunterfahren abbrechen": "✖️ Cancel shutdown",
    "Seit dem Boot noch nicht abgefragt: {what} (zypper gibt noch keine Auskunft)":
        "Not yet queried since boot: {what} (zypper does not answer yet)",
    "Neustartbedarf": "pending reboot",
    "Dienste mit alten Dateien": "services using old files",
    "Der Neustart von {services} wurde durch den Neustart des Servers unterbrochen; das Ergebnis ist unbekannt.":
        "Restarting {services} was interrupted by the server reboot; the result is unknown.",
    "Paketverwaltung seit %d min belegt durch %s, nichts eingespielt":
        "package management locked for %d min by %s, nothing installed",
    "Paketverwaltung belegt: {holder}": "Package management locked: {holder}",
    "zypper needs-rebooting: Paketverwaltung belegt durch %s": "zypper needs-rebooting: package management locked by %s",
    "zypper ps: Paketverwaltung belegt durch %s": "zypper ps: package management locked by %s",
    "ein anderes Programm": "another program",
    "🔒 Paketverwaltung belegt: {holder}. Prüfen und Einspielen warten darauf.":
        "🔒 Package management locked: {holder}. Checks and installs wait for it.",
    "Ein Update wird gerade eingespielt und läuft noch zu Ende.":
        "An update is being installed right now and will run to completion.",
    "Nach dem Update ist ein Neustart nötig (Kernel oder Systembibliotheken).":
        "A reboot is required after the update (kernel or system libraries).",
    # zypper
    "keine Antwort nach {n} s, abgebrochen": "no response after {n} s, aborted",
    "zypper-Ausgabe nicht lesbar: {error}": "zypper output not readable: {error}",
    "zypper refresh: Exit-Code {rc}": "zypper refresh: exit code {rc}",
    "zypper dup --dry-run: Exit-Code {rc} {stderr}": "zypper dup --dry-run: exit code {rc} {stderr}",
    # Einstufung
    "Solver-Konflikt, nur per SSH lösbar:": "Solver conflict, can only be resolved via SSH:",
    "Downgrade": "Downgrade",
    "Architekturwechsel": "Architecture change",
    "Anbieterwechsel": "Vendor change",
    "kritisches Paket: {package}": "critical package: {package}",
    "kritisches Paket wird entfernt: {package}": "critical package will be removed: {package}",
    "Ausgangsversion unbekannt: {package}": "previous version unknown: {package}",
    "neuer Kernel-Zweig: {package} (läuft: {running})": "new kernel branch: {package} (running: {running})",
    "Versionssprung: {package}": "version jump: {package}",
    "neues Paket: {package}": "new package: {package}",
    "wird entfernt: {package}": "will be removed: {package}",
    "überwachtes Paket wird entfernt: {package}": "watched package will be removed: {package}",
    "{n} Pakete werden entfernt (Grenze {limit})": "{n} packages will be removed (limit {limit})",
    "{n} Pakete (Grenze {limit})": "{n} packages (limit {limit})",
    "{n} Paket, Download {mb} MB": "{n} package, download {mb} MB",
    "{n} Pakete, Download {mb} MB": "{n} packages, download {mb} MB",
    "Neu": "New",
    "Aktualisiert": "Updated",
    "Neu installiert": "Reinstalled",
    "Entfernt": "Removed",
    # Paketliste
    "<i>nur neu gebaut:</i>": "<i>rebuilds only:</i>",
    "{summary}, davon {jumps} mit neuer Version; Stand {when}": "{summary}, {jumps} with a new version; as of {when}",
    "Hervorgehoben: neue Version. Grau: nur neu gebaut.": "Highlighted: new version. Gray: rebuild only.",
    "Pakete {host}": "Packages {host}",
    "pakete": "packages",
    "Noch kein Probelauf.": "No dry run yet.",
    "Keine Paketliste vorhanden.": "No package list available.",
    "Datei konnte nicht gesendet werden.": "The file could not be sent.",
    # Bot
    "Offene Punkte (alle Server)": "Open issues (all servers)",
    "Sofort prüfen": "Check now",
    "Paketliste des letzten Probelaufs": "Package list of the last dry run",
    "Gemeldetes Update freigeben": "Approve the reported update",
    "Erinnerungen aufschieben, z. B. 12h": "Snooze reminders, e.g. 12h",
    "Neustart in 1 Minute": "Reboot in 1 minute",
    "Geplanten Neustart abbrechen": "Cancel a scheduled reboot",
    "Neuen Stand von twguard holen": "Fetch the new twguard version",
    "Version und Modus": "Version and mode",
    "Hilfe": "Help",
    "Sprache bis zum Neustart ändern": "Change the language until restart",
    "🌐 <b>{host}</b>: Sprache {language}, in der Konfiguration {configured}. Ändern: /language en oder /language de":
        "🌐 <b>{host}</b>: language {language}, in the configuration {configured}. To change: /language en or /language de",
    "Format: /language en oder /language de": "Format: /language en or /language de",
    "🌐 <b>{host}</b>: Sprache {language}, wie in der Konfiguration.": "🌐 <b>{host}</b>: language {language}, as in the configuration.",
    "🌐 <b>{host}</b>: Sprache {language} bis zum nächsten Neustart des Bots. Die Konfiguration enthält {configured}. "
    "Damit es dauerhaft gilt, in /etc/twguard/config.toml unter [general] <code>language = \"{language}\"</code> eintragen.":
        "🌐 <b>{host}</b>: language {language} until the next restart of the bot. The configuration says {configured}. "
        "To make it permanent, set <code>language = \"{language}\"</code> under [general] in /etc/twguard/config.toml.",
    "Modus bis zum Neustart ändern": "Change the mode until restart",
    "Modus {mode} bis zum nächsten Neustart des Bots, in der Konfiguration {configured}.":
        "Mode {mode} until the next restart of the bot, in the configuration {configured}.",
    "(bis zum Neustart)": "(until restart)",
    "/mode gilt nur gezielt: mit @botname oder als Antwort auf eine Nachricht des Servers.":
        "/mode only works when addressed: with @botname or as a reply to a message from the server.",
    "⚙️ <b>{host}</b>: Modus {mode}, in der Konfiguration {configured}. Ändern: /mode auto oder /mode observe":
        "⚙️ <b>{host}</b>: mode {mode}, in the configuration {configured}. To change: /mode auto or /mode observe",
    "Format: /mode auto oder /mode observe": "Format: /mode auto or /mode observe",
    "⚙️ <b>{host}</b>: Modus {mode}, wie in der Konfiguration.": "⚙️ <b>{host}</b>: mode {mode}, as in the configuration.",
    "⚙️ <b>{host}</b>: Modus {mode} bis zum nächsten Neustart des Bots (auch durch /update oder einen Reboot). "
    "Die Konfiguration enthält {configured}. Damit es dauerhaft gilt, in /etc/twguard/config.toml unter [general] "
    "<code>mode = \"{mode}\"</code> eintragen.":
        "⚙️ <b>{host}</b>: mode {mode} until the next restart of the bot (also through /update or a reboot). "
        "The configuration says {configured}. To make it permanent, set <code>mode = \"{mode}\"</code> under "
        "[general] in /etc/twguard/config.toml.",
    "Routine-Updates spielt der nächste Prüflauf ein, sofort: /check":
        "The next check installs routine updates; right now: /check",
    "🟢 <b>{host}</b> ist wieder da (Kernel {kernel}).": "🟢 <b>{host}</b> is back (kernel {kernel}).",
    "Feste Tastatur einblenden": "Show the permanent keyboard",
    "Seit {age} keine erfolgreiche Prüfung. Läuft twguard-check.timer?":
        "No successful check for {age}. Is twguard-check.timer running?",
    "zypper needs-rebooting meldet einen ausstehenden Neustart.": "zypper needs-rebooting reports a pending reboot.",
    "Unbekannter Befehl. /help zeigt die Befehle.": "Unknown command. /help shows the commands.",
    "Es liegt kein Update zur Freigabe vor.": "There is no update to approve.",
    "Das lässt sich nicht per Telegram freigeben, bitte per SSH lösen.":
        "This cannot be approved via Telegram, please resolve it via SSH.",
    "Diese Meldung gilt einem älteren Stand. /status zeigt den aktuellen.":
        "This message refers to an older state. /status shows the current one.",
    "Format: /later 12h oder /later 2d": "Format: /later 12h or /later 2d",
    "Es ist nichts offen.": "Nothing is open.",
    "Aufgeschoben um {hours} Stunden: {titles}": "Snoozed for {hours} hours: {titles}",
    "Nicht weiter aufschiebbar: {titles}": "Cannot be snoozed any further: {titles}",
    "Neustart fehlgeschlagen: {error}": "Reboot failed: {error}",
    "Abbrechen fehlgeschlagen: {error}": "Cancelling failed: {error}",
    "<b>{host}</b>: twguard ist aktuell ({commit}).": "<b>{host}</b>: twguard is up to date ({commit}).",
    "Inzwischen gibt es einen anderen Stand. Bitte /update erneut.": "There is a different version by now. Please run /update again.",
    "<b>{host}</b>: twguard ist schon aktuell.": "<b>{host}</b>: twguard is already up to date.",
    "Ein Update läuft bereits.": "An update is already running.",
    "Update nicht gestartet: {error}": "Update not started: {error}",
    "Beobachtung": "observation",
    "automatisch": "automatic",
    "Die Tastatur-Nachricht ist älter als 48 Stunden; bitte von Hand löschen.":
        "The keyboard message is older than 48 hours; please delete it by hand.",
    "Dieser Bot hat keine Tastatur eingeblendet.": "This bot has not shown a keyboard.",
    "⌨️ Tastatur für alle Server. Diese Nachricht nicht löschen, sonst verschwinden die Tasten (/keyboard off blendet sie aus).":
        "⌨️ Keyboard for all servers. Do not delete this message or the keys disappear (/keyboard off hides them).",
    "Nicht berechtigt.": "Not authorized.",
    "Unbekannte Schaltfläche.": "Unknown button.",
    "Freigegeben": "Approved",
    "Prüfung läuft …": "Check running …",
    "Update läuft …": "Update running …",
    "✖️ verworfen": "✖️ dismissed",
    # Update über das Netz
    "{what}: keine Antwort nach {n} s": "{what}: no response after {n} s",
    "{what} endete mit Exit-Code {rc}:\n{tail}": "{what} ended with exit code {rc}:\n{tail}",
    "OTA ist nicht eingerichtet: [update] repo fehlt in der Konfiguration.":
        "OTA is not set up: [update] repo is missing from the configuration.",
    "{repo} hat lokale Änderungen, bitte per SSH klären.": "{repo} has local changes, please resolve via SSH.",
    "{repo} ist von origin/{branch} abgewichen, bitte per SSH klären.":
        "{repo} has diverged from origin/{branch}, please resolve via SSH.",
    "[update] repo fehlt in der Konfiguration.": "[update] repo is missing from the configuration.",
    "❌ <b>{host}</b>: Update abgebrochen.\n<pre>{error}</pre>": "❌ <b>{host}</b>: update aborted.\n<pre>{error}</pre>",
    "❌ <b>{host}</b>: Update auf {commit} fehlgeschlagen, {result}\n<pre>{error}</pre>":
        "❌ <b>{host}</b>: update to {commit} failed, {result}\n<pre>{error}</pre>",
    "❌ <b>{host}</b>: Der Bot mit {commit} ist nicht gestartet, {result} Bitte journalctl -u twguard-bot ansehen.":
        "❌ <b>{host}</b>: the bot with {commit} did not start, {result} Please see journalctl -u twguard-bot.",
    "alte Version wieder eingespielt.": "the old version was restored.",
    "Zurückrollen unvollständig, bitte per SSH nachsehen:\n{problems}\n": "rollback incomplete, please check via SSH:\n{problems}\n",
    "Units nicht gesichert: {error}": "Units not backed up: {error}",
    "Arbeitskopie nicht zurückgesetzt: {error}": "Working copy not reset: {error}",
    "Dateien nicht wiederhergestellt: {error}": "Files not restored: {error}",
    "systemctl daemon-reload schlug fehl": "systemctl daemon-reload failed",
    "✅ <b>{host}</b>: twguard läuft jetzt mit {commit}.": "✅ <b>{host}</b>: twguard is now running {commit}.",
    "⚠️ <b>{host}</b>: Vom Update auf {commit} kam keine Rückmeldung. Läuft weiter mit {running}; siehe journalctl -u {unit}.":
        "⚠️ <b>{host}</b>: no response from the update to {commit}. Still running {running}; see journalctl -u {unit}.",
    "👋 Testnachricht von <b>{host}</b>": "👋 Test message from <b>{host}</b>",
    # Protokoll
    "angemeldet als @%s": "logged in as @%s",
    "veralteten Befehl ignoriert: %s": "ignored outdated command: %s",
    "Befehl von %s in Chat %s ignoriert (nicht freigegeben)": "command from %s in chat %s ignored (not allowed)",
    "Schaltfläche von %s in Chat %s ignoriert (nicht freigegeben)": "button from %s in chat %s ignored (not allowed)",
    "Befehlsmenü nicht gesetzt: %s": "command menu not set: %s",
    "Befehl fehlgeschlagen": "command failed",
    "Falls die Gruppe zur Supergruppe wurde: chat_id = %s in config.toml eintragen (bisher %s)":
        "If the group became a supergroup: set chat_id = %s in config.toml (currently %s)",
    "Datei nicht gesendet: %s": "file not sent: %s",
    "Antwort nicht gesendet: %s": "answer not sent: %s",
    "Nachricht nicht gesendet: %s": "message not sent: %s",
    "{address} nach {seconds} s: {error}": "{address} after {seconds} s: {error}",
    "antwort": "response",
    "Tippanzeige nicht gesendet: %s": "typing indicator not sent: %s",
    "Schaltfläche nicht bestätigt: %s": "button not acknowledged: %s",
    "Telegram nicht erreichbar: %s": "Telegram not reachable: %s",
    "Befehle werden nicht gelöscht (Bot braucht das Admin-Recht „Nachrichten löschen“): %s":
        "commands are not deleted (the bot needs the admin right “Delete messages”): %s",
    "spiele %d Pakete ein (%s)": "installing %d packages (%s)",
    "freigegeben": "approved",
    "zypper dup: Exit-Code %s\n%s": "zypper dup: exit code %s\n%s",
    "nichts zu tun": "nothing to do",
    "Freigabe nötig: %s": "approval required: %s",
    "Beobachtungsmodus: würde %d Pakete einspielen": "observation mode: would install %d packages",
    "Plan des Einspielens nicht lesbar, Abgleich mit dem Probelauf entfällt":
        "plan of the installation not readable, comparison with the dry run skipped",
    "eingespielter Plan weicht vom geprüften ab:\n%s": "installed plan differs from the checked one:\n%s",
    "chat_id %s in der Konfiguration ist veraltet, nutze %s (bitte eintragen)":
        "chat_id %s in the configuration is outdated, using %s (please update it)",
    "Nachricht %s nicht bearbeitet: %s": "message %s not edited: %s",
    "Nachricht %s nicht gelöscht: %s": "message %s not deleted: %s",
    "Gruppe %s ist jetzt Supergruppe %s; bitte chat_id in config.toml anpassen":
        "group %s is now supergroup %s; please update chat_id in config.toml",
    "Hinweis zur neuen Gruppen-ID nicht gesendet: %s": "notice about the new group ID not sent: %s",
    "neuer Bot hat sich nicht gemeldet, spiele alte Version zurück": "new bot did not report, restoring the old version",
    "Zurückrollen: %s": "rollback: %s",
    "Telegram: mehr als %d Fehler in einer Stunde (Abruf %d, Senden %d), zuletzt: %s":
        "Telegram: more than %d errors in one hour (fetch %d, send %d), last: %s",
    "Verbindung zu %s über %s, künftig zuerst; vorher: %s": "connection to %s via %s, tried first from now on; before: %s",
    "langsam: %s brauchte %.1f s (%s)": "slow: %s took %.1f s (%s)",
    "keine Adresse erreichbar: {failed}": "no address reachable: {failed}",
    "über {address}": "via {address}",
    "starte: %s": "starting: %s",
    "zypper needs-rebooting: Exit-Code %s": "zypper needs-rebooting: exit code %s",
    "zypper ps: %s": "zypper ps: %s",
    "zypper ps: unerwartete Zeile: %s": "zypper ps: unexpected line: %s",
    "%s: keine Antwort nach %d s, abgebrochen": "%s: no response after %d s, aborted",
    # Kommandozeile
    "Keine Konfiguration gefunden ({path})": "No configuration found ({path})",
    "{path}: unbekannter Eintrag ({error})": "{path}: unknown entry ({error})",
    "{path}: {key} muss einer von {allowed} sein, nicht {value}": "{path}: {key} must be one of {allowed}, not {value}",
    "Einstufung:": "Classification:",
    "Routine": "routine",
    "Freigabe nötig": "approval required",
    "Fingerabdruck:": "Fingerprint:",
    "vor {age}": "{age} ago",
    "nie": "never",
    "{host}: letzte erfolgreiche Prüfung {last}": "{host}: last successful check {last}",
    "Nichts offen.": "Nothing open.",
    "Keine offenen Punkte.": "No open issues.",
    "Ausstehend, nicht eingespielt (Beobachtungsmodus): {plan}": "Pending, not installed (observation mode): {plan}",
    "{n} Meldung": "{n} notification",
    "{n} Meldungen": "{n} notifications",
    """Der Bot-Dienst läuft und holt die Nachrichten selbst ab. Erst anhalten:
  systemctl stop twguard-bot
dann in der Gruppe erneut /start@botname schreiben und twguard telegram-ids wiederholen.""":
    """The bot service is running and fetches the messages itself. Stop it first:
  systemctl stop twguard-bot
then write /start@botname in the group again and repeat twguard telegram-ids.""",
    "Versuch {n} gescheitert ({error}), neuer Versuch …": "Attempt {n} failed ({error}), trying again …",
    "Keine Nachrichten. Schreib dem Bot (in der Gruppe: /start@botname) und versuch es erneut.":
        "No messages. Write to the bot (in a group: /start@botname) and try again.",
    "Benutzer": "user",
    "gesendet": "sent",
    "Modus:": "Mode:",
    "Beobachtung (spielt nichts ein)": "observation (installs nothing)",
    "Routine (würde eingespielt)": "routine (would be installed)",
    "Neustart zu erwarten": "Reboot expected",
    "Tumbleweed-Updates: Routine einspielen, Kritisches melden und nachhaken.":
        "Tumbleweed updates: install routine ones, report critical ones and keep reminding.",
    "Konfigurationsdatei (Standard: {path})": "configuration file (default: {path})",
    "Repos auffrischen, einstufen, Routine-Updates einspielen (Timer)":
        "refresh repositories, classify, install routine updates (timer)",
    "nur einstufen und ausgeben; nichts einspielen, nichts senden": "only classify and print; install nothing, send nothing",
    "Telegram-Bot und Erinnerungen (Dienst)": "Telegram bot and reminders (service)",
    "offene Punkte anzeigen": "show open issues",
    "gespeicherte Ausgabe von 'zypper --xmlout dup --dry-run --details' einstufen":
        "classify saved output of 'zypper --xmlout dup --dry-run --details'",
    "Exit-Code des zypper-Laufs": "exit code of the zypper run",
    "Chat- und Benutzer-IDs aus den letzten Nachrichten an den Bot zeigen":
        "show chat and user IDs from the latest messages to the bot",
    "nur die Werte der Konfiguration zeigen, die von den Vorgaben abweichen":
        "show only the configuration values that differ from the defaults",
    "Testnachricht an den konfigurierten Chat senden": "send a test message to the configured chat",
    "twguard aus der Git-Arbeitskopie aktualisieren": "update twguard from the Git working copy",
    "Ziel-Commit (Standard: neuester Stand)": "target commit (default: latest version)",
    "twguard ist aktuell ({commit}).": "twguard is up to date ({commit}).",
    "Aktualisiere auf {commit} …": "Updating to {commit} …",
    "twguard läuft jetzt mit {commit}.": "twguard is now running {commit}.",
    "Update fehlgeschlagen, siehe die Meldung in Telegram und oben.": "Update failed, see the message in Telegram and above.",
    "Unbekannter Commit: {commit}": "Unknown commit: {commit}",
    '(dauert ungewöhnlich lange, systemd arbeitet noch daran)': '(taking unusually long, systemd is still working on it)',
    'Ausgenommen vom Neustart, nutzen alte Bibliotheken: {services}':
        'Excluded from restarts, using old libraries: {services}',
    'Ausstehend: {plan}': 'Pending: {plan}',
    'Bitte per SSH nachsehen (journalctl -u twguard-check), danach „Behoben“.':
        'Please check via SSH (journalctl -u twguard-check), then press "Fixed".',
    'Der Neustart von Diensten läuft noch, das Update wurde nicht eingespielt. Der nächste Prüflauf versucht es erneut.':
        'Services are still restarting, the update was not installed. The next check will try again.',
    'Die vorige Freigabe ist verfallen.': 'The previous approval has expired.',
    'Diese Nachricht ist veraltet, nichts ausgeführt. Der aktuelle Stand steht jetzt ganz unten.':
        'This message is outdated, nothing was done. The current state is now at the bottom.',
    'Erinnerung: {titles}': 'Reminder: {titles}',
    'Kernel {kernel} seit {when}': 'kernel {kernel} since {when}',
    'Letzte Prüfung {when}': 'Last check {when}',
    'Neustart abgebrochen.': 'Reboot cancelled.',
    'Neustart per Telegram ausgelöst ({planned}).': 'Reboot requested via Telegram ({planned}).',
    'Noch keine Prüfung': 'No check yet',
    'Paketverwaltung seit {minutes} min belegt durch {holder}, nichts eingespielt; der nächste Prüflauf versucht es erneut.':
        'Package management locked for {minutes} min by {holder}, nothing installed; the next check will try again.',
    'Prüfung fertig: {summary}, Freigabe nötig.': 'Check finished: {summary}, approval needed.',
    'Stand {when}': 'as of {when}',
    'Telegram: zu viele Nachrichten, warte %d s': 'Telegram: too many messages, waiting %d s',
    'Wartezeit nach Too Many Requests, noch {n} s': 'waiting after Too Many Requests, {n} s left',
    '(gekürzt, siehe journalctl)': '(shortened, see journalctl)',
    '(Text gekürzt, zu viele offene Punkte)': '(text shortened, too many open items)',
    '… weitere Angaben passen nicht in die Nachricht': '… more does not fit into the message',
    'Updates ausstehend (Beobachtungsmodus)': 'updates pending (observe mode)',
    'Zustandsnachricht nicht gesendet: %s': 'state message not sent: %s',
    'aufgeschoben bis {when}': 'snoozed until {when}',
    'danach Neustart nötig': 'reboot needed afterwards',
    'nichts offen': 'nothing open',
    'seit {when}': 'since {when}',
    '{n} Paket wäre Routine und würde eingespielt, im Beobachtungsmodus nicht.':
        '{n} package would be routine and installed, but not in observe mode.',
    '{n} Pakete wären Routine und würden eingespielt, im Beobachtungsmodus nicht.':
        '{n} packages would be routine and installed, but not in observe mode.',
    '{title}: als behoben quittiert.': '{title}: marked as fixed.',
    '↓ veraltet, der aktuelle Stand steht weiter unten (/status)': '↓ outdated, the current state is further down (/status)',
    '⏳ Einspielen wartet, bis die Dienste neu gestartet sind': '⏳ Installing waits until the services have restarted',
    '⏳ Freigegeben, Prüfung und Einspielen starten …': '⏳ Approved, check and installation starting …',
    '⏳ Prüfung wartet auf die Paketverwaltung: {holder}': '⏳ Check waits for package management: {holder}',
    '⏳ Spielt ein: {summary}': '⏳ Installing: {summary}',
    '✖️ Verwerfen': '✖️ Dismiss',
    '⬆️ Update von twguard auf {commit} läuft, der Bot antwortet so lange nicht. Tests und Installation dauern einige Minuten, danach wartet twguard bis zu {seconds} s auf den neuen Bot. Meldet er sich nicht oder schlägt die Installation fehl, wird die ursprüngliche Version wiederhergestellt.':
        '⬆️ Updating twguard to {commit}, the bot does not answer meanwhile. Tests and installation take a few minutes, then twguard waits up to {seconds} s for the new bot. If it does not report back or the installation fails, the original version is restored.',
    '⬆️ twguard {target} verfügbar, {n} neue Commits (läuft mit {commit}):':
        '⬆️ twguard {target} available, {n} new commits (running {commit}):',
    '🔁 Dienste werden neu gestartet: {services}': '🔁 Restarting services: {services}',
    '🔄 Prüfung läuft: Probelauf': '🔄 Check running: dry run',
    '🔄 Prüfung läuft: Repos auffrischen': '🔄 Check running: refreshing repos',
    '🔄 Prüfung startet …': '🔄 Check starting …',
    'Befehle. In der Gruppe als Antwort auf eine Nachricht dieses Bots oder mit @botname:\n/status – Zustandsnachricht nach unten holen (ohne @botname antworten alle Server)\n/details – Paketliste des letzten Probelaufs in der Zustandsnachricht\n/approve – gemeldetes Update freigeben und einspielen\n/check – sofort prüfen\n/later 1d – Erinnerungen aufschieben (z. B. 12h, 2d)\n/reboot – Neustart in 1 Minute\n/cancel – geplanten Neustart abbrechen\n/update – neuen Stand von twguard holen (ohne @botname: alle Server)\n/version – Version und Modus\n/language en – Sprache bis zum nächsten Neustart des Bots ändern (ohne @botname: alle Server)\n/mode auto – Modus bis zum nächsten Neustart des Bots ändern (auto oder observe; nur gezielt)\n/help – diese Hilfe (ohne @botname antwortet nur ein Server, gezielt jeder)\n/keyboard – feste Tastatur einblenden (/keyboard off: ausblenden)\n\nDie Tastatur „📊 Status“, „🔄 Alle prüfen“, „⬆️ Update“, „🏷️ Version“, „❓ Hilfe“ gilt für alle Server.\nDie Schaltflächen stehen in der Zustandsnachricht, der jeweils neuesten Nachricht mit Schaltflächen.':
        'Commands. In a group as a reply to a message from this bot or with @botname:\n/status – bring the state message to the bottom (without @botname all servers answer)\n/details – package list of the last dry run in the state message\n/approve – approve and install the reported update\n/check – check now\n/later 1d – snooze reminders (e.g. 12h, 2d)\n/reboot – reboot in 1 minute\n/cancel – cancel a scheduled reboot\n/update – fetch the new twguard version (without @botname: all servers)\n/version – version and mode\n/language de – change the language until the next restart of the bot (without @botname: all servers)\n/mode auto – change the mode until the next restart of the bot (auto or observe; only when addressed)\n/help – this help (without @botname only one server answers, when addressed each one does)\n/keyboard – show the permanent keyboard (/keyboard off: hide it)\n\nThe keyboard “📊 Status”, “🔄 Check all”, “⬆️ Update”, “🏷️ Version”, “❓ Help” applies to all servers.\nThe buttons are in the state message, always the newest message with buttons.',
}

TRANSLATIONS = {"de": {}, "en": EN}
LANGUAGES = tuple(TRANSLATIONS)

_language = "en"


def set_language(language: str) -> None:
    global _language
    _language = language


def current() -> str:
    return _language


def tr(text: str, **values) -> str:
    """Übersetzt den deutschen Text und setzt `values` in {platzhalter} ein."""
    text = TRANSLATIONS[_language].get(text, text)
    return text.format(**values) if values else text


def trn(one: str, many: str, n: int, **values) -> str:
    return tr(one if n == 1 else many, n=n, **values)
