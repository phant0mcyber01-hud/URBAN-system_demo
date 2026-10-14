#!/usr/bin/env bash
# Daily backup of what cannot be rebuilt: the history (SQLite, copied safely while the platform runs),
# the operator accounts and the device registry. Keys are left out on purpose.
#   crontab -u urban -e:   0 3 * * * /opt/urban/deploy/backup.sh
set -euo pipefail
APP=${URBAN_HOME:-/opt/urban}
OUT=${URBAN_BACKUPS:-/var/backups/urban}
STAMP=$(date +%F)
mkdir -p "$OUT"
sqlite3 "$APP/data/urban.db" ".backup '$OUT/urban-$STAMP.db'"
cp "$APP/data/users.json" "$OUT/users-$STAMP.json"
cp "$APP/data/pki/devices.json" "$OUT/devices-$STAMP.json"
chmod 600 "$OUT"/*
find "$OUT" -type f -mtime +30 -delete
echo "backup $STAMP: $(du -sh "$OUT" | cut -f1) in $OUT"
