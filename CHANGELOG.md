# Changelog

Published versions of tw-update-guard. Versions in between were development steps and are part of the next
published version. **Upgrade** notes name what you may have to change by hand; `twguard config-diff` shows which
values in your `config.toml` differ from the defaults.

## 0.10.9 – 2026-11-13

- "Approve" (and "Install this time") is hidden and refused while twguard checks or installs: an approval
  starts the check that installs the plan itself, so a second one meant nothing. Before, "Approve" stayed in the
  state message next to "Installing: …"
- "1 day later" is hidden while twguard installs, and `/later` does nothing then

**Upgrade:** none.

## 0.10.8 – 2026-11-12

- **Approving needs a second tap.** “Approve” and “Install this time” first ask “Install this update now?” with
  “Cancel” and “Yes, install” side by side; only “Yes” approves, for the plan shown and within one minute. Before,
  a single tap installed a plan that needed approval: with Telegram responding slowly, the state message changed
  under the finger and a second tap meant for “Check now” hit “Approve”. A typed `/approve` stays one step
- Every approval is logged in the journal (user, way, plan) and reported as an event in the group

**Upgrade:** none.

## 0.10.7 – 2026-11-12

- English texts: "observation mode" everywhere (some said "observe mode"); the plan size reads
  "download size 10 MB" instead of "download 10 MB"; `/mode` and the hint in the state message show the mode as
  the value used in `config.toml` and after `/mode` (`auto`, `observe`), formatted as code, so it is not taken
  for the prose name ("observation mode")

**Upgrade:** none.

## 0.10.6 – 2026-11-10

- "Check now", `/check`, "Status" and `/status` now also ask at once which services still use old files.
  Services restarted on the console disappeared from the state message only after up to 30 minutes, and new
  ones appeared just as late. The package list (`/details`) still asks only whether a reboot is needed

**Upgrade:** none.

## 0.10.5 – 2026-11-09

- The size of an update that needs approval is shown right after updating twguard, without waiting for the
  next check; 0.10.4 showed it only for checks made by 0.10.4 itself
- An update that needs approval and one waiting in observation mode are now one thing internally: state
  message, buttons, `/approve` and `twguard status` treat both alike. `/approve` without a button now also
  installs a routine update shown in observation mode once, like "Install this time"
- "Install this time" on an older message no longer starts a check when nothing is pending any more

**Upgrade:** none.

## 0.10.4 – 2026-11-09

- An update that needs approval now shows its size (number of packages, download size) in the state message,
  as the observation mode already did; before, only the reasons were listed

**Upgrade:** none.

## 0.10.3 – 2026-11-04

- The state message always names the mode in its last line, "automatic mode" as well as "observation mode";
  before, automatic mode was shown nowhere
- A service without a main process whose leftover process survives a restart (a wrapped init script with
  `KillMode=process`, e.g. `vboxadd-service`, whose stop script does not end `VBoxService`) was offered for a
  restart again and again. When all of its processes holding old files are older than the service's last start,
  it is now shown as a hint about leftover processes, without a restart button

**Upgrade:** none.

## 0.10.2 – 2026-11-04

- While twguard's own check refreshed the repositories, the state message also said "Package management locked:
  twguard-check.service … Checks and installs wait for it", as if the check waited for itself, and the line stayed
  up to a minute after the check had finished. Only other programs holding the lock are shown now
- Updating from 0.9.x to 0.10 left the old "updating to … The result will appear here" message standing next to
  the result. It is removed like the other old messages once the state message stands
- "Check now" is hidden while a check runs. `/check`, "Check all" and a button pressed just before do nothing
  then: a second check cannot be queued, and the state message already shows the running one

**Upgrade:** none. If a server came from 0.9.x to 0.10.0 or 0.10.1, an old "updating to …" message may still
stand; delete it by hand.

## 0.10.1 – 2026-11-04

- A service whose program fails right after starting (e.g. its executable is missing, systemd `203/EXEC`) was
  reported as restarted: for a simple service systemd finishes the start job as soon as the process exists. A
  restarted service now has to stay active for 10 seconds before it counts as a success; otherwise the result
  names it as failed, with sound. This also holds when systemd finishes the job only after the 15-minute limit.
  A service that does not reach these 10 seconds within half a minute (restart loop) or whose start time systemd
  does not report is named as "result not confirmed", with sound, never as restarted
- Package changes made outside twguard (e.g. `zypper dup` over SSH) were not noticed on installations whose rpm
  database uses the ndb format (`Packages.db`, the default on fresh Tumbleweed installations); only
  `rpmdb.sqlite` was watched. Both are watched now
- Buttons whose action would be refused right now (e.g. reboot while services restart, service restarts while
  the package management is busy or a reboot is scheduled) are no longer shown; they come back when the action
  is possible again. The rule is the same table that refuses a pressed button, so both always agree
- A button pressed while Telegram was unreachable was answered "outdated" afterwards, because the bot sent a
  new state message before it fetched the press. After a failed fetch the bot now reads first and sends then
  (at most 5 minutes, in case only fetching is broken). Several presses fetched at once all count on the message
  they were made on, even when the first one already brought a new state message

**Upgrade:** none.

## 0.10.0 – 2026-11-04

