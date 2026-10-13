#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
python3 app_server.py --port 8000
