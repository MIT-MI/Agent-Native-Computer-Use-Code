#!/usr/bin/env python3
"""Regenerate the RE-LESSONS Markdown tables from an experiment's round directories.

Supports both experiment layouts:
  1. experiment.sh layout:   <dir>/round-N/{g1-re,g2-no-re,g3-gui}/
  2. grouped archive layout: <dir>/{re_mcp,no_re_mcp,gui}/round-N/

Counting method (thresholds read from native/re-layout.json under `analysis`):
  - In-round score: score on the first evaluator state sample with `phase == "result"` (which
    precedes the bonus tally and captures any point scored on the final tick), falling back to
    `result.json`'s `in_round_score` or the last `playing` sample. `bonus = final - in_round`.
  - Fruit episodes (MCP rounds only; GUI logs hold no memory entities by design): track each
    entity pointer `id` in `scene.fruits` during `phase == "playing"`. Start a new episode when
    an `id` reappears after absence, changes `kind`, reverses from falling (`vy > 0`) to launched
    upward (`vy < -100`), or jumps `|dy| > 150`.
  - Visible fruit sliced: among completed moving episodes (excluding fruit still in flight when
    `phase` switches to `"result"`) that entered the visible arena (`0 <= x <= W`, `0 <= y <= H`),
    any episode whose final observed sample is falling near the bottom edge
    (`y > H - dropped_y_margin` and `vy > 0`) is counted as dropped; all others disappeared above
    the bottom and are counted as sliced.
  - Specials sliced: completed moving episodes with `kind >= special_kind_min` (17, the Arcade
    bananas), using the same dropped-vs-sliced rule.
  - Bombs hit: count of score decreases (`prev_score - score >= bomb_score_drop_min`, default 1)
    during `playing` and into the first `result` sample. Bombs subtract 10 points (clamped at 0)
    and are the only penalty in Arcade, so any drop indicates a bomb hit; hits while already at 0
    are invisible, hence `~N`.
"""
import argparse
import json
from pathlib import Path
import re
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
LAYOUT = json.loads((ROOT / "native/re-layout.json").read_text())
ANALYSIS = LAYOUT["analysis"]
SPECIAL_KIND_MIN = int(ANALYSIS["special_kind_min"])
BOMB_DROP_MIN = int(ANALYSIS["bomb_score_drop_min"])
DROPPED_Y_MARGIN = float(ANALYSIS["dropped_y_margin"])

GROUPS = [
    ("re_mcp", "g1-re", "MCP built with RE"),
    ("no_re_mcp", "g2-no-re", "MCP built without RE"),
    ("gui", "g3-gui", "GUI"),
]


def discover_rounds(exp_dir: Path):
    """Return {group_key: [(round_num, round_dir), ...]} sorted by round number."""
    found = {key: [] for key, _, _ in GROUPS}
    # Layout 1: round-N/g1-re, round-N/g2-no-re, round-N/g3-gui
    for round_dir in sorted(exp_dir.glob("round-*")):
        m = re.fullmatch(r"round-(\d+)", round_dir.name)
        if not m or not round_dir.is_dir():
            continue
        rnum = int(m.group(1))
        for key, exp_name, _ in GROUPS:
            candidate = round_dir / exp_name
            if (candidate / "result.json").exists():
                found[key].append((rnum, candidate))
    # Layout 2: re_mcp/round-N, no_re_mcp/round-N, gui/round-N
    if not any(found.values()):
        for key, _, _ in GROUPS:
            gdir = exp_dir / key
            if not gdir.is_dir():
                continue
            for round_dir in sorted(gdir.glob("round-*")):
                m = re.fullmatch(r"round-(\d+)", round_dir.name)
                if m and (round_dir / "result.json").exists():
                    found[key].append((int(m.group(1)), round_dir))
    for key in found:
        found[key].sort(key=lambda pair: pair[0])
    return found