- **One state message per server.** Everything that is true right now (open issues with their buttons, a running
  check with its phase, a service restart, a self-update, a scheduled reboot, who holds the package lock, hints,
  last check and kernel) stands in a single message that the bot edits whenever the state changes. It is the
  only message with buttons. Before, status, briefing, reminders, check results and update offers were separate
  messages, each with its own buttons; a button pressed in one could make the buttons of another item disappear
  (e.g. the reboot button after a service restart), and `/check` could leave two nearly identical messages
- What happened (installed, restarted, back after a reboot, errors) arrives as a short event without buttons,
  followed by the state message again. Events during a running action of twguard are collected into one message
  (at the latest after 5 minutes); errors come at once, with sound
- A button on an older message no longer acts: it says the message is outdated, removes it and brings the
  current state message down
- `/status`, a due reminder (with sound, "🔔") and the daily briefing send the state message again at the bottom.
  A reminder counts only once it was delivered. "Details" shows the package list inside the state message and
  returns to the overview by itself when the state changes
- The check run and the self-update no longer talk to Telegram; only the bot sends. When Telegram answers
  "Too Many Requests", the bot pauses for the time Telegram asks for
- Services whose own process is already fresh but where other processes keep old files (units with
  `KillMode=process`, e.g. a `screen` session inside `sshd.service` or a server started by cron) no longer get
  a restart button and no reminder: restarting never helps there. The status, the briefing and the restart
  result say "Service sshd restarted. Process screen (PID …, user) keeps using deleted files until it ends."
- `zypper ps` is now read as a table (`-ss`) instead of names only (`-sss`); unexpected output counts as unknown
- A very full state message is shortened by whole sections (each open item keeps its title), never in the middle
  of its formatting; long events are sent completely, spread over several messages if needed

**Upgrade:** none. After the update the bot sends its state message once and then removes the old status,
briefing, reminder, reboot and update messages (older than 48 hours: shortened to one line). The persistent
keyboard stays.

## 0.9.5 – 2026-11-04

- A locked package management (zypper exit code 7, e.g. `purge-kernels` right after boot) is no longer an
  error: a check waits up to 30 minutes and names who holds the lock (e.g. `purge-kernels.service`). Once it is
  free, the check starts over (fresh plan, mode and approval check), since the other operation may have changed
  the packages; if the lock is still held after 30 minutes, nothing is installed and the next check tries again
- Whether a reboot or shutdown is scheduled now comes from systemd-logind, so one scheduled or cancelled on the
  console (`shutdown -r +5`, `shutdown -c`) counts too. While one is scheduled, the status, the briefing and check
  results show "Reboot scheduled for …" with a "Cancel reboot" button instead of "Reboot pending", there is no
  reboot reminder, and checks, approvals, installs, `/update`, service restarts and a second reboot are refused.
  If logind does not answer, actions that change the system are refused, viewing keeps working
- Reboots, installs, service restarts and self-updates (also `twguard self-update` on the console) exclude each
  other through one rule table; a reboot is refused while the package management is locked. A self-update
  started by the bot is carried out by exactly one process; a second `twguard self-update` meanwhile is refused
- After a boot (detected by the boot ID, not by a bot restart), pending reboot and service items from before are
  closed; until zypper answers again, the status says "not yet queried" instead of "nothing open". A service
  restart cut off by the boot is reported as interrupted. The "is back" message names who still holds the
  package lock and is completed once it is free; it is kept and retried if Telegram is unreachable at start
- The status shows who currently holds the package lock
- `twguard check --dry-run` no longer edits a Telegram message while waiting for the package lock

**Upgrade:** none. The first bot start after the update records the boot ID and closes nothing.

## 0.9.4 – 2026-11-04

- Removing a package from `watch` now always needs approval, even if it also matches `routine_removals`
  (e.g. `php8-lang` with `php8*` and `*-lang`). A newer version of the same package replacing the old one stays
  routine
- `/mode observe` also stops a check that is already running, as long as it has not started installing; the
  mode is read again right before installing. If an install is already running, `/mode` says it will finish
- Installing and restarting services exclude each other: an install (timer, `/check`, approval) waits up to
  20 minutes for a running service restart and otherwise installs nothing (an approval stays valid); the restart
  button is refused while twguard is installing, also if the check got there first. A second check that reaches
  the install step while another one installs installs nothing and leaves the other run's lock alone
- The message shown while a self-update runs no longer promises a total deadline: twguard waits up to
  90 seconds for the new bot after tests and installation
- Versions are shown as "v0.9.3 (1a658c84)" instead of a 12-digit commit hash: `/version`, `twguard --version`,
  the update offer (now with the target version), the waiting message and the update results

## 0.9.3 – 2026-11-03

- The message shown while a self-update runs says that the bot does not answer meanwhile and that the original
  version is restored after 90 seconds at the latest if installing the update fails

## 0.9.2 – 2026-11-03

- After "Cancel reboot", the message offers the reboot again (with "1 day later") while it is still needed

## 0.9.1 – 2026-11-03

- Status and "Check now" ask `zypper needs-rebooting` first instead of waiting for the bot's check every 30 minutes,
  so a pending reboot shows up at once with its button; shown in the status, it counts as the first notification

## 0.9.0 – 2026-11-03

- `/mode auto` and `/mode observe` change the mode until the next restart of the bot, only when addressed (with
  `@botname` or as a reply); status and `/version` show a mode that differs from the configuration
- Log lines about slow or failed Telegram connections are fully translated ("response", "after")

## 0.8.9 – 2026-11-03

- Tests no longer call the real `zypper needs-rebooting` (failed in CI, where zypper is missing)
- CI uses the current major versions of the checkout and setup-python actions

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
