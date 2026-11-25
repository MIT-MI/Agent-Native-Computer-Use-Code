// Analysis script and game-config consistency checks.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

test("config/game.json and sandbox/windows/app.json agree on shared fields", async () => {
  const game = JSON.parse(await readFile("config/game.json", "utf8"));
  const app = JSON.parse(await readFile("sandbox/windows/app.json", "utf8"));
  for (const key of ["display", "screen", "executable", "window_title"])
    assert.equal(app[key], game[key], `mismatch on ${key}`);
  assert.equal(app.user, game.game_user);
  assert.equal(app.prefix, game.wine_prefix);
});

test("analyze.py regenerates summary, per-round and MCP play tables from round directories", async () => {
  const root = await mkdtemp(join(tmpdir(), "fnb-analyze-"));
  try {
    const mkRound = async (
      rel: string,
      result: Record<string, unknown>,
      events: Record<string, unknown>[],
    ) => {
      const dir = join(root, rel);
      await mkdir(dir, { recursive: true });
      await writeFile(join(dir, "result.json"), JSON.stringify(result));
      await writeFile(
        join(dir, "viewer-events.jsonl"),
        events.map((e) => JSON.stringify(e)).join("\n") + "\n",
      );
    };

    const canvas = { width: 1024, height: 768, W: 480, H: 360 };
    // RE round: scores last point on first result sample (201 -> 202), slices 1 normal + 1 special fruit, 0 bombs
    await mkRound(
      "round-1/g1-re",
      {
        mode: "mcp",
        valid: true,
        score: 287,
        in_round_score: 201,
        round_integrity: { paused_seconds: 0, restarts: 0 },
        agent: { calls: { auto_play: 1 } },
      },
      [
        {
          type: "state",
          phase: "playing",
          score: 200,
          scene: {
            canvas,
            fruits: [
              { id: "0x1", kind: 3, x: 200, y: 180, vy: -100, moving: true },
              { id: "0x2", kind: 17, x: 240, y: 150, vy: -50, moving: true },
            ],
            bombs: [],
          },
        },
        {
          type: "state",
          phase: "playing",
          score: 201,
          scene: { canvas, fruits: [], bombs: [] },
        },
        {
          type: "state",
          phase: "result",
          score: 202,
          scene: { canvas, fruits: [], bombs: [] },
        },
        {
          type: "state",
          phase: "result",
          score: 287,
          scene: { canvas, fruits: [], bombs: [] },
        },
      ],
    );

    // no-RE round: 1 sliced, 1 dropped off bottom, 1 bomb hit (score drops 15 -> 5)
    await mkRound(
      "round-1/g2-no-re",
      {
        mode: "mcp",
        valid: true,
        score: 115,
        in_round_score: 40,
        round_integrity: { paused_seconds: 0, restarts: 0 },
        agent: { calls: { play: 1 } },
      },
      [
        {
          type: "state",
          phase: "playing",
          score: 15,
          scene: {
            canvas,
            fruits: [
              { id: "0xa", kind: 2, x: 100, y: 120, vy: -80, moving: true },
              { id: "0xb", kind: 4, x: 300, y: 355, vy: 400, moving: true },
            ],
            bombs: [],
          },
        },
        {
          type: "state",
          phase: "playing",
          score: 5,
          scene: { canvas, fruits: [], bombs: [] },
        },
        {
          type: "state",
          phase: "result",
          score: 40,
          scene: { canvas, fruits: [], bombs: [] },
        },
      ],
    );

    // GUI round: no scene field by design
    await mkRound(
      "round-1/g3-gui",
      {
        mode: "gui",
        valid: true,
        score: 74,
        in_round_score: 9,
        round_integrity: { paused_seconds: 0, restarts: 0 },
        agent: { tool_calls: 23 },
      },
      [
        { type: "state", phase: "playing", score: 9 },
        { type: "state", phase: "result", score: 9 },
      ],
    );

    const md = execFileSync("python3", ["scripts/analyze.py", root]).toString();
    assert.match(
      md,
      /\| MCP built with RE \| 202 \(287\) \| \*\*202\*\* \(287\) \|/,
    );
    assert.match(md, /\| MCP built without RE \| 40 \(115\) \| 40 \(115\) \|/);
    assert.match(md, /\| GUI \| 9 \(74\) \| 9 \(74\) \|/);
    assert.match(md, /2 \/ 2 \(100%\)/);
    assert.match(md, /1 \/ 1/);
    assert.match(md, /1 \/ 2 \(50%\)/);
    assert.match(md, /~1/);
    assert.match(md, /`auto_play` ×1/);
    assert.match(md, /`play` ×1/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
