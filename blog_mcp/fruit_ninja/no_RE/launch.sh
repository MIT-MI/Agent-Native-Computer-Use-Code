#!/bin/bash
# Fruit Ninja HD MCP server launcher.
cd "$(dirname "$0")"
export DISPLAY="${DISPLAY:-:7}"
exec python3 server.py
