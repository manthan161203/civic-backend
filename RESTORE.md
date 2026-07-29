# Backup and restore

## Why this file exists

Until recently there was no backup of any kind. The only durability mechanism was
the `pgdata` named volume — and `docker compose down -v` destroys it. Three of
the integrity migrations are explicitly non-reversible, so `alembic downgrade`
cannot rescue a bad deploy either. Without a dump, a mistake was unrecoverable.

## Taking a backup

```bash
docker compose --profile backup run --rm backup
```

Writes a compressed custom-format dump to the `backups` volume and **verifies it**
with `pg_restore --list` before pruning anything older than
`BACKUP_RETENTION_DAYS` (default 14). A dump that fails verification is deleted
and the script exits non-zero, so a broken backup never silently replaces a good
one.

Schedule it from the host crontab:

```cron
30 2 * * * cd /path/to/civic-backend && docker compose --profile backup run --rm backup >> /var/log/civic-backup.log 2>&1
```

Copy the dumps off this host. A backup on the same disk as the database is not a
backup — it is a second copy of the same failure domain.

```bash
docker run --rm -v civic_backups:/backups -v "$PWD/offsite":/out alpine \
  sh -c 'cp /backups/*.dump /out/'
```

## Listing what you have

```bash
docker compose --profile backup run --rm --entrypoint sh backup \
  -c 'ls -lh /backups/'
```

## Restoring

### Practise first, into a scratch database

This is the drill. Run it after any change to the backup setup — a restore path
you have never exercised is a guess.

```bash
DUMP=/backups/civic_YYYYMMDDTHHMMSSZ.dump

docker compose --profile backup run --rm --entrypoint sh backup -c "
  psql -h db -U civic -d postgres -c 'CREATE DATABASE civic_restore_drill;'
  pg_restore -h db -U civic -d civic_restore_drill --no-owner $DUMP
  psql -h db -U civic -d civic_restore_drill -c 'select count(*) from users;'
  psql -h db -U civic -d civic_restore_drill -c 'select version_num from alembic_version;'
  psql -h db -U civic -d postgres -c 'DROP DATABASE civic_restore_drill;'
"
```

Check that `alembic_version` matches the revision the application code expects.
A dump from before a migration restores a schema the current code cannot run
against — you must either restore the matching code revision or run
`alembic upgrade head` afterwards.

### Real restore, over the live database

Stop everything that writes first. `pg_restore` into a database with open
connections leaves you with a half-restored schema.

```bash
# 1. Stop the writers, keep the database up
docker compose stop api jobs

# 2. Recreate the database empty
docker compose exec db psql -U civic -d postgres -c \
  "DROP DATABASE civic WITH (FORCE);"
docker compose exec db psql -U civic -d postgres -c \
  "CREATE DATABASE civic OWNER civic;"

# 3. Restore
docker compose --profile backup run --rm --entrypoint sh backup -c \
  "pg_restore -h db -U civic -d civic --no-owner /backups/civic_YYYYMMDDTHHMMSSZ.dump"

# 4. Bring the schema up to the code's expectation
docker compose run --rm migrate

# 5. Start the writers
docker compose up -d api jobs
docker compose logs -f api | head -30
```

### What the dump does not include

Uploaded photos live in the `uploads_data` volume, not in Postgres. When
`STORAGE_BACKEND=local`, restoring the database without the uploads volume gives
you issues whose `before_photos` URLs 404. Back both up together:

```bash
docker run --rm -v civic_uploads_data:/uploads -v "$PWD/offsite":/out alpine \
  tar czf /out/uploads_$(date -u +%Y%m%d).tar.gz -C /uploads .
```

## Recovery objectives

The current setup gives you, at a daily cadence:

- **RPO** (data you can lose): up to 24 hours.
- **RTO** (time to recover): minutes — the dump of a small database restores fast.

If 24 hours of lost reports is unacceptable, daily `pg_dump` is the wrong tool:
turn on WAL archiving for point-in-time recovery, or move to a managed Postgres
with continuous backup. That is a deployment decision, not an application one.