def analyze_round(round_dir: Path):
    result = json.loads((round_dir / "result.json").read_text())
    final_score = result.get("score")
    in_round_score = result.get("in_round_score")
    integrity = result.get("round_integrity") or {}
    events_path = round_dir / "viewer-events.jsonl"

    first_result_score = None
    last_playing_score = None
    bomb_drops = 0
    prev_score = None
    has_scene = False

    active = {}
    episodes = []
    canvas_h = 360.0
    canvas_w = 480.0

    if events_path.exists():
        with events_path.open() as stream:
            for line in stream:
                if not line.strip():
                    continue
                ev = json.loads(line)
                if ev.get("type") != "state":
                    continue
                phase = ev.get("phase")
                score = ev.get("score")
                if phase == "playing":
                    if prev_score is not None and score is not None and prev_score - score >= BOMB_DROP_MIN:
                        bomb_drops += 1
                    prev_score = score
                    last_playing_score = score
                    scene = ev.get("scene")
                    if scene:
                        has_scene = True
                        canvas_w = float(scene["canvas"]["W"])
                        canvas_h = float(scene["canvas"]["H"])
                        seen_now = {}
                        for f in scene.get("fruits", []):
                            fid = f["id"]
                            seen_now[fid] = f
                            ep = active.get(fid)
                            if ep is not None:
                                last = ep["last"]
                                reused = (
                                    f.get("kind") != last.get("kind")
                                    or (last.get("vy", 0) > 0 and f.get("vy", 0) < -100)
                                    or abs(f.get("y", 0) - last.get("y", 0)) > 150
                                )
                                if reused:
                                    episodes.append(ep)
                                    ep = None
                            if ep is None:
                                ep = {
                                    "id": fid,
                                    "kind": f.get("kind"),
                                    "moving": False,
                                    "visible": False,
                                    "last": f,
                                    "at_end": False,
                                }
                                active[fid] = ep
                            if f.get("moving"):
                                ep["moving"] = True
                            if 0 <= f.get("x", -1) <= canvas_w and 0 <= f.get("y", -1) <= canvas_h:
                                ep["visible"] = True
                            ep["last"] = f
                        for fid in list(active):
                            if fid not in seen_now:
                                episodes.append(active.pop(fid))
                elif phase == "result":
                    if first_result_score is None:
                        if prev_score is not None and score is not None and prev_score - score >= BOMB_DROP_MIN:
                            bomb_drops += 1
                        first_result_score = score
                    break

    for ep in active.values():
        ep["at_end"] = True
        episodes.append(ep)

    # Use the first result-phase sample when available (matches evaluator's last-sample fix).
    if first_result_score is not None:
        in_round_score = first_result_score
    elif in_round_score is None:
        in_round_score = last_playing_score

    bonus = (final_score - in_round_score) if (final_score is not None and in_round_score is not None) else None

    fruit_stats = None
    if has_scene:
        completed = [ep for ep in episodes if ep["moving"] and ep["visible"] and not ep["at_end"]]
        dropped = [
            ep for ep in completed
            if ep["last"].get("y", 0) > canvas_h - DROPPED_Y_MARGIN and ep["last"].get("vy", 0) > 0
        ]
        sliced = [ep for ep in completed if ep not in dropped]
        specials = [ep for ep in episodes if ep["moving"] and (ep["kind"] or 0) >= SPECIAL_KIND_MIN and not ep["at_end"]]
        specials_dropped = [
            ep for ep in specials
            if ep["last"].get("y", 0) > canvas_h - DROPPED_Y_MARGIN and ep["last"].get("vy", 0) > 0
        ]
        specials_sliced = len(specials) - len(specials_dropped)
        fruit_stats = {
            "visible_total": len(completed),
            "visible_sliced": len(sliced),
            "specials_total": len(specials),
            "specials_sliced": specials_sliced,
        }

    agent = result.get("agent") or {}
    calls = agent.get("calls")
    if calls is None and agent.get("tool_calls") is not None:
        calls = {"total_calls": agent["tool_calls"]}

    return {
        "valid": bool(result.get("valid")),
        "mode": result.get("mode"),
        "in_round": in_round_score,
        "bonus": bonus,
        "final": final_score,
        "paused_seconds": integrity.get("paused_seconds", 0),
        "restarts": integrity.get("restarts", 0),
        "bomb_drops": bomb_drops,
        "fruit": fruit_stats,
        "calls": calls or {},
    }


