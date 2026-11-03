# Changelog

Published versions of tw-update-guard. Versions in between were development steps and are part of the next
published version. **Upgrade** notes name what you may have to change by hand; `twguard config-diff` shows which
values in your `config.toml` differ from the defaults.

## 0.8.8 – 2026-11-03

- A kernel of a newer branch than the running one (e.g. 7.3 → 7.4) needs approval; it becomes the default boot
  entry and would start with any reboot, also an unplanned one. Updates within the running branch stay routine.
  Setting: `kernel` in `[rules]`, `[]` restores the old behaviour
- After `/check` or an approval, the waiting message shows that the update is being installed (packages, download
  size, reboot expected) until the result replaces it
- The status message offers the buttons of open items (e.g. "Reboot", "Approve", service restarts) in addition to
  "Check now"; like in the briefing, a button answers in a message of its own. "Details" only appears when the last
  check found packages
- A check without updates lists the open items with their buttons below the result
- Pressing "Back" twice no longer replaces the message with the package list without buttons
- Continuous integration: unit tests on Python 3.11 and the latest Python, zipapp build with a start check,
  `bash -n` and shellcheck for `install.sh`
- Documentation: this changelog, security model, more limits, uninstalling

**Upgrade:** Nothing to change. The new `kernel` rule applies with its default, also with an existing `[rules]`
section; set `kernel = []` there to keep kernels routine.

## 0.8.6 – 2026-11-03

- A reminder due when the daily briefing goes out is part of the briefing (🔔 line, text and buttons) instead of
  a second message right after it
- Buttons pressed in the briefing answer in a message of their own; only the buttons of that item disappear from
  the briefing, the other items stay usable. If Telegram refuses that message, the briefing stays unchanged and the
  results of updates and restarts arrive later as new messages
- Default quiet hours end at 08:00, like the briefing

**Upgrade:** An explicit `quiet_hours = "22:00-07:00"` in `config.toml` keeps the old end; a reminder may then
still arrive an hour before the briefing. Remove the entry or change it to `22:00-08:00`.

## 0.8.3 – 2026-11-02

- Services still using deleted files after an update become an open item with reminders, a button per service and
  "All". They are never restarted automatically. `[services] exclude` keeps services such as D-Bus, sessions, getty,
  the display manager and the network out of the offer
- Restart results are kept until Telegram accepts them; unknown or unfinished restarts are never reported as success
- Solver conflicts: one short line per conflict in the message, the full text with solutions under Details
- `/approve` only applies to the plan last shown; a changed plan is reported at once as a new offer and ends a snooze
- New "Resolved" button for a failed update that was fixed by hand
- Self-update also rolls back on file and process errors
- Messages are identified by chat and message ID

## 0.7.4 – 2026-10-30

- `twguard self-update` without an argument updates to the latest version; an invalid commit is rejected before
  anything happens; the result is printed on the console

## 0.7.3 – 2026-10-30

- `twguard config-diff` prints the configuration values that differ from the defaults
- `install.sh` creates a short `config.toml` with only the required values on a new installation;
  `config.default.toml` stays the reference for all options

**Upgrade:** Existing `config.toml` files are not changed. Old `[rules]` lists in them hide new defaults of later
versions; shorten the file with the help of `twguard config-diff`.

## 0.7.2 – 2026-10-30

- `/language en|de` changes the language at once, on all servers when sent without `@botname`, until the next
  restart of the bot

## 0.7.1 – 2026-10-30

- Tests no longer read the real `/etc/twguard/config.toml`; on such machines an update via `/update` failed and
  rolled back

## 0.7.0 – 2026-10-30

- Telegram, command line, `--help` and log in English and German (`language` in `[general]`), English by default
- Pending routine updates in observe mode are shown in `twguard status`, `/status` and the briefing

**Upgrade:** Installations without a `language` entry switch to English; set `language = "de"` for German.

## 0.5.4 – 2026-10-30

- `/details` sorted by name or grouped by version change, changed version part in bold, long lists on pages, the
  whole list as an HTML file
- Telegram fetch and send errors are counted; a warning only above `max_errors_per_hour` (default 10)

## 0.5.2 – 2026-10-29

First published version: classification by fixed rules, automatic routine updates (`mode = "auto"`) or observe mode
(default), approvals bound to a fingerprint of the plan, one Telegram bot per server with commands and buttons,
escalating reminders with quiet hours, reboot and stall detection, daily briefing, self-update from a Git working
copy with tests and rollback.
