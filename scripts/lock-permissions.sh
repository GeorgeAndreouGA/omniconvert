#!/usr/bin/env bash
# Makes this folder private to you: folders 700, files 600, scripts 700, and .env 600. Needs no sudo.
#
#   bash scripts/lock-permissions.sh
#
# Run it after copying the folder to a new machine, because scp and git don't keep these. It matters even
# on a machine only you use: `docker compose` runs what's written here as root, in effect, so anyone who
# can edit these files can take the machine, and .env holds your token. preflight.sh fails if it isn't done.
set -euo pipefail
cd "$(dirname "$0")/.."

find . -type d -exec chmod 700 {} +
find . -type f -exec chmod 600 {} +
chmod 700 scripts/*.sh scripts/*.py
echo "Locked: only $(id -un) can read or change anything in $(pwd)"
