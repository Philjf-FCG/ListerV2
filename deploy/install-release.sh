#!/usr/bin/env bash
# Run as root on the server (deploy.sh calls this):  install-release.sh <release-dir> <name>...
# Replaces each instance's code and Python deps, then restarts it and waits until it is healthy.
# Only app/, requirements.txt and migrate_paths.py are replaced: .env, the database, OAuth
# tokens, thumbnails, rotation backups and the Vinted data are never touched.
set -euo pipefail
die() { echo "error: $*" >&2; exit 1; }
REL="${1:-}"; shift || true
[[ -d "$REL/backend/app" ]] || die "usage: $0 <release-dir> <name>..."
(( $# )) || die "no instances given"
(( EUID == 0 )) || die "run as root"

for NAME in "$@"; do
  ROOT="/srv/lister/$NAME"; SVC_USER="lister-$NAME"
  [[ -d "$ROOT/backend" ]] || die "instance '$NAME' is not provisioned (run provision-instance.sh first)"
  echo "== $NAME"
  rm -rf "$ROOT/backend/app"
  cp -a "$REL/backend/app" "$ROOT/backend/app"
  cp -a "$REL/backend/requirements.txt" "$REL/backend/migrate_paths.py" "$ROOT/backend/"
  chown -R "$SVC_USER:$SVC_USER" "$ROOT/backend/app" "$ROOT/backend/requirements.txt" "$ROOT/backend/migrate_paths.py"

  as_user() { runuser -u "$SVC_USER" -- env HOME="$ROOT" "$@"; }
  [[ -x "$ROOT/venv/bin/python" ]] || as_user python3 -m venv "$ROOT/venv"
  as_user "$ROOT/venv/bin/pip" install --quiet --upgrade pip
  as_user "$ROOT/venv/bin/pip" install --quiet -r "$ROOT/backend/requirements.txt"

  systemctl restart "lister@$NAME.service"
  PORT="$(sed -n 's/^LISTER_PORT=//p' "/etc/lister/$NAME.service.env")"
  healthy=0
  for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then healthy=1; break; fi
    sleep 1
  done
  if (( ! healthy )); then
    journalctl -u "lister@$NAME.service" -n 40 --no-pager || true
    die "$NAME did not become healthy on port $PORT"
  fi
  echo "$NAME healthy on :$PORT"
done
