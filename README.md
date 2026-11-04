# tw-update-guard (twguard)

🇩🇪 [Deutsche Fassung](README.de.md) · **Installation:** short guide in [INSTALL.md](INSTALL.md)

A service for headless openSUSE Tumbleweed servers. It installs routine updates (`zypper dup`) by itself and reports anything risky through Telegram. It keeps reminding you until the issue is actually resolved; reading the message is not enough.

Telegram messages, the `twguard` command line output, `--help` and the log lines (journal) are available in English (the default) and German (`language = "de"` in `[general]`).

## How it works

1. **Daily** (`twguard-check.timer`, 03:30 ± 30 min): `zypper refresh`, then `zypper dup --dry-run --details` with XML output.
2. **Classification** by fixed rules (`[rules]` in the configuration):

   | Case | Result |
   |---|---|
   | Normal updates, kernel updates within the running branch (7.3.0 → 7.3.2), removal of old libraries | routine: gets installed |
   | Kernel of a newer branch than the running one (7.3 → 7.4, `kernel`); it becomes the default boot entry | approval required |
   | Packages from `block` (boot loader, SSH, network) | approval required |
   | Version jump of a package from `watch` (e.g. systemd 258 → 259) | approval required |
   | Package from `watch` without a known previous version | approval required |
   | New or removed package from `watch` (e.g. `postgresql18-server`) | approval required |
   | Unexpected removals, more than `max_removals` removals | approval required |
   | Downgrade, vendor or architecture change | approval required |
   | Solver conflict | can only be resolved via SSH |

3. **Routine** updates are installed in `auto` mode with `zypper dup --no-refresh` (same repository state as the dry run; snapper takes snapshots as usual), followed by a *silent* Telegram message. If a reboot is needed, that becomes an open issue of its own.
4. **Open issues** (approval, reboot, error) stay open until the state is fixed. The bot reminds you on an escalation ladder:

   | Open for | Reminder |
   |---|---|
   | right away | once, then daily |
   | 2 days | every 8 hours |
   | 5 days | every 3 hours |
   | 10 days | hourly |

   Nights (22:00–08:00) are quiet; a reminder due when the daily briefing goes out is part of the briefing. `/later` postpones twice, at most 72 hours each. Temporary errors (repository unreachable) are only reported after a day. If no check succeeds for three days, that is reported too.

**Observation mode** (`mode = "observe"` in `[general]`, the default after installation): the timer checks and reports as usual but installs nothing. Routine updates appear in the state message as pending, with an “Install this time” button; a new state also comes once as a short silent event. Approvals also work in this mode. Only with `mode = "auto"` does twguard install routine updates by itself. Every non-empty zypper output is saved under `/var/lib/twguard/plans/` (the last 20).

An approval is bound to exactly the reported state (fingerprint of the package list with old and new version, architecture and repository). If a new snapshot arrives first, the approval expires and the new state is reported. zypper recomputes the plan when installing; if the executed plan differs from the checked one (for example because of a parallel `zypper ref`), the report lists the differences, with sound.

**Daily briefing** (`[briefing]`, default 08:00): every server sends its state message again once a day, also when everything is fine (“☀️ server: all good”, with the last check, kernel and, in observation mode, the pending packages). It is silent as long as nothing is open. If a server's briefing is missing, the server is off, offline or the bot is not running.

If you run `zypper dup` yourself via SSH, nothing else is needed: the bot notices the change of the RPM database and checks again right away.

## Setup

Step by step in [INSTALL.md](INSTALL.md): create a Telegram bot, clone, `./install.sh`, enter token and IDs, start the services. In short:

- **One bot per server**, all bots in one shared group. With @BotFather `/setprivacy` → **Disable**, so that a plain `/status` reaches all bots.
- Optionally the admin right **Delete messages**: the bots then clean up commands and old messages. Telegram often turns the group into a **supergroup with a new ID** (`-100…`); the bots adopt it and report it once, then enter it as `chat_id`.
- `install.sh` builds `twguard` as a single Python archive (standard library only, Python ≥ 3.11) in `/usr/local/sbin`, copies the systemd units and creates a short `/etc/twguard/config.toml` if there is none yet (only the required values and the path of the working copy under `[update]`). Only enter what differs: everything else comes from the defaults in the program, so new defaults arrive with each update. All options are explained in `/etc/twguard/config.default.toml`, which every update replaces. `twguard config-diff` prints the values of an existing configuration that differ from the defaults (including the token), so you can shorten it.
- The working copy (e.g. `/root/tw-update-guard`) is also the source for `/update`. A public clone over HTTPS is enough; for a private repository of your own, use a read-only deploy key.
- Without Git on the server: copy `build/twguard.pyz` and the files from `systemd/` over (then without `/update`).

