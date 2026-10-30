# Installation (Kurzfassung)

🇬🇧 [English version](INSTALL.md)

Für jeden Server, als root. Voraussetzungen: openSUSE Tumbleweed, `git`, Python ≥ 3.11 (ist auf Tumbleweed vorhanden), ein Telegram-Konto. Weitere Pakete braucht twguard nicht.

## 1. Telegram-Bot anlegen

1. Bei **@BotFather**: `/newbot` → Name und Benutzername (z. B. `server1_updates_bot`) → Token notieren.
2. Ebenfalls bei @BotFather: `/setprivacy` → Bot wählen → **Disable**. Sonst sieht der Bot in der Gruppe nur gezielt an ihn gerichtete Befehle.
3. Eine Gruppe anlegen (bei mehreren Servern: eine für alle) und den Bot hinzufügen. Optional das Admin-Recht **Nachrichten löschen** geben, dann räumt der Bot Befehle und alte Meldungen weg. Telegram macht daraus oft eine Supergruppe mit neuer ID; twguard bemerkt das und meldet die neue ID.

## 2. Installieren

```bash
git clone https://github.com/DemoFreak/tw-update-guard.git /root/tw-update-guard
cd /root/tw-update-guard && ./install.sh
```

`install.sh` baut `twguard` als einzelne Datei nach `/usr/local/sbin`, kopiert die systemd-Units und legt `/etc/twguard/config.toml` an (Beobachtungsmodus: prüft und meldet, spielt nichts ein). Es startet noch keinen Dienst. Ein erneuter Aufruf schadet nicht und zeigt die nächsten Schritte wieder an.

## 3. Konfigurieren

1. Token in `/etc/twguard/config.toml` unter `[telegram]` eintragen.
2. In der Gruppe `/start@server1_updates_bot` schreiben, dann:
   ```bash
   twguard telegram-ids
   ```
   Die angezeigte Chat-ID als `chat_id` und deine Benutzer-ID unter `allowed_user_ids` eintragen.
3. Bei genau einem Server `help_full = true` setzen; nur er beantwortet `/help` an alle.
4. Für deutsche Ausgaben in `[general]` `language = "de"` setzen; Standard ist Englisch, auch die Kommentare in der Konfigurationsdatei sind englisch.

## 4. Prüfen und starten

```bash
twguard check --dry-run      # Einstufung ansehen; ändert nichts, sendet nichts
twguard test-message         # Telegram prüfen
systemctl enable --now twguard-check.timer twguard-bot.service
```

Im Gruppenchat `/status` schreiben; jeder Server antwortet. `/keyboard@server1_updates_bot` blendet eine Tastatur mit den wichtigsten Befehlen ein.

Wenn die Meldungen eine Weile stimmen, in `[general]` `mode = "auto"` setzen und `systemctl restart twguard-bot`. Ab dann spielt twguard Routine-Updates selbst ein.

## Aktualisieren

In Telegram `/update` (oder die Taste „⬆️ Update“): Der Bot zeigt die neuen Commits und spielt sie nach Bestätigung ein, mit Tests vorher und Rückfall auf die alte Version. Von Hand:

```bash
cd /root/tw-update-guard && git pull --ff-only && ./install.sh
```

Neue Konfigurationseinträge zeigt `diff -u /etc/twguard/config.toml /etc/twguard/config.default.toml`.

## Entfernen

```bash
systemctl disable --now twguard-check.timer twguard-bot.service
rm /etc/systemd/system/twguard-{check.service,check.timer,bot.service} /usr/local/sbin/twguard*
systemctl daemon-reload
rm -r /etc/twguard /var/lib/twguard    # Konfiguration und Zustand
```
