#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Installiert twguard auf diesem Server (als root ausführen).
set -euo pipefail
cd "$(dirname "$0")"

rm -rf build
mkdir -p build
cp -r src build/src
find build/src -name __pycache__ -prune -exec rm -rf {} +
# Git-Stand einbauen; daran erkennt das OTA-Update, dass der neue Bot läuft
commit=$(git rev-parse --short=12 HEAD 2>/dev/null || echo unknown)
if [ -n "$(git status --porcelain --untracked-files=no 2>/dev/null)" ]; then
    commit="$commit+lokal"
fi
echo "COMMIT = \"$commit\"" > build/src/twguard/_build.py
# Ein einziges ausführbares Archiv, nur Standardbibliothek; -I ignoriert Umgebungsvariablen
python3 -m zipapp build/src -m 'twguard.cli:main' -p '/usr/bin/python3 -I' -o build/twguard.pyz
# Probelauf, bevor die laufende Version ersetzt wird
build/twguard.pyz --version

install -m 0755 build/twguard.pyz /usr/local/sbin/twguard.new
mv /usr/local/sbin/twguard.new /usr/local/sbin/twguard
install -m 0644 systemd/twguard-check.service systemd/twguard-check.timer systemd/twguard-bot.service /etc/systemd/system/
install -d -m 0700 /etc/twguard /var/lib/twguard
# Vorlage der aktuellen Version immer daneben legen, damit neue Einträge sichtbar werden
install -m 0600 config.example.toml /etc/twguard/config.default.toml
# Git-Arbeitskopie: von hier holt /update den neuen Stand
if [ -d .git ]; then
    sed -i "s|^# repo = .*|repo = \"$(pwd)\"|" /etc/twguard/config.default.toml
fi
if [ ! -e /etc/twguard/config.toml ]; then
    install -m 0600 /etc/twguard/config.default.toml /etc/twguard/config.toml
    echo "New configuration: /etc/twguard/config.toml – enter token, chat_id and allowed_user_ids."
else
    echo "Template of this version: /etc/twguard/config.default.toml"
    echo "  New entries: diff -u /etc/twguard/config.toml /etc/twguard/config.default.toml"
fi
systemctl daemon-reload
# Laufenden Bot auf die neue Version umstellen
systemctl try-restart twguard-bot.service

cat <<'TXT'
Next steps:
  twguard check --dry-run          # show the classification, changes nothing
  twguard test-message             # test Telegram
  systemctl enable --now twguard-check.timer twguard-bot.service
TXT
