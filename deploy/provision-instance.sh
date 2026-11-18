#!/usr/bin/env bash
# Run as root on the server:  provision-instance.sh <name> <port>
# Creates (idempotently) the system user, directories, port file, systemd unit and a starter
# .env for one isolated Lister instance. Code and Python deps are installed by install-release.sh.
set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }
NAME="${1:-}"; PORT="${2:-}"
[[ -n "$NAME" && -n "$PORT" ]] || die "usage: $0 <name> <port>"
[[ "$NAME" =~ ^[a-z][a-z0-9]{0,15}$ ]] || die "name must be lowercase letters/digits, starting with a letter (max 16)"
[[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT >= 1024 && PORT <= 65535 )) || die "port must be 1024-65535"
(( EUID == 0 )) || die "run as root"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="/srv/lister/$NAME"
SVC_USER="lister-$NAME"
ENV_FILE="$ROOT/backend/.env"

# Two instances on one port would fail confusingly at start-up
for other in /etc/lister/*.service.env; do
  [[ -e "$other" && "$other" != "/etc/lister/$NAME.service.env" ]] || continue
  if grep -qx "LISTER_PORT=$PORT" "$other"; then die "port $PORT is already used by $(basename "$other" .service.env)"; fi
done

id "$SVC_USER" &>/dev/null || useradd --system --user-group --home-dir "$ROOT" --shell /usr/sbin/nologin "$SVC_USER"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 0750 "$ROOT" "$ROOT/backend" "$ROOT/vinted" "$ROOT/backups"

install -d -m 0755 /etc/lister
echo "LISTER_PORT=$PORT" > "/etc/lister/$NAME.service.env"
chmod 0644 "/etc/lister/$NAME.service.env"

install -m 0644 "$HERE/lister@.service" /etc/systemd/system/lister@.service
install -m 0644 "$HERE/lister-backup@.service" /etc/systemd/system/lister-backup@.service
install -m 0644 "$HERE/lister-backup@.timer" /etc/systemd/system/lister-backup@.timer
install -d -m 0755 /opt/lister
install -m 0755 "$HERE/backup-instance.sh" /opt/lister/backup-instance.sh
systemctl daemon-reload

if [[ ! -e "$ENV_FILE" ]]; then
  PASSWORD="$(head -c 48 /dev/urandom | base64 | tr -d '/+=\n' | head -c 24)"
  sed -e "s|@NAME@|$NAME|g" -e "s|@PASSWORD@|$PASSWORD|g" "$HERE/instance.env.template" > "$ENV_FILE"
  chown "$SVC_USER:$SVC_USER" "$ENV_FILE"
  chmod 0600 "$ENV_FILE"
  echo "created $ENV_FILE (fill in the Google/eBay app credentials; password is ACCESS_PASSWORD)"
else
  echo "kept existing $ENV_FILE"
fi
systemctl enable "lister@$NAME.service" "lister-backup@$NAME.timer" >/dev/null 2>&1 || true
echo "provisioned instance '$NAME' on port $PORT (user $SVC_USER, tree $ROOT)"
