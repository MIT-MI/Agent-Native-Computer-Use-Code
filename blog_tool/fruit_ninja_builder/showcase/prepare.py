#!/usr/bin/env python3
"""Prepare showcase/data.json, showcase/build.json, and showcase/public/ from an experiment directory.

Supports both experiment.sh layout (round-N/{g1-re,g2-no-re,g3-gui}, build-g1-re)
and grouped archive layout ({re_mcp,no_re_mcp,gui}/round-N, re_mcp/build).
"""
import argparse
import ast
import collections
import datetime
import glob
import json
import os
import pathlib
import re
import shutil
import statistics
import sys
import tarfile

ROOT = pathlib.Path(__file__).resolve().parent

GROUP_SPEC = [
    ("gui", "g3-gui", "Computer Use"),
    ("no_re_mcp", "g2-no-re", "MCP without RE"),
    ("re_mcp", "g1-re", "MCP with RE"),
]

LIVE = r"memtool (find|watch|dump)|fnmem|xdotool|fn-mem\.sock|fn-app"
STATIC = r"fnproj|kuna|objdump|memtool rtti|app\.json|decompile\.log|FruitNinja\.exe','rb'|^ls -la /(output|home)"
EDIT = r"open\('(controller|server|game)\.py'\)\.read\(\)|cat >> (game|server|controller)\.py|chmod \+x /output/mcp|__pycache__|NOTES\.md"


def iso_ms(ts: str) -> float:
    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000.0


def find_round_dirs(exp_dir: pathlib.Path):
    found = {key: {} for key, _, _ in GROUP_SPEC}
    for rdir in sorted(exp_dir.glob("round-*")):
        m = re.fullmatch(r"round-(\d+)", rdir.name)
        if not m:
            continue
        rnum = int(m.group(1))
        for key, exp_name, _ in GROUP_SPEC:
            cand = rdir / exp_name
            if (cand / "result.json").exists():
                found[key][rnum] = cand
    if not any(found.values()):
        for key, _, _ in GROUP_SPEC:
            for rdir in sorted((exp_dir / key).glob("round-*")):
                m = re.fullmatch(r"round-(\d+)", rdir.name)
                if m and (rdir / "result.json").exists():
                    found[key][int(m.group(1))] = rdir
    return found


def find_re_build_dir(exp_dir: pathlib.Path):
    for rel in ("build-g1-re", "re_mcp/build"):
        cand = exp_dir / rel
        if (cand / "conversation.jsonl").exists():
            return cand
    return None


def extract_round_scores_and_calls(round_dir: pathlib.Path):
    meta = json.loads((round_dir / "metadata.json").read_text())
    res = json.loads((round_dir / "result.json").read_text())
    round_start_ms = iso_ms(meta["round_started_at"])
    session_start_ms = iso_ms(res["started_at"])

    scores = [[0.0, 0]]
    last_score = 0
    in_round = res.get("in_round_score")
    first_result = None
    with (round_dir / "viewer-events.jsonl").open() as stream:
        for line in stream:
            ev = json.loads(line)
            if ev.get("type") != "state":
                continue
            t = round((ev["at"] - round_start_ms) / 1000.0, 3)
            if t < 0:
                continue
            sc = ev.get("score")
            if ev.get("phase") == "result" and first_result is None:
                first_result = sc
            if sc is not None and sc != last_score:
                scores.append([t, sc])
                last_score = sc
    if first_result is not None:
        in_round = first_result

    calls = []
    tools = []
    pending = {}
    stdio_path = round_dir / "stdio.jsonl"
    if stdio_path.exists():
        # Align stdio t_ms to round_started_at using metadata.started_at as origin fallback
        origin_ms = iso_ms(meta.get("started_at", res["started_at"]))
        if origin_ms > round_start_ms:
            origin_ms = session_start_ms
        with stdio_path.open() as stream:
            for line in stream:
                rec = json.loads(line)
                try:
                    msg = json.loads(rec["line"])
                except ValueError:
                    continue
                t_s = round((origin_ms + rec["t_ms"] - round_start_ms) / 1000.0, 3)
                if rec["direction"] == "out" and msg.get("method") == "tools/call":
                    name = (msg.get("params") or {}).get("name", "")
                    entry = [t_s, None, name]
                    calls.append(entry)
                    if "id" in msg:
                        pending[msg["id"]] = entry
                elif rec["direction"] == "in" and "id" in msg:
                    if msg["id"] in pending:
                        pending.pop(msg["id"])[1] = t_s
                    elif isinstance(msg.get("result"), dict) and "tools" in msg["result"]:
                        tools = [
                            {"name": t["name"], "description": (t.get("description") or "")[:240]}
                            for t in msg["result"]["tools"]
                        ]

    final = res["score"]
    return {
        "video_start_s": round(float(meta.get("gameplay_start_video_s", 0.0)), 3),
        "round_s": round(float(meta.get("native_round_wall_seconds", 60.0)), 3),
        "in_round": in_round,
        "final": final,
        "bonus": final - in_round,
        "scores": scores,
        "calls": calls,
        "tools": tools,
    }