## Telegram commands

In a group, commands for a specific server only apply as a **reply to a message from that bot** or with `@botname`. In a private chat with the bot this is not needed.

| Command | Effect |
|---|---|
| `/status` | bring the state message down; without `@botname` all servers answer |
| `/details` | package list of the last dry run, inside the state message |
| `/approve` | approve and install the reported update |
| `/check` | check now; the state message shows the progress, the result follows as a short event |
| `/later 12h`, `/later 2d` | postpone reminders (limited) |
| `/reboot` | reboot in 1 minute; the server reports back after the restart |
| `/cancel` | cancel a scheduled reboot |
| `/update` | fetch a new twguard version and offer it for confirmation; without `@botname` all servers answer |
| `/version` | version, Git commit and mode |
| `/language en`, `/language de` | change the language at runtime; without `@botname` all servers. Lasts until the next restart of the bot; the configuration is not changed |
| `/mode auto`, `/mode observe` | change the mode at runtime, only with `@botname` or as a reply (never all servers at once). Lasts until the next restart of the bot (also through `/update` or a reboot); the configuration is not changed |
| `/help` | all commands; without `@botname` only the bot with `help_full` answers. Disappears after 5 minutes |
| `/keyboard` | show a permanent keyboard: “📊 Status”, “🔄 Check all”, “⬆️ Update”, “🏷️ Version”, “❓ Help” (they apply to all servers; a server understands a key only from the version that introduced it). The keys belong to the message “⌨️ Keyboard …”, so that message stays. `/keyboard off` deletes it and thereby hides the keys |

All commands are also in the menu next to the input line.

**Services holding old files:** After an update, and every 30 minutes, twguard asks `zypper ps` which services still hold deleted files. They become an open issue with one button per service and “Restart all”. A press restarts only services that were offered and that `zypper ps` still lists; the state message shows the restart while it runs, the result follows as a short event (with sound if a service is not running afterwards). Services whose restart would end sessions or cut off the network (D-Bus, `systemd-*`, `user@*`, network services and the like) are not offered; `[services] exclude` sets the patterns, `["*"]` turns the offer off. These services get renewed by the next reboot.

**Language:** `language = "en"` (default) or `"de"` in `[general]` sets the language of the Telegram output per server, including buttons, command menu and the HTML file; then run `systemctl restart twguard-bot`. `/language` changes it at runtime and says so; a bot restart (also through `/update` or a reboot) returns to the configuration, so to make a change permanent you enter it there. Every server understands the permanent keyboard in all languages, even if it comes from a bot with a different language. Messages already sent and the reasons of open issues stay in the old language until they are generated again.

Only the state message has buttons (Approve, Details, 1 day later, Reboot, Cancel reboot, Restart service, Check now). A button only reaches the bot that sent the message, and it only acts on that bot's current state message: a button on an older message does nothing, says so and brings the current state down. “Approve” only applies to the state shown.

## Updating twguard itself (OTA)

`/update` runs `git fetch` for `origin/main` in the working copy (`[update] repo`) and shows the new commits with an “Install” button. Then `twguard self-update` runs as a separate unit, `twguard-update`:

1. Fast-forward to exactly the commit shown (local changes or diverged history: abort).
2. Unit tests of the new version; if they fail, everything stays as it was.
3. The running version is saved as `/usr/local/sbin/twguard.prev`, `install.sh` builds and installs the new one and restarts the bot.
4. If the new bot reports within 90 seconds, it writes “now running …”. Otherwise the old version, the old systemd units and the old Git state are restored, with a message (also if something goes wrong while doing so).

Log: `journalctl -u twguard-update`. Changes per version and upgrade notes: [CHANGELOG.md](CHANGELOG.md).

