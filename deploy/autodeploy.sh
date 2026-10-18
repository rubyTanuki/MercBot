#!/bin/sh
# Run every minute by mercbot-autodeploy.timer: deploys origin/main if it has moved on.
set -eu
cd /opt/mercbot
as_bot() { sudo -u mercbot -H "$@"; }

as_bot git fetch --quiet origin main
current=$(as_bot git rev-parse HEAD)
latest=$(as_bot git rev-parse origin/main)
[ "$current" = "$latest" ] && exit 0

echo "deploying $(as_bot git log -1 --format='%h %s' "$latest")"
# Fast-forward only: a server with local edits stops here rather than deploying a merge.
as_bot git merge --ff-only --quiet origin/main
as_bot uv pip install --quiet --python .venv/bin/python -r requirements.txt
systemctl restart mercbot
echo "deployed; mercbot restarted"
