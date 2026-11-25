#!/bin/sh
# Launcher for the FreeCAD MCP server.
# The server speaks MCP JSON-RPC on stdin/stdout and writes diagnostics to stderr.
DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
export PYTHONPATH="$DIR/deps:$DIR:${PYTHONPATH:-}"
exec python3 "$DIR/server.py"
