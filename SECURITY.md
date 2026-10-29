# Security / Sicherheit

## Reporting a vulnerability

Please report vulnerabilities privately via GitHub: **Security → Report a vulnerability** in this repository. Do not open a public issue. You will get an answer as soon as possible; this is a spare-time project.

## Schwachstelle melden

Schwachstellen bitte vertraulich über GitHub melden: **Security → Report a vulnerability** in diesem Repository, nicht als öffentliches Issue. Eine Antwort kommt so bald wie möglich; das Projekt entsteht in der Freizeit.

## Security model / Sicherheitsmodell

- twguard runs as root and installs packages. / twguard läuft als root und spielt Pakete ein.
- Telegram commands are accepted only from `allowed_user_ids`, only from the configured chat or a private chat, and only if at most 10 minutes old. There are no shell commands via Telegram. / Telegram-Befehle nur von `allowed_user_ids`, aus dem konfigurierten oder privaten Chat, höchstens 10 Minuten alt; keine Shell-Befehle.
- The bot token in `/etc/twguard/config.toml` (mode 0600) grants control over the bot; keep it secret. / Der Bot-Token gibt Kontrolle über den Bot; geheim halten.
- The self-update (`/update`) runs code from the configured Git repository as root, so write access to that repository means control over your servers. Pull from a fork you review if you prefer. / Das Selbst-Update führt Code aus dem konfigurierten Repo als root aus; Schreibzugriff darauf heißt Kontrolle über die Server. Wer mag, holt aus einem eigenen, geprüften Fork.
