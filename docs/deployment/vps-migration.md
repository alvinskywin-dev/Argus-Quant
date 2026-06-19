# Moving the bot to a new VPS (without losing data)

The **code** lives on GitHub, so `git clone` brings it over. But two things are
**not** in git and must be carried by hand:

- `.env` — config + secrets
- the **Postgres data** (your signals + history) — it lives in a Docker volume,
  not in the repo

Two helper scripts automate the whole move so you only run one command on each
side.

## 1. On the OLD server — export

```bash
cd ~/futures-signal-bot
bash scripts/migrate_export.sh
```

This creates `migrate_bundle.tar.gz` (`.env` + a full DB dump + `backups/`).

> ⚠️ Do **not** run `docker compose down -v` before exporting — the `-v` flag
> deletes the database volume.

## 2. Copy the bundle to the NEW server

```bash
scp migrate_bundle.tar.gz user@NEW_VPS:~/
```

## 3. On the NEW server — import

```bash
# install Docker if needed
curl -fsSL https://get.docker.com | sh

# clone the code
git clone git@github.com:alvinskywin-dev/Argus-Quant.git futures-signal-bot
cd futures-signal-bot

# move the bundle into the repo, then:
mv ~/migrate_bundle.tar.gz .
bash scripts/migrate_import.sh
```

The import script extracts the bundle, starts Postgres + Redis, restores the
database, builds and starts the bot, then prints the restored signal count so you
can confirm it matches the old server.

## Notes

- **Redis** is just a cache — it is rebuilt automatically, nothing to migrate.
- The DB dump uses `--clean --if-exists`, so re-running the import is safe (it
  replaces the data rather than erroring on existing tables).
- The new VPS's IP must not be blocked by Binance, or the bot will fail its
  startup Binance check.
