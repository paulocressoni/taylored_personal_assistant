# Backups (M28)

Encrypted, cron-driven backups of the production stack, plus a non-destructive
restore drill you run monthly. Two scripts share one config file:

| File | Purpose |
| --- | --- |
| `backup.py` | Produce one dated, age-encrypted backup per run. |
| `restore.py` | Decrypt + restore a dated backup (drill-safe by default). |
| `backup.conf.example` | Template for the real `backup.conf`. |
| `backup.crontab.example` | Root cron line (installed to `/etc/cron.d/`). |
| `BACKUP.md` | This runbook. |

Both scripts are pure-stdlib Python 3.10+ and require the `age` binary, the
Docker CLI with the compose plugin, GNU `tar`, and root (to read
`/var/lib/docker/volumes`). Never commit `backup.conf` - it references paths,
not secrets, but keep it host-local alongside `.env.prod`.

## What is backed up

| Item | Source | Method |
| --- | --- | --- |
| `app-postgres` | `app-db` service (`assistant` DB) - conversation memory + future `scheduled_items` | `pg_dump -Fc` |
| `langfuse-postgres` | `postgres` service (Langfuse org/project/user) | `pg_dump -Fc` |
| `VOLUMES` | ClickHouse traces, MinIO media (Chroma volume added when it exists) | volume tar |
| `OAUTH_TOKEN_DIR` | Google/Spotify refresh tokens (slot; empty until the feature ships) | host-dir tar |

**Back up the OAuth refresh tokens without fail.** Losing them means redoing
the Google Calendar AND Spotify consent flows from scratch. Wire
`OAUTH_TOKEN_DIR` the day that feature lands.

## Encryption model

* Each item is hashed (SHA-256), then encrypted with `age -R <recipient>`.
* The **recipient** (public) key lives on the backup box - it can encrypt but
  **cannot decrypt**. The box is useless to an attacker who steals it.
* The matching **identity** (private) key lives on offline media and is mounted
  only during a verify/restore drill or a real recovery.
* `manifest.txt` stores the SHA-256 of each item's *decrypted* contents, so a
  drill proves integrity without trusting the `.age` files' own metadata.

## One-time setup (DeskMini, as root)

```bash
# 1. age + first backup dir
apt-get update && apt-get install -y age
mkdir -p /var/backups/taylored-assistant

# 2. keypair
age-keygen -o /root/.config/age/backup-identity.txt
age-keygen -y /root/.config/age/backup-identity.txt \
  | tee /var/backups/taylored-assistant/backup-recipient.pub >/dev/null

# 3. COPY /root/.config/age/backup-identity.txt TO OFFLINE MEDIA, then:
shred -u /root/.config/age/backup-identity.txt

# 4. config (repo checked out at /opt/taylored-personal-assistant below)
cd /opt/taylored-personal-assistant
cp infra/backup/backup.conf.example infra/backup/backup.conf
nano infra/backup/backup.conf   # set AGE_RECIPIENT; leave AGE_IDENTITY empty

# 5. cron - edit REPO_ROOT if your checkout differs, then:
cp infra/backup/backup.crontab.example /etc/cron.d/taylored-backup
systemctl restart cron
```

## Running

All commands run from the repo root (the conf's compose paths are
repo-relative).

```bash
# resolved config only - no writes
python3 infra/backup/backup.py --config infra/backup/backup.conf --dry-run

# one manual run (stack must be up: `make prod-up`)
python3 infra/backup/backup.py --config infra/backup/backup.conf

# list dated backups + manifests
python3 infra/backup/restore.py --config infra/backup/backup.conf list

# log output lands here (see also cron's stderr redirect)
tail -f /var/backups/taylored-assistant/backup.log
```

Expected layout after a run:

```
/var/backups/taylored-assistant/daily/2026-09-09/
  app-postgres.dump.age
  langfuse-postgres.dump.age
  volume-<...>_clickhouse_data.tar.gz.age
  volume-<...>_minio_data.tar.gz.age
  manifest.txt
```

## Monthly verify drill (proves your backups are good)

Do this once a month. It needs the offline identity mounted.

