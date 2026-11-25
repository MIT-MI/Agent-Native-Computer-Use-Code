#!/usr/bin/env bash
# Verify the no-RE lockdown by attempting every blocked and allowed path as the `agent` user.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
read -r IMAGE LABEL RUN_LABEL PREFIX TEMPLATE DISPLAY_VAL < <(
  python3 -c '
import json, sys
c = json.load(open(sys.argv[1]))
g = json.load(open(sys.argv[2]))
print(c["images"]["no_re"], c["container_label"], c["container_run_label"], c["container_prefix"], c["template"], g["display"])
' "$ROOT/src/config.json" "$ROOT/config/game.json"
)

CID="${PREFIX}-lockcheck-$$"
cleanup() { docker rm -f "$CID" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --name "$CID" --label "$LABEL" --label "${RUN_LABEL}=${CID}" --network none \
  -v "$ROOT/$TEMPLATE:/opt/fn-private/template:ro" \
  "$IMAGE" sleep infinity >/dev/null
docker exec -u root "$CID" /opt/fn/desktop-setup builder agent >/dev/null

docker exec -i -u agent -e HOME=/home/agent -e "DISPLAY=${DISPLAY_VAL}" "$CID" python3 - << 'PY'
import ctypes, json, os, subprocess, sys

ok = True
def check(label, cond, detail=""):
    global ok
    status = "PASS" if cond else "FAIL"
    if not cond:
        ok = False
    print(f"[{status}] {label}: {detail}")

check("uid is agent (10002)", os.getuid() == 10002, f"uid={os.getuid()}")

st = subprocess.run(["fn-app", "status"], capture_output=True, text=True)
info = json.loads(st.stdout)
pid = info["pids"][0]
check("fn-app status allowed", st.returncode == 0 and info["running"] and pid > 0, f"pid={pid}")

bad = subprocess.run(["fn-app", "shell"], capture_output=True, text=True)
check("fn-app invalid verb refused", bad.returncode != 0 and "error" in bad.stdout, bad.stdout.strip())

try:
    open(f"/proc/{pid}/mem", "rb")
    check("/proc/PID/mem denied", False, "OPENED!")
except PermissionError as e:
    check("/proc/PID/mem denied", True, str(e))

try:
    open(f"/proc/{pid}/maps", "r").read()
    check("/proc/PID/maps denied", False, "READ!")
except PermissionError as e:
    check("/proc/PID/maps denied", True, str(e))

libc = ctypes.CDLL("libc.so.6", use_errno=True)
rc = libc.ptrace(16, pid, 0, 0)  # PTRACE_ATTACH
err = ctypes.get_errno()
check("ptrace denied", rc == -1 and err in (1, 13), f"rc={rc} errno={err}")

try:
    os.kill(pid, 0)
    check("signal to game denied", False, "ALLOWED!")
except PermissionError as e:
    check("signal to game denied", True, str(e))

for path in ["/home/player", "/home/player/fn/game", "/home/player/fn/wine", "/tmp/fn-app", "/opt/fn-private"]:
    try:
        os.listdir(path)
        check(f"{path} denied", False, "LISTED!")
    except PermissionError as e:
        check(f"{path} denied", True, str(e))

check("/run/fn-mem.sock absent", not os.path.exists("/run/fn-mem.sock"), "not present")

tools = [
    "objdump", "readelf", "strings", "nm", "addr2line", "size", "objcopy", "c++filt",
    "elfedit", "gprof", "winedbg", "gdb", "strace", "ltrace", "frida", "kuna", "rizin",
    "xxd", "memtool",
]
present = [
    t for t in tools
    if subprocess.run(["bash", "-c", f"command -v {t} >/dev/null 2>&1 || ls /usr/bin/*-{t} >/dev/null 2>&1"]).returncode == 0
]
check("analysis tools absent", len(present) == 0, f"checked {len(tools)} tools, present={present}")

shot = json.loads(subprocess.check_output(["python3", "/opt/fn/xinput", "screenshot"]).decode())
check("screenshot allowed", len(shot.get("image", "")) > 1000, f"b64_len={len(shot.get('image', ''))}")

inp = json.loads(subprocess.check_output(["python3", "/opt/fn/xinput", "click", "512", "384"]).decode())
check("input allowed", inp.get("done") == "click", str(inp))

open("/output/mcp/locktest.txt", "w").write("ok")
check("/output writable", open("/output/mcp/locktest.txt").read() == "ok", "/output/mcp/locktest.txt")

sys.exit(0 if ok else 1)
PY
