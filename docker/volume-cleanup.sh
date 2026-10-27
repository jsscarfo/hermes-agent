#!/bin/sh
# Container-boot cleanup of approved stale paths under /opt/data.
# No arguments in production. Tests pass --data / --venv / --project.
# shellcheck shell=sh
set -eu
here=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
exec python3 -I "$here/volume_cleanup.py" "$@"
