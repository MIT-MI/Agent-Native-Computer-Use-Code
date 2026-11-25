#!/bin/bash
# The FreeCAD build used by the tasks' GUI runs, installed as a normal user would.
set -euo pipefail
V=0.20.2+dfsg1-4+deb12u1
apt-get update
apt-get install -y freecad-python3=$V freecad-common=$V libfreecad-python3-0.20=$V
# The metapackage adds only the desktop entry, MIME data and docs the lifecycle helper reads.
apt-get install -y --no-install-recommends freecad=$V
rm -rf /var/lib/apt/lists/*
