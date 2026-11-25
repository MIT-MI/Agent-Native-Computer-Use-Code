#!/usr/bin/env bash
set -eu
mkdir -p /logs/verifier
printf '0\n' > /logs/verifier/reward.txt
ln -sfn /opt/reef/node_modules /tests/node_modules
node /tests/judge.mjs freecad-lid-vent-pattern showcase
