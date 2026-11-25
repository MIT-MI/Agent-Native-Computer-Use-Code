#!/bin/sh
# Fruit Ninja HD MCP launcher. Paths relative to this script.
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export DISPLAY=${DISPLAY:-:7}
exec python3 "$DIR/server.py"
