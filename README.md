# tw-update-guard (twguard)

🇩🇪 [Deutsche Fassung](README.de.md) · **Installation:** short guide in [INSTALL.md](INSTALL.md)

A service for headless openSUSE Tumbleweed servers. It installs routine updates (`zypper dup`) by itself and reports anything risky through Telegram. It keeps reminding you until the issue is actually resolved; reading the message is not enough.

Telegram messages, the `twguard` command line output, `--help` and the log lines (journal) are available in English (the default) and German (`language = "de"` in `[general]`).

## How it works

1. **Daily** (`twguard-check.timer`, 03:30 ± 30 min): `zypper refresh`, then `zypper dup --dry-run --details` with XML output.
2. **Classification** by fixed rules (`[rules]` in the configuration):

   | Case | Result |
   |---|---|
   | Normal updates, kernel, removal of old libraries | routine: gets installed |
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

   Nights (22:00–07:00) are quiet. `/later` postpones twice, at most 72 hours each. Temporary errors (repository unreachable) are only reported after a day. If no check succeeds for three days, that is reported too.

**Observation mode** (`mode = "observe"` in `[general]`, the default after installation): the timer checks and reports as usual but installs nothing. Routine updates are reported silently as “would be installed” (once per state), with an “Install this time” button. Approvals also work in this mode. Only with `mode = "auto"` does twguard install routine updates by itself. Every non-empty zypper output is saved under `/var/lib/twguard/plans/` (the last 20).

An approval is bound to exactly the reported state (fingerprint of the package list with old and new version, architecture and repository). If a new snapshot arrives first, the approval expires and the new state is reported. zypper recomputes the plan when installing; if the executed plan differs from the checked one (for example because of a parallel `zypper ref`), the report lists the differences, with sound.

**Daily briefing** (`[briefing]`, default 08:00): every server reports once a day, also when everything is fine (“☀️ server: all good”, with the last check, kernel, uptime and, in observation mode, the pending packages). It is silent as long as nothing is open. If a server's briefing is missing, the server is off, offline or the bot is not running.

If you run `zypper dup` yourself via SSH, nothing else is needed: the bot notices the change of the RPM database and checks again right away.

## Setup

Step by step in [INSTALL.md](INSTALL.md): create a Telegram bot, clone, `./install.sh`, enter token and IDs, start the services. In short:

- **One bot per server**, all bots in one shared group. With @BotFather `/setprivacy` → **Disable**, so that a plain `/status` reaches all bots.
- Optionally the admin right **Delete messages**: the bots then clean up commands and old messages. Telegram often turns the group into a **supergroup with a new ID** (`-100…`); the bots adopt it and report it once, then enter it as `chat_id`.
- `install.sh` builds `twguard` as a single Python archive (standard library only, Python ≥ 3.11) in `/usr/local/sbin`, copies the systemd units and creates `/etc/twguard/config.toml` if there is none yet (with `mode = "observe"` and the path of the working copy under `[update]`). The template of the installed version is always next to it as `/etc/twguard/config.default.toml`; `diff -u /etc/twguard/config.toml /etc/twguard/config.default.toml` shows new entries.
- The working copy (e.g. `/root/tw-update-guard`) is also the source for `/update`. A public clone over HTTPS is enough; for a private repository of your own, use a read-only deploy key.
- Without Git on the server: copy `build/twguard.pyz` and the files from `systemd/` over (then without `/update`).

## Telegram commands

In a group, commands for a specific server only apply as a **reply to a message from that bot** or with `@botname`. In a private chat with the bot this is not needed.

| Command | Effect |
|---|---|
| `/status` | open issues; without `@botname` all servers answer |
| `/details` | package list of the last dry run |
| `/approve` | approve and install the reported update |
| `/check` | check now; a waiting message appears at once and is replaced by the result |
| `/later 12h`, `/later 2d` | postpone reminders (limited) |
| `/reboot` | reboot in 1 minute; the server reports back after the restart |
| `/cancel` | cancel a scheduled reboot |
| `/update` | fetch a new twguard version and offer it for confirmation; without `@botname` all servers answer |
| `/version` | version, Git commit and mode |
| `/help` | all commands; without `@botname` only the bot with `help_full` answers. Disappears after 5 minutes |
| `/keyboard` | show a permanent keyboard: “📊 Status”, “🔄 Check all”, “⬆️ Update”, “🏷️ Version”, “❓ Help” (they apply to all servers; a server understands a key only from the version that introduced it). The keys belong to the message “⌨️ Keyboard …”, so that message stays. `/keyboard off` deletes it and thereby hides the keys |