def prepare_rounds(exp_dir: pathlib.Path, round_num: int):
    pub = ROOT / "public"
    pub.mkdir(parents=True, exist_ok=True)
    discovered = find_round_dirs(exp_dir)
    summary = {}
    groups = []
    tools_by_group = {}
    for key, _, label in GROUP_SPEC:
        rounds = discovered[key]
        if round_num not in rounds:
            sys.exit(f"prepare.py: missing round {round_num} for group {key} under {exp_dir}")
        in_list, fin_list = [], []
        for r in sorted(rounds):
            info = extract_round_scores_and_calls(rounds[r])
            in_list.append(info["in_round"])
            fin_list.append(info["final"])
            if r == round_num:
                rdir = rounds[r]
                shutil.copyfile(rdir / "video.mp4", pub / f"{key}.mp4")
                shutil.copyfile(rdir / "result.png", pub / f"{key}-result.png")
                groups.append(
                    {
                        "key": key,
                        "label": label,
                        "video": f"{key}.mp4",
                        "result": f"{key}-result.png",
                        "video_start_s": info["video_start_s"],
                        "round_s": info["round_s"],
                        "in_round": info["in_round"],
                        "final": info["final"],
                        "bonus": info["bonus"],
                        "scores": info["scores"],
                        "calls": info["calls"],
                    }
                )
                tools_by_group[key] = info["tools"]
        summary[key] = {
            "in_round_mean": round(statistics.mean(in_list), 1),
            "final_mean": round(statistics.mean(fin_list), 1),
            "in_round": in_list,
            "final": fin_list,
        }

    data = {
        "round": round_num,
        "summary": summary,
        "groups": groups,
        "tools_re_mcp": tools_by_group.get("re_mcp", []),
        "tools_no_re_mcp": tools_by_group.get("no_re_mcp", []),
    }
    (ROOT / "data.json").write_text(json.dumps(data, indent=1) + "\n")
    print(f"wrote {ROOT / 'data.json'} (round {round_num})")


def stage(name, a):
    c = a.get("command", "") if isinstance(a, dict) else ""
    path = a.get("path", "") if isinstance(a, dict) else ""
    if name in ("try", "finish"):
        return 4
    if name == "write_file":
        return 3 if "/output/mcp/" in path or "NOTES" in path else 2
    if name == "read_file":
        return 3 if "/output/mcp" in path else 1
    if name == "screenshot":
        return 2
    if re.search(r"MCP protocol|import time, server|import server", c):
        return 4
    if re.search(EDIT, c):
        return 3
    if re.search(STATIC, c) and not re.search(LIVE, c):
        return 1
    return 2


def call_text(name, a):
    if name == "exec":
        c = re.sub(r"\s+", " ", a.get("command", "")).strip()
        c = re.sub(r"^cd \S+ && ", "", c)
        c = re.sub(r"^python3 - <<'EOF' ", "python3: ", c)
        return c[:78]
    if name in ("write_file", "read_file"):
        return a.get("path", "")
    if name == "try":
        names = [c.get("name", "") for c in a.get("calls", [])]
        return f"{len(names)} calls: " + ", ".join(names[:3]) + (", …" if len(names) > 3 else "")
    return ""


