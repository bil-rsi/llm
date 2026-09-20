#!/bin/sh
# Nightly logical backup (pg_dump custom format) with retention. Also runs on demand: `backup.sh now`.
set -eu
: "${BACKUP_AT:=0230}" "${BACKUP_KEEP:=14}"
export PGPASSWORD="$(tr -d '\r\n' < /run/secrets/pg_backup)"
dump() {
  f="/backups/aiplatform-$(date -u +%Y%m%d-%H%M%S).dump"
  pg_dump -h postgres -U aimem_backup -d aiplatform -Fc --no-owner -f "$f.part" && mv "$f.part" "$f"
  pg_restore -l "$f" > /dev/null   # structural check of the archive
  echo "backup ok: $f ($(wc -c < "$f") bytes)"
  ls -1t /backups/aiplatform-*.dump 2>/dev/null | tail -n +"$((BACKUP_KEEP + 1))" | while read -r old; do rm -f -- "$old"; echo "pruned $old"; done
}
if [ "${1:-}" = "now" ]; then dump; exit 0; fi
echo "backup scheduler: daily at ${BACKUP_AT} UTC, keep ${BACKUP_KEEP}"
last=""
while true; do
  now="$(date -u +%H%M)"; day="$(date -u +%Y%m%d)"
  if [ "$now" = "$BACKUP_AT" ] && [ "$last" != "$day" ]; then dump || echo "backup FAILED" >&2; last="$day"; fi
  sleep 30
done
