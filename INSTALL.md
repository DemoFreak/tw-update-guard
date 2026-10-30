# Installation (short version)

🇩🇪 [Deutsche Fassung](INSTALL.de.md)

For each server, as root. Requirements: openSUSE Tumbleweed, `git`, Python ≥ 3.11 (part of Tumbleweed), a Telegram account. twguard needs no other packages.

Messages are in English by default; German is available (step 3).

## 1. Create a Telegram bot

1. With **@BotFather**: `/newbot` → name and username (e.g. `server1_updates_bot`) → note the token.
2. Also with @BotFather: `/setprivacy` → choose the bot → **Disable**. Otherwise the bot only sees commands addressed to it in a group.
3. Create a group (with several servers: one for all of them) and add the bot. Optionally grant the admin right **Delete messages**, then the bot cleans up commands and old messages. Telegram often turns the group into a supergroup with a new ID; twguard notices this and reports the new ID.

## 2. Install

```bash
git clone https://github.com/DemoFreak/tw-update-guard.git /root/tw-update-guard
cd /root/tw-update-guard && ./install.sh
```

`install.sh` builds `twguard` as a single file in `/usr/local/sbin`, copies the systemd units and creates `/etc/twguard/config.toml` (observe mode: checks and reports, installs nothing). It does not start any service yet. Running it again is harmless and shows the next steps again.

## 3. Configure

1. Put the token into `/etc/twguard/config.toml` under `[telegram]`.
2. Write `/start@server1_updates_bot` in the group, then:
   ```bash
   twguard telegram-ids
   ```
   Enter the chat ID shown as `chat_id` and your user ID under `allowed_user_ids`.
3. Set `help_full = true` on exactly one server; only that one answers `/help` sent to all bots.
4. For German output set `language = "de"` in `[general]`.

## 4. Test and start

```bash
twguard check --dry-run      # show the classification; changes nothing, sends nothing
twguard test-message         # test Telegram
systemctl enable --now twguard-check.timer twguard-bot.service
```

Write `/status` in the group; every server answers. `/keyboard@server1_updates_bot` shows a keyboard with the most important commands.

Once the reports look right for a while, set `mode = "auto"` in `[general]` and run `systemctl restart twguard-bot`. From then on twguard installs routine updates by itself.

## Updating

In Telegram `/update` (or the “⬆️ Update” key): the bot shows the new commits and installs them after confirmation, running the tests first and falling back to the old version on failure. Manually:

```bash
cd /root/tw-update-guard && git pull --ff-only && ./install.sh
```

New configuration entries: `diff -u /etc/twguard/config.toml /etc/twguard/config.default.toml`.

## Removing

```bash
systemctl disable --now twguard-check.timer twguard-bot.service
rm /etc/systemd/system/twguard-{check.service,check.timer,bot.service} /usr/local/sbin/twguard*
systemctl daemon-reload
rm -r /etc/twguard /var/lib/twguard    # configuration and state
```
