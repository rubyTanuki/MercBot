#!/bin/sh
# Run on the server to deploy the latest code: pull, install dependencies, restart.
set -eu
cd /opt/mercbot
sudo -u mercbot git pull --ff-only
sudo -u mercbot uv pip install --python .venv/bin/python -r requirements.txt
sudo systemctl restart mercbot
sudo systemctl --no-pager status mercbot