def fmt_mean(values):
    nums = [v for v in values if v is not None]
    if not nums:
        return "—"
    m = statistics.mean(nums)
    return str(int(m)) if m == int(m) else f"{m:.1f}"


def fmt_calls(calls: dict):
    if not calls:
        return "—"
    parts = [f"`{name}` ×{count}" for name, count in sorted(calls.items(), key=lambda kv: (-kv[1], kv[0]))]
    return ", ".join(parts)


def render_markdown(report: dict) -> str:
    lines = []

    # Table 1: Group summary
    lines.append("## Summary\n")
    lines.append("| Group | In-round score (final score) | Mean in-round (final) |")
    lines.append("| --- | --- | --- |")
    best_mean = max(
        (statistics.mean([r["in_round"] for r in rows if r["in_round"] is not None])
         for rows in report.values() if any(r["in_round"] is not None for r in rows)),
        default=None,
    )
    for key, _, label in GROUPS:
        rows = report.get(key, [])
        if not rows:
            continue
        scores_str = ", ".join(f"{r['in_round']} ({r['final']})" for r in rows)
        in_vals = [r["in_round"] for r in rows if r["in_round"] is not None]
        fin_vals = [r["final"] for r in rows if r["final"] is not None]
        in_mean = fmt_mean(in_vals)
        fin_mean = fmt_mean(fin_vals)
        is_best = best_mean is not None and in_vals and abs(statistics.mean(in_vals) - best_mean) < 1e-9
        mean_cell = f"**{in_mean}** ({fin_mean})" if is_best else f"{in_mean} ({fin_mean})"
        lines.append(f"| {label} | {scores_str} | {mean_cell} |")

    # Table 2: Every round
    lines.append("\n## Every round\n")
    lines.append("| Group | Round | In-round | Bonus | Final | Paused (s) | Restarts |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for key, _, label in GROUPS:
        rows = report.get(key, [])
        for idx, r in enumerate(rows):
            glabel = label if idx == 0 else ""
            lines.append(
                f"| {glabel} | {r['round']} | {r['in_round']} | {r['bonus']} | {r['final']} | "
                f"{r['paused_seconds']:.1f} | {r['restarts']} |"
            )

    # Table 3: How each MCP played
    mcp_rows = [(key, label, report.get(key, [])) for key, _, label in GROUPS if key != "gui" and report.get(key)]
    if mcp_rows:
        lines.append("\n## How each MCP played\n")
        lines.append("| Group | Round | Visible fruit sliced | Specials sliced | Bombs hit | Player's calls |")
        lines.append("| --- | ---: | --- | --- | ---: | --- |")
        for key, label, rows in mcp_rows:
            for idx, r in enumerate(rows):
                glabel = label if idx == 0 else ""
                f = r["fruit"]
                if f and f["visible_total"]:
                    pct = round(100 * f["visible_sliced"] / f["visible_total"])
                    vis_str = f"{f['visible_sliced']} / {f['visible_total']} ({pct}%)"
                    spec_str = f"{f['specials_sliced']} / {f['specials_total']}"
                else:
                    vis_str = "—"
                    spec_str = "—"
                bombs_str = "0" if r["bomb_drops"] == 0 else f"~{r['bomb_drops']}"
                lines.append(f"| {glabel} | {r['round']} | {vis_str} | {spec_str} | {bombs_str} | {fmt_calls(r['calls'])} |")

    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment_dir", type=Path, help="Experiment directory containing round directories")
    parser.add_argument("--json", action="store_true", help="Print structured JSON instead of Markdown")
    args = parser.parse_args()

    discovered = discover_rounds(args.experiment_dir)
    if not any(discovered.values()):
        sys.exit(f"analyze.py: no round result.json files found under {args.experiment_dir}")

    report = {}
    for key, _, _ in GROUPS:
        entries = []
        for rnum, rdir in discovered[key]:
            data = analyze_round(rdir)
            data["round"] = rnum
            data["dir"] = str(rdir)
            entries.append(data)
        if entries:
            report[key] = entries

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_markdown(report), end="")


if __name__ == "__main__":
    main()