```bash
# mount the USB stick with backup-identity.txt, then e.g.:
AGE_ID=/mnt/usb/backup-identity.txt

# 1. decrypt + hash the newest backup vs its manifest
sed -i "s|^AGE_IDENTITY=.*|AGE_IDENTITY=$AGE_ID|" infra/backup/backup.conf
python3 infra/backup/backup.py --config infra/backup/backup.conf --verify

# 2. restore into DISPOSABLE targets (touches nothing live)
python3 infra/backup/restore.py --config infra/backup/backup.conf restore \
  --date 2026-09-09

# 3. sanity-check the drill database contents
docker compose -p taylored-assistant-prod -f infra/compose/docker-compose.base.yml \
  -f infra/compose/docker-compose.prod.yml --env-file infra/compose/.env.prod \
  exec -T app-db psql -U assistant -d assistant_restore_2026-09-09 \
  -c "\dt" -c "SELECT count(*) FROM checkpoints;"

# 4. clean up the disposable drill artifacts (optional but tidy)
docker volume rm -f <clickhouse>_restore_2026-09-09 <minio>_restore_2026-09-09
docker compose -p taylored-assistant-prod -f ... exec -T app-db \
  psql -U assistant -d postgres -c "DROP DATABASE assistant_restore_2026-09-09;"

# 5. remove the identity line again
sed -i "s|^AGE_IDENTITY=.*|AGE_IDENTITY=|" infra/backup/backup.conf
umount /mnt/usb
```

If step 1 reports SHA-256 MISMATCH, stop - that backup is bad. Investigate
before the next cron run overwrites anything, and confirm the preceding day's
backup verifies instead.

## Real recovery (only after data loss)

```bash
# 1. stop the whole stack so nothing writes during the restore
docker compose -p taylored-assistant-prod -f infra/compose/docker-compose.base.yml \
  -f infra/compose/docker-compose.prod.yml --env-file infra/compose/.env.prod stop

# 2. mount identity, point AGE_IDENTITY at it, then:
python3 infra/backup/restore.py --config infra/backup/backup.conf restore \
  --date 2026-09-09 --to-live --yes

# 3. restart and let the app recreate the Postgres checkpointer schema if needed
docker compose -p taylored-assistant-prod -f infra/compose/docker-compose.base.yml \
  -f infra/compose/docker-compose.prod.yml --env-file infra/compose/.env.prod start
```

`--to-live` replaces existing objects in the real databases/volumes
(`pg_restore --clean --if-exists`), so it is destructive - the `--yes` flag is
the confirmation guard.

## Extending the backup (when a new feature lands)

1. Chroma volume - add its full volume name to VOLUMES in backup.conf
(comma-separated). Nothing else changes.
2. OAuth tokens - set OAUTH_TOKEN_DIR to the host dir holding the Google/
Spotify refresh tokens. Do this the same day the feature ships.
3. Langfuse media (if MinIO is replaced) - swap the volume name in VOLUMES.
4. Re-run the verify drill once after each change.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `volume '...' not found - is the stack up?` | Volume name in `VOLUMES` is wrong or the stack never ran. Check `docker volume ls` for the exact `taylored-assistant-prod_...` names. |
| `AGE_RECIPIENT is empty ... refusing` | Run without `--allow-plaintext`; set `AGE_RECIPIENT` to the `.pub` file. |
| `AGE_IDENTITY is empty` | Restore/verify requires the identity - mount the offline copy and set `AGE_IDENTITY`. |
| `age: command not found` | `apt-get install -y age`. Root cron also needs `age` on its `PATH`. |
| `psql: error ... connection` | Service is not running - `make prod-up` first. |
| Backup exits `1` after `item failed` | One item failed; others were still produced. Read the log line above the summary - common cause is a service being down or a new volume not yet created. |
| Cron never runs | `systemctl status cron`; check `/var/backups/taylored-assistant/backup.log`; confirm `REPO_ROOT` in `/etc/cron.d/taylored-backup` matches the checkout. |
| `permission denied` on volumes | Script must run as root (cron user is `root` in the example). |
