#!/bin/sh
# Launcher for the FreeCAD GUI MCP server.  Paths are relative to this script.
set -e
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PYTHONPATH="$DIR:$DIR/deps${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -u "$DIR/server.py" "$@"
