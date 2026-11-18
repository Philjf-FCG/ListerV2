#!/usr/bin/env bash
# Move one user's data from this Windows PC to their instance on the server (run from Git Bash):
#   deploy/migrate-data.sh <instance> [user@host]
# Copies a consistent snapshot of lister.db, the Google/eBay token files, thumbnails, rotation
# backups and the Vinted export, rewrites the Windows photo paths in the database to the
# server's, and restarts the instance. The local copies are left untouched.
# Override locations with VINTED_SRC (default /c/Vinted/Data/listings).
set -euo pipefail
NAME="${1:?usage: $0 <instance> [user@host]}"; HOST="${2:-root@192.168.1.37}"
cd "$(git rev-parse --show-toplevel)/backend"
ROOT="/srv/lister/$NAME"
VINTED_SRC="${VINTED_SRC:-/c/Vinted/Data/listings}"
# Extra ssh options, e.g. SSH_OPTS="-i ~/.ssh/key -o IdentitiesOnly=yes"
read -ra EXTRA_SSH <<< "${SSH_OPTS:-}"
SSH=(ssh -o BatchMode=yes "${EXTRA_SSH[@]}" "$HOST")

if [[ -x .venv/Scripts/python.exe ]]; then PY=.venv/Scripts/python.exe; elif [[ -x .venv/bin/python ]]; then PY=.venv/bin/python; else PY=python; fi
STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT

echo "-- snapshotting the database (consistent even if the local app is running)"
"$PY" - lister.db "$STAGE/lister.db" <<'PY'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
PY
for f in google_token.json ebay_token.json; do [[ -e "$f" ]] && cp "$f" "$STAGE/$f"; done

"${SSH[@]}" "test -d '$ROOT/backend'" || { echo "instance '$NAME' is not provisioned on $HOST" >&2; exit 1; }
echo "-- stopping the instance"
"${SSH[@]}" "systemctl stop lister@$NAME.service"

echo "-- copying database, tokens, thumbnails, rotation backups"
(cd "$STAGE" && tar -c .) | "${SSH[@]}" "tar -x -C '$ROOT/backend'"
for d in thumbnails rotation_backups vinted_photo_cache; do
  [[ -d "$d" ]] && tar -c "$d" | "${SSH[@]}" "tar -x -C '$ROOT/backend'"
done

echo "-- copying the Vinted export ($VINTED_SRC)"
if [[ -d "$VINTED_SRC" ]]; then
  "${SSH[@]}" "mkdir -p '$ROOT/vinted'"
  tar -c -C "$(dirname "$VINTED_SRC")" "$(basename "$VINTED_SRC")" | "${SSH[@]}" "tar -x -C '$ROOT/vinted'"
else
  echo "   (not found - skipped)"
fi

echo "-- rewriting Windows photo paths in the database"
"${SSH[@]}" "chown -R lister-$NAME:lister-$NAME '$ROOT' && runuser -u lister-$NAME -- '$ROOT/venv/bin/python' '$ROOT/backend/migrate_paths.py' --db '$ROOT/backend/lister.db' \
  --map 'C:\Vinted\Data=$ROOT/vinted' --map 'C:\DevProjects\lister2\backend=$ROOT/backend'"

echo "-- starting the instance"
"${SSH[@]}" "systemctl start lister@$NAME.service"
echo "done"