All commands are also in the menu next to the input line.

**Language:** `language = "en"` (default) or `"de"` in `[general]` sets the language of the Telegram output per server, including buttons, command menu and the HTML file; then run `systemctl restart twguard-bot`. Every server understands the permanent keyboard in all languages, even if it comes from a bot with a different language. Messages already sent and the reasons of open issues stay in the old language until they are generated again.

Messages have buttons for the most important commands (Approve, Details, 1 day later, Reboot, Cancel reboot, Check again). A button only reaches the bot that sent the message; buttons with consequences disappear after the first press. “Approve” only applies to the state reported in that message.

## Updating twguard itself (OTA)

`/update` runs `git fetch` for `origin/main` in the working copy (`[update] repo`) and shows the new commits with an “Install” button. Then `twguard self-update` runs as a separate unit, `twguard-update`:

1. Fast-forward to exactly the commit shown (local changes or diverged history: abort).
2. Unit tests of the new version; if they fail, everything stays as it was.
3. The running version is saved as `/usr/local/sbin/twguard.prev`, `install.sh` builds and installs the new one and restarts the bot.
4. If the new bot reports within 90 seconds, it writes “now running …”. Otherwise the old version, the old systemd units and the old Git state are restored, with a message (also if something goes wrong while doing so).

Log: `journalctl -u twguard-update`.

Feedback: for commands Telegram immediately shows “typing …”, for buttons briefly “Running …”. `/check`, “Check now” and approvals immediately send a waiting message that the check later replaces with its result (also “no updates”).

**Tidy chat:** at most one message stays per server and topic.
- Commands are deleted after the answer (admin right needed). Help, version, details and confirmations delete themselves after 2 to 5 minutes.
- `/status`, briefing and reminders replace their predecessor. A reminder therefore still arrives as new (with sound), the previous one disappears.
- Buttons edit the message they belong to: “Approve” turns it into the waiting message and then the result, “Details” expands the package list (by name, switchable to “by version” with grouped packages, paged if long; “📄 As a file” sends all packages as an HTML file; “◀️ Back” collapses it), “1 day later” notes the postponement, “Reboot” shows the reboot and “back” after the restart.
- Resolved issues remain as “✅ … resolved”, without buttons.
- Silent update reports stay until the next briefing, which summarizes and deletes them.
- Telegram only allows deleting within the first 48 hours. The bot shortens older messages to one line instead.

Commands are only accepted from the user IDs in `allowed_user_ids`, only from the configured chat or a private chat, and only if they are at most 10 minutes old. There are no shell commands via Telegram.

## Command line

```bash
twguard status                      # open issues
twguard check --dry-run             # classification without any effect
twguard classify file.xml           # classify a saved zypper XML output
journalctl -u twguard-check -u twguard-bot
journalctl -u twguard-bot | grep -E 'slow|connection'   # Telegram calls over 3 s, with duration per phase
journalctl -u twguard-bot | grep 'errors in one hour'   # more fetch and send errors than max_errors_per_hour
```

## Limits

- An OTA update runs code from the Git repository as root. Whoever can push to `main` of the repository your servers pull from controls your servers; passing tests do not protect against malicious code. If you do not want to trust this repository, let your servers pull from your own fork and review changes before merging.
- If the server is off or offline, the bot is silent too. Use external monitoring for that (e.g. Uptime Kuma).
- Solver conflicts cannot be resolved via Telegram.
- Services using outdated libraries are only reported, not restarted.

## Security

Please do not report vulnerabilities publicly; see [SECURITY.md](SECURITY.md).

## Credits

twguard was written in dialogue with AI assistants: [Claude Code](https://claude.com/claude-code) (Anthropic) wrote most of the code, OpenAI Codex reviewed it several times for bugs and security issues. Design, requirements, testing on real servers and all decisions: DemoFreak.

## License

Copyright © 2026 DemoFreak

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. It is distributed in the hope that it will be useful, but **without any warranty**. See [LICENSE](LICENSE). SPDX: `GPL-3.0-or-later`.
