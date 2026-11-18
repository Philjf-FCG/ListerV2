#!/usr/bin/env bash
# Consistent daily backup of one instance: SQLite online-backup of the database plus the OAuth
# token files, kept for 14 days. Run as the instance user by lister-backup@<name>.service.
set -euo pipefail
NAME="${1:?usage: $0 <name>}"
ROOT="/srv/lister/$NAME"
DEST="$ROOT/backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$DEST"
"$ROOT/venv/bin/python" - "$ROOT/backend/lister.db" "$DEST/lister.db" <<'PY'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
PY
for f in google_token.json ebay_token.json .env; do
  [[ -e "$ROOT/backend/$f" ]] && install -m 0600 "$ROOT/backend/$f" "$DEST/$f"
done
# keep the newest 14 backups
ls -1dt "$ROOT"/backups/*/ | tail -n +15 | xargs -r rm -rf
echo "backup written to $DEST"
