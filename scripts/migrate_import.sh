#!/usr/bin/env bash
#
# migrate_import.sh — restore a deployment exported by scripts/migrate_export.sh
# onto a fresh host. Run this on the NEW server, in the cloned repo root, after
# copying `migrate_bundle.tar.gz` into it.
#
# Steps: extract bundle -> start postgres+redis -> restore DB -> build+run bot.
# Postgres data lives in a Docker volume, so this is what actually carries your
# signals/history across hosts (git only carries the code).
#
set -euo pipefail

cd "$(dirname "$0")/.."

BUNDLE="migrate_bundle.tar.gz"
DUMP="migrate_db.sql"

if [ ! -f "$BUNDLE" ]; then
    echo "❌ $BUNDLE not found in the repo root."
    echo "   Copy it here first (from the old server), then re-run."
    exit 1
fi

echo "==> Extracting bundle..."
tar xzf "$BUNDLE"

if [ ! -f .env ]; then
    echo "❌ .env was not in the bundle — the bot cannot start without it."
    exit 1
fi
if [ ! -f "$DUMP" ]; then
    echo "❌ $DUMP was not in the bundle — nothing to restore."
    exit 1
fi

echo "==> Starting Postgres + Redis..."
docker compose up -d postgres redis

echo "==> Waiting for Postgres to accept connections..."
ready=""
for _ in $(seq 1 30); do
    if docker compose exec -T postgres pg_isready -U signals >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 2
done
if [ -z "$ready" ]; then
    echo "❌ Postgres did not become ready within 60s. Check: docker compose logs postgres"
    exit 1
fi

echo "==> Restoring database..."
docker compose exec -T postgres psql -U signals -d signals < "$DUMP"

echo "==> Building + starting the bot..."
docker compose up -d --build bot

echo "==> Verifying..."
sleep 5
docker compose exec -T postgres psql -U signals -d signals \
    -c "SELECT count(*) AS signals_restored FROM signals;"

echo "✅ Migration done. Check the bot:  docker compose logs bot --tail 40"
