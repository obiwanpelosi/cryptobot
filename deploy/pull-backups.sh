#!/usr/bin/env bash
# Run on your Mac: copy the server's database backups into ./backups/ (git-ignored).
#   deploy/pull-backups.sh bot@YOUR_SERVER_IP
set -euo pipefail
if [[ $# -ne 1 ]]; then echo "usage: $0 bot@SERVER_IP" >&2; exit 1; fi
cd "$(dirname "$0")/.."
mkdir -p backups
rsync -av "$1:/opt/cryptobot/data/backups/" ./backups/
ls -lh backups | tail -5
