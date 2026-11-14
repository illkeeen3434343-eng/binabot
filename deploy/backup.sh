#!/bin/sh
# Consistent online copy of the database (safe while the bot runs). Cron example:
#   15 3 * * * /home/vboxuser/binabot/deploy/backup.sh
cd "$(dirname "$0")/.." || exit 1
mkdir -p backups
sqlite3 data/binabot.sqlite3 ".backup 'backups/binabot-$(date +%F).sqlite3'"
ls -1t backups/binabot-*.sqlite3 | tail -n +15 | xargs -r rm --
