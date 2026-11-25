#!/usr/bin/env bash
# Full Fruit Ninja Builder experiment:
#   1. Two builds run in parallel:
#        g1-re     Builder with the RE toolbox, memtool and fn-memd (3 h default)
#        g2-no-re  Builder locked without RE (agent user, no-re image, fn-appd only; 3 h default)
#   2. Three groups play N Arcade rounds (default 3), one round at a time, with rotating group order:
#        g1-re     MCP built with RE
#        g2-no-re  MCP built without RE
#        g3-gui    Screenshot + drag GUI player
#
# Model API keys are read from the environment (see config/*.json).
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT=""
ROUNDS=3
EXTRA_BUILD=()
EXTRA_SOLVE=()

while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --rounds) ROUNDS="$2"; shift 2 ;;
    --seconds) EXTRA_BUILD+=(--seconds "$2"); shift 2 ;;
    --solve-seconds) EXTRA_SOLVE+=(--seconds "$2"); shift 2 ;;
    --smoke) EXTRA_BUILD+=(--smoke); EXTRA_SOLVE+=(--smoke); shift ;;
    --mock) EXTRA_BUILD+=(--mock); EXTRA_SOLVE+=(--mock); shift ;;
    -h|--help)
      echo "usage: $0 --out DIR [--rounds N] [--seconds BUILD_SEC] [--solve-seconds SOLVE_SEC] [--smoke] [--mock]"
      exit 0
      ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$OUT" ]; then
  OUT="$ROOT/.local/runs/experiment-$(date -u +%Y%m%dT%H%M%SZ)"
fi
mkdir -p "$OUT"
RUN="$(cd "$OUT" && pwd)"

cd "$ROOT" || exit 1
git -C "$ROOT" rev-parse HEAD > "$RUN/commit" 2>/dev/null || echo "unknown" > "$RUN/commit"
cli() { node --import tsx src/cli.ts "$@"; }

date -Is > "$RUN/started"
cli build --variant re --out "$RUN/build-g1-re" "${EXTRA_BUILD[@]}" > "$RUN/build-g1-re.log" 2>&1 &
PID_RE=$!
cli build --variant no-re --out "$RUN/build-g2-no-re" "${EXTRA_BUILD[@]}" > "$RUN/build-g2-no-re.log" 2>&1 &
PID_NORE=$!

wait "$PID_RE"; RC_RE=$?
wait "$PID_NORE"; RC_NORE=$?
echo "build g1-re exit $RC_RE" >> "$RUN/progress"
echo "build g2-no-re exit $RC_NORE" >> "$RUN/progress"
date -Is > "$RUN/builds-done"

groups=("g1-re" "g2-no-re" "g3-gui")
for ((r = 1; r <= ROUNDS; r++)); do
  offset=$(( (r - 1) % 3 ))
  for ((i = 0; i < 3; i++)); do
    g="${groups[$(( (offset + i) % 3 ))]}"
    round_out="$RUN/round-$r/$g"
    if [ "$g" = "g3-gui" ]; then
      cli solve --mode gui --out "$round_out" "${EXTRA_SOLVE[@]}" > "$RUN/round-$r-$g.log" 2>&1
    else
      cli solve --mode mcp --mcp "$RUN/build-$g/delivery.json" --out "$round_out" "${EXTRA_SOLVE[@]}" > "$RUN/round-$r-$g.log" 2>&1
    fi
    echo "round $r $g exit $?" >> "$RUN/progress"
  done
done
date -Is > "$RUN/done"

if command -v python3 > /dev/null 2>&1; then
  python3 "$ROOT/scripts/analyze.py" "$RUN" > "$RUN/summary.md" 2>/dev/null || true
fi