**One state message per server.** Everything that is true right now stands in a single message: open issues with their buttons, what is running (check and its phase, service restart, self-update, scheduled reboot, who holds the package lock), hints, last check and kernel. The bot edits it whenever the state changes, so a running check shows its progress there.
- What happened (installed, restarted, back after reboot, errors) arrives as a short **event** without buttons, and the state message is sent again below it. Events during a running action of twguard are collected and come as one message once it is done (at the latest after 5 minutes); errors come at once, with sound.
- `/status`, a due reminder and the daily briefing send the state message again at the bottom; the previous one disappears. A reminder comes with sound and starts with “🔔”.
- “Details” shows the package list inside the state message (by name, switchable to “by version”, paged if long; “📄 As a file” sends all packages as an HTML file; “◀️ Back” returns). If the state changes meanwhile, it returns to the overview by itself.
- Commands are deleted after the answer (admin right needed). Help, version and confirmations delete themselves after 2 to 5 minutes.
- Telegram only allows deleting within the first 48 hours. The bot shortens older messages to one line instead.

Feedback: for commands Telegram immediately shows “typing …”, for buttons a short note.

Commands are only accepted from the user IDs in `allowed_user_ids`, only from the configured chat or a private chat, and only if they are at most 10 minutes old. There are no shell commands via Telegram.

## Command line

```bash
twguard status                      # open issues
twguard check --dry-run             # classification without any effect
twguard classify file.xml           # classify a saved zypper XML output
twguard config-diff                 # configuration values that differ from the defaults
twguard self-update                 # update to the latest version, like /update
journalctl -u twguard-check -u twguard-bot
journalctl -u twguard-bot | grep -E 'slow|connection'   # Telegram calls over 3 s, with duration per phase
journalctl -u twguard-bot | grep 'errors in one hour'   # more fetch and send errors than max_errors_per_hour
```

## Limits

- An OTA update runs code from the Git repository as root. Whoever can push to `main` of the repository your servers pull from controls your servers; passing tests do not protect against malicious code. If you do not want to trust this repository, let your servers pull from your own fork and review changes before merging.
- If the server is off or offline, the bot is silent too. Use external monitoring for that (e.g. Uptime Kuma).
- Solver conflicts cannot be resolved via Telegram.
- Buttons are bound to the plan they show. A typed `/approve`, even as a reply to an older message, approves the plan shown last.
- Package operations outside twguard (e.g. `zypper` via SSH, `purge-kernels` after boot) cannot be excluded, only detected: while they hold the package lock, checks and installs wait and a reboot or service restart from the bot is refused. A foreign operation that starts *after* the reboot button was pressed, during the one-minute delay, is not stopped. twguard's own installs, service restarts, self-updates and reboots exclude each other; whether a reboot is scheduled comes from systemd-logind (also one scheduled or cancelled on the console). Changes between check and update are reported afterwards, not prevented.
- Values in your `config.toml` always win over the defaults, also over better defaults of a later version (see the upgrade notes in [CHANGELOG.md](CHANGELOG.md) and `twguard config-diff`).
- Services using outdated libraries are never restarted automatically, not even in `auto` mode: only when you press their button, and services in `[services] exclude` (D-Bus, `systemd-*`, getty, display manager, network and others) are not offered at all.

## Security

twguard runs as root, and its bot acts on Telegram messages: it approves and installs updates, reboots the server, restarts services and updates itself. Keep this in mind:

- Everyone listed in `allowed_user_ids` may do all of that; there are no roles. List only people who administer the server.
- Whoever takes over one of these Telegram accounts controls the server within these limits. Enable two-step verification in Telegram and protect the devices logged in.
- Use a small group of your own (or a private chat), and keep the bot token secret: `config.toml` is readable by root only. A leaked token lets others read what is sent to the bot, send in its name and disturb it, but not give it commands.
- Nothing sent via Telegram is executed as a shell command; buttons and commands only trigger the fixed actions above.
- See also the first item under [Limits](#limits) on OTA updates.

Please do not report vulnerabilities publicly; see [SECURITY.md](SECURITY.md).

## Credits

twguard was written in dialogue with AI assistants: [Claude Code](https://claude.com/claude-code) (Anthropic) wrote most of the code, OpenAI Codex reviewed it several times for bugs and security issues. Design, requirements, testing on real servers and all decisions: DemoFreak.

## License

Copyright © 2026 DemoFreak

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. It is distributed in the hope that it will be useful, but **without any warranty**. See [LICENSE](LICENSE). SPDX: `GPL-3.0-or-later`.
