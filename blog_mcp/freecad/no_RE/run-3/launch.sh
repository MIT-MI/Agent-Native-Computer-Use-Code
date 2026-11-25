#!/bin/sh
# Launch the FreeCAD UI MCP server (JSON-RPC over stdin/stdout).
set -e
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$DIR/server.py"
