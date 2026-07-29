#!/bin/sh
# Take a compressed, verified pg_dump of the civic database.
# =========================================================
#
# There was no backup of any kind. The only durability mechanism was the
# `pgdata` named volume, and `docker compose down -v` — documented in the README
# two lines below the safe variant — destroys it. Compounding that, three of the
# integrity migrations are explicitly non-reversible, so `alembic downgrade`
# cannot get you out of a bad deploy either.
#
# Run one:
#     docker compose --profile backup run --rm backup
#
# Schedule it (host crontab, daily at 02:30):
#     30 2 * * * cd /path/to/civic-backend && docker compose --profile backup run --rm backup
#
# Restore — see RESTORE.md. Practise it before you need it.

set -eu

: "${POSTGRES_USER:=civic}"
: "${POSTGRES_DB:=civic}"
: "${BACKUP_RETENTION_DAYS:=14}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="/backups/civic_${STAMP}.dump"

echo "[backup] dumping ${POSTGRES_DB} as ${POSTGRES_USER} -> ${OUT}"

# Custom format (-Fc): compressed, and restorable selectively with pg_restore.
# --no-owner so the dump can be restored into a database owned by a different
# role, which is what happens on any restore-to-a-fresh-environment drill.
pg_dump \
  --host=db \
  --username="${POSTGRES_USER}" \
  --dbname="${POSTGRES_DB}" \
  --format=custom \
  --no-owner \
  --file="${OUT}"

# A dump you have not verified is not a backup. pg_restore --list fails on a
# truncated or corrupt archive, which is the failure mode that otherwise stays
# invisible until the day you need it.
if ! pg_restore --list "${OUT}" > /dev/null 2>&1; then
    echo "[backup] FAILED: ${OUT} is not a readable archive" >&2
    rm -f "${OUT}"
    exit 1
fi

SIZE="$(du -h "${OUT}" | cut -f1)"
echo "[backup] ok: ${OUT} (${SIZE}), archive verified"

# Prune old dumps. Runs after the new one is verified, so a failing backup
# never deletes the last good copy.
DELETED="$(find /backups -name 'civic_*.dump' -type f -mtime "+${BACKUP_RETENTION_DAYS}" -print -delete | wc -l)"
echo "[backup] pruned ${DELETED} dump(s) older than ${BACKUP_RETENTION_DAYS} days"
