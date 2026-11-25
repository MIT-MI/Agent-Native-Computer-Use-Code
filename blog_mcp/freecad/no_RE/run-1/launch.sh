#!/bin/sh
# Launch the FreeCAD MCP server (JSON-RPC over stdio).
DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 -u "$DIR/server.py"
