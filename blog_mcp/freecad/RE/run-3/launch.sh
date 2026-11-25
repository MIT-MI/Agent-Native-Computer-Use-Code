#!/bin/sh
# Launcher for the FreeCAD MCP server.  All paths are relative to this script.
set -e
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PY=/usr/bin/python3
if [ ! -x "$PY" ]; then
    PY=$(command -v python3)
fi
exec "$PY" "$DIR/server.py" "$@"
