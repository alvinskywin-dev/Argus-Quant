#!/usr/bin/env bash
#
# migrate_export.sh — export everything needed to move this deployment to a new
# host. Run this on the OLD server (from anywhere; it cd's to the repo root).
#
# It produces a single `migrate_bundle.tar.gz` containing:
#   - .env            (config + secrets — NOT in git)
#   - migrate_db.sql  (full Postgres dump: schema + all data)
#   - backups/        (existing .sql safety dumps, if any)
#
# Copy that one file to the new server and run scripts/migrate_import.sh there.
#
set -euo pipefail

cd "$(dirname "$0")/.."

DUMP="migrate_db.sql"
BUNDLE="migrate_bundle.tar.gz"

echo "==> Dumping database (schema + data)..."
if ! docker compose exec -T postgres pg_dump -U signals -d signals --clean --if-exists > "$DUMP"; then
    echo "❌ Could not dump the database. Is Postgres running?"
    echo "   Start it first:  docker compose up -d postgres"
    rm -f "$DUMP"
    exit 1
fi
echo "    wrote $DUMP ($(du -h "$DUMP" | cut -f1))"

if [ ! -f .env ]; then
    echo "❌ .env is missing — the bot cannot start on the new host without it."
    exit 1
fi

echo "==> Bundling .env + dump$([ -d backups ] && echo ' + backups/')..."
files=(.env "$DUMP")
[ -d backups ] && files+=(backups)
tar czf "$BUNDLE" "${files[@]}"

echo "✅ Done -> $BUNDLE ($(du -h "$BUNDLE" | cut -f1))"
echo
echo "Next: copy it to the new server, e.g."
echo "    scp $BUNDLE user@NEW_VPS:~/"
echo "then clone the repo there, move $BUNDLE into it, and run scripts/migrate_import.sh"