def prepare_build(build_dir: pathlib.Path, quotes_file: pathlib.Path | None = None):
    out_shots = ROOT / "public/build"
    out_shots.mkdir(parents=True, exist_ok=True)
    conv = [json.loads(l) for l in (build_dir / "conversation.jsonl").open()]
    run_meta = json.loads((build_dir / "run.json").read_text())
    start = datetime.datetime.fromisoformat(run_meta["started_at"].replace("Z", "+00:00")).timestamp()
    done = {
        int(os.path.basename(f)[:-5].split("-")[1]): os.path.getmtime(f)
        for f in glob.glob(str(build_dir / "outputs/*.json"))
    }

    calls, k, where = [], 0, {}
    for i, m in enumerate(conv):
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            k += 1
            try:
                a = json.loads(tc["function"]["arguments"])
            except ValueError:
                a = {}
            n = tc["function"]["name"]
            t = round((done.get(k, start) - start) / 60, 2)
            where.setdefault(i, t)
            calls.append(dict(t=t, stage=stage(n, a), tool=n, args=call_text(n, a)))
    calls.sort(key=lambda c: c["t"])
    counts = collections.Counter(c["stage"] for c in calls)

    shots = []
    for f in sorted(glob.glob(str(build_dir / "images/*.png")), key=os.path.getmtime):
        name = "shot-%02d.png" % (len(shots) + 1)
        shutil.copyfile(f, out_shots / name)
        shots.append(dict(t=round((os.path.getmtime(f) - start) / 60, 2), file="build/" + name))

    tries = []
    for f in sorted(glob.glob(str(build_dir / "try-*.json")), key=os.path.getmtime):
        o = json.loads(pathlib.Path(f).read_text())
        n = len((o.get("startup") or {}).get("tools") or []) or None
        snap = o.get("snapshot")
        snap_path = snap.get("archive") if isinstance(snap, dict) else snap
        tar = build_dir / os.path.basename(str(snap_path))
        if n is None and tar.exists():
            try:
                with tarfile.open(tar) as tf:
                    member = tf.extractfile("./mcp/server.py")
                    if member:
                        src = member.read().decode()
                        for node in ast.parse(src).body:
                            if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "TOOLS":
                                n = len(ast.literal_eval(node.value))
            except Exception:
                pass
        tries.append(dict(t=round((os.path.getmtime(f) - start) / 60, 2), tools=n, snap=os.path.basename(str(snap_path))))

    quotes = json.loads(quotes_file.read_text()) if quotes_file else []
    moments = []
    for q in quotes:
        hit = [
            i
            for i, m in enumerate(conv)
            if m.get("role") == "assistant"
            and re.sub(r"\s+", " ", (m.get("content") or "") + " " + (m.get("reasoning_content") or "")).strip().find(q) >= 0
        ]
        if hit:
            i = hit[0]
            moments.append(
                dict(t=where[i], stage=next(c["stage"] for c in calls if c["t"] >= where[i]), text=q.replace("`", ""))
            )

    minutes = round(max((c["t"] for c in calls), default=0.0), 1)
    model_name = (run_meta.get("builder_model") or run_meta.get("model") or {}).get("model", "Builder").split("/")[-1]
    valid_tries = [t for t in tries if t["tools"]]
    out = dict(
        model=model_name,
        minutes=minutes,
        calls=calls,
        counts={str(s): n for s, n in sorted(counts.items())},
        shots=shots,
        tries=valid_tries,
        tools=valid_tries[-1]["tools"] if valid_tries else 0,
        moments=moments,
    )
    (ROOT / "build.json").write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False))
    print(f"wrote {ROOT / 'build.json'} ({minutes} min, {len(calls)} calls, {len(shots)} shots)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment_dir", type=pathlib.Path, help="Experiment directory")
    parser.add_argument("--round", type=int, default=1, help="Round number to feature (default: 1)")
    parser.add_argument("--build", type=pathlib.Path, help="Override RE build directory for build.json")
    parser.add_argument("--quotes", type=pathlib.Path, help="Optional JSON array of build quote strings")
    args = parser.parse_args()

    prepare_rounds(args.experiment_dir, args.round)
    build_dir = args.build or find_re_build_dir(args.experiment_dir)
    if build_dir:
        prepare_build(build_dir, args.quotes)


if __name__ == "__main__":
    main()
