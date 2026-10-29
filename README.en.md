# tw-update-guard (twguard)

🇩🇪 [Deutsche Fassung](README.md) · **Installation:** [INSTALL.en.md](INSTALL.en.md)

A service for headless openSUSE Tumbleweed servers. It installs routine updates (`zypper dup`) by itself and reports anything risky through Telegram. It keeps reminding you until the issue is actually resolved – reading the message is not enough.

> The user interface (Telegram messages, command line output) is currently German. Multilingual output is planned.

## How it works

1. **Daily** (`twguard-check.timer`, 03:30 ± 30 min): `zypper refresh`, then `zypper dup --dry-run --details` with XML output.
2. **Classification** by fixed rules (`[rules]` in the configuration):

   | Case | Result |
   |---|---|
   | Normal updates, kernel, removal of old libraries | routine: installed automatically |
   | Packages from `block` (boot loader, SSH, network) | approval required |
   | Version jump of a package from `watch` (e.g. systemd 258 → 259) | approval required |
   | Package from `watch` without a known previous version | approval required |
   | New or removed package from `watch` (e.g. `postgresql18-server`) | approval required |
   | Unexpected removals, more than `max_removals` removals | approval required |
   | Downgrade, vendor or architecture change | approval required |
   | Solver conflict | only solvable via SSH |

3. **Routine** updates are installed in `auto` mode with `zypper dup --no-refresh` (same repository state as the dry run; snapper takes snapshots as usual), followed by a *silent* Telegram message. If a reboot is needed, that becomes an open issue of its own.
4. **Open issues** (approval, reboot, error) stay open until the state is fixed. The bot reminds you on an escalation ladder: right away and then daily, after 2 days every 8 hours, after 5 days every 3 hours, after 10 days hourly. Nights (22–7 h) are quiet. `/later` postpones twice, at most 72 hours each.

**Observe mode** (`mode = "observe"`, the default after installation): checks and reports as usual but installs nothing. Routine updates are reported silently as “would be installed”, with a button to install them this once. Only with `mode = "auto"` does twguard install routine updates by itself.

An approval is bound to exactly the reported state (fingerprint of the package list with old and new version, architecture and repository). If a new snapshot arrives first, the approval expires and the new state is reported. zypper recomputes the plan when installing; if the executed plan differs from the checked one, the report lists the differences with sound.

**Daily briefing** (default 08:00): every server reports once a day, also when everything is fine. A missing briefing means the server or the bot is down.

If you run `zypper dup` yourself via SSH, nothing else is needed: the bot notices the change of the RPM database and checks again.

## Telegram

One bot per server, all bots in one group. Commands for a specific server must be a reply to one of its messages or carry `@botname`; `/status`, `/update`, `/version` and the keyboard keys go to all servers.

| Command | Effect |
|---|---|
| `/status` | open issues |
| `/details` | package list of the last dry run |
| `/approve` | approve and install the reported update |
| `/check` | check now; a waiting message is replaced by the result |
| `/later 12h`, `/later 2d` | postpone reminders (limited) |
| `/reboot`, `/cancel` | reboot in 1 minute, cancel it |
| `/update` | fetch a new twguard version and offer it for installation |
| `/version` | version, Git commit and mode |
| `/help` | all commands (sent to all: only the bot with `help_full` answers) |
| `/keyboard` | show a persistent keyboard (Status, Check all, Update, Version, Help); `/keyboard off` removes it |

Messages carry buttons for the most important actions. The chat stays tidy: commands are deleted after the answer (admin right needed), status and reminders replace their predecessor, resolved issues remain as “✅ done”.

Commands are only accepted from the user IDs in `allowed_user_ids`, only from the configured chat or a private chat, and only if they are at most 10 minutes old. There are no shell commands via Telegram.

## Self-update (OTA)

`/update` runs `git fetch` in the working copy, shows the new commits and, after confirmation, runs `twguard self-update` as a separate systemd unit: fast-forward to exactly the shown commit, unit tests, backup of the running version and the systemd units, `install.sh`, restart. If the new bot does not report within 90 seconds, the old version, units and Git state are restored.

## Command line

```bash
twguard status                      # open issues
twguard check --dry-run             # classification without any effect
twguard classify file.xml           # classify a saved zypper XML output
journalctl -u twguard-check -u twguard-bot
```

## Limits

- An OTA update runs code from the Git repository as root. Whoever can push to `main` of the repository your servers pull from controls your servers; passing tests do not protect against malicious code. If you do not want to trust this repository, let your servers pull from your own fork and review changes before merging.
- If the server is off or offline, the bot is silent too. Use external monitoring (e.g. Uptime Kuma) for that.
- Solver conflicts cannot be resolved via Telegram.
- Services using outdated libraries are only reported, not restarted.

## Security

Please do not report vulnerabilities publicly; see [SECURITY.md](SECURITY.md).

## Credits

twguard was written in dialogue with AI assistants: [Claude Code](https://claude.com/claude-code) (Anthropic) wrote most of the code, OpenAI Codex reviewed it several times for bugs and security issues. Design, requirements, testing on real servers and all decisions: DemoFreak.

## License

Copyright © 2026 DemoFreak

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. It is distributed in the hope that it will be useful, but **without any warranty**. See [LICENSE](LICENSE). SPDX: `GPL-3.0-or-later`.
