#!/usr/bin/env bash
# Deploy the COMMITTED code to the server. Run from anywhere inside the repo:
#   deploy/deploy.sh [user@host] [instance...]      (default: root@192.168.1.37, every instance)
# Uses `git archive`, so only tracked files are shipped - never .env, tokens or databases.
set -euo pipefail
HOST="${1:-root@192.168.1.37}"; shift || true
# Extra ssh options, e.g. SSH_OPTS="-i ~/.ssh/key -o IdentitiesOnly=yes"
read -ra EXTRA_SSH <<< "${SSH_OPTS:-}"
SSH=(ssh -o BatchMode=yes "${EXTRA_SSH[@]}" "$HOST")
cd "$(git rev-parse --show-toplevel)"

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "warning: you have uncommitted changes - only HEAD is deployed" >&2
fi
REV="$(git rev-parse --short HEAD)"
REL="/opt/lister/releases/$REV"
echo "deploying $REV to $HOST"

"${SSH[@]}" "rm -rf '$REL' && mkdir -p '$REL'"
git archive --format=tar HEAD backend deploy | "${SSH[@]}" "tar -x -C '$REL'"

INSTANCES="$*"
if [[ -z "$INSTANCES" ]]; then
  INSTANCES="$("${SSH[@]}" "cd /etc/lister && ls *.service.env | sed 's/\.service\.env\$//'" | tr '\n' ' ')"
fi
[[ -n "${INSTANCES// }" ]] || { echo "no instances found - run provision-instance.sh on the server first" >&2; exit 1; }

"${SSH[@]}" "bash '$REL/deploy/install-release.sh' '$REL' $INSTANCES"
"${SSH[@]}" "ls -1dt /opt/lister/releases/*/ | tail -n +4 | xargs -r rm -rf"   # keep the last 3 releases
echo "done: $REV on $INSTANCES"
