/**
 * Evaluator-side game control and the screenshot GUI interface. GameController reads the
 * native state (state.mjs), enters Arcade through the visible menu and executes GUI strokes;
 * createServer exposes the GUI tools. The Builder and its MCP never see this directory.
 */
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { performance } from "node:perf_hooks";
import { fileURLToPath } from "node:url";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { NativeState, layout } from "./state.mjs";

const run = promisify(execFile);
const sleep = (ms) =>
  new Promise((resolve) => setTimeout(resolve, Math.max(0, ms)));
const root = fileURLToPath(new URL("..", import.meta.url));
const gameConfig = JSON.parse(
  await readFile(new URL("../config/game.json", import.meta.url), "utf8"),
);

export class GameController {
  constructor(runtime, { onEvent } = {}) {
    this.runtime = runtime;
    this.reader = new NativeState(runtime.helper);
    this.buttonDown = false;
    this.onEvent = onEvent ?? (() => {});
  }
  async observe() {
    const state = await this.reader.observe();
    this.onEvent({ type: "state", state });
    return state;
  }

  async ready() {
    if (this.readyDone) return this.observe();
    const deadline = performance.now() + 60000;
    let state;
    do {
      try {
        state = await this.observe();
      } catch (error) {
        if (!error.message.includes("entity manager is not ready")) throw error;
        await sleep(250);
        continue;
      }
      if (state.fruits.length >= 2) {
        // Entities are allocated before the opening camera/menu animation is
        // finished. Let the visible controls settle before accepting input.
        await sleep(4500);
        // SetForegroundWindow reaches the game thread asynchronously; under
        // software rendering that thread can take a moment to process it.
        let focused;
        for (let attempt = 0; attempt < 50; attempt++) {
          focused = await this.runtime.helper.request("focus");
          if (focused.ok) break;
          await sleep(100);
        }
        if (!focused.ok)
          throw new Error(`Unable to focus native game: ${focused.error}`);
        await run(
          "xdotool",
          [
            "search",
            "--onlyvisible",
            "--name",
            gameConfig.window_title,
            "windowfocus",
            "--sync",
          ],
          { env: { ...process.env, DISPLAY: this.runtime.display } },
        );
        this.reader.clockHistory = [];
        this.readyDone = true;
        return this.observe();
      }
      await sleep(250);
    } while (performance.now() < deadline);
    throw new Error("Game menu did not become ready within 60 seconds");
  }

  async mouse(x, y, down) {
    const info = await this.runtime.helper.request("info");
    const b = info.window;
    if (
      !info.alive ||
      !b ||
      ![x, y].every(Number.isFinite) ||
      x < b.left ||
      x >= b.left + b.width ||
      y < b.top ||
      y >= b.top + b.height
    )
      throw new Error("Native input outside the live game window");
    await this.runtime.desktop.request({ op: "mouse", x, y, down });
    this.buttonDown = down;
    this.lastMouse = { x, y };
  }

  async execute(points, startAt = performance.now()) {
    if (this.inputClosed)
      throw new Error("The evaluated round has ended; native input is closed");
    if (this.executing)
      throw new Error("Native input is busy; wait for the current stroke");
    this.executing = true;
    try {
      return await this.performPath(points, startAt);
    } finally {
      this.executing = false;
    }
  }

  async performPath(points, startAt) {
    const info = await this.runtime.helper.request("info");
    const bounds = info.window;
    if (!bounds || points.length < 2 || points.length > 256)
      throw new Error("Invalid native path");
    if (points[0].t_ms !== 0)
      throw new Error("The first path timestamp must be zero");
    let previous = -1;
    for (const p of points) {
      if (
        ![p.x, p.y, p.t_ms].every(Number.isFinite) ||
        p.t_ms <= previous ||
        p.t_ms > 1000 ||
        p.x < bounds.left ||
        p.x >= bounds.left + bounds.width ||
        p.y < bounds.top ||
        p.y >= bounds.top + bounds.height
      )
        throw new Error("Path outside client bounds or timing limit");
      previous = p.t_ms;
    }
    for (let i = 1; i < points.length; i++) {
      const a = points[i - 1],
        b = points[i];
      if ((Math.hypot(b.x - a.x, b.y - a.y) / (b.t_ms - a.t_ms)) * 1000 > 5000)
        throw new Error("Native input exceeds the common 5000 px/s speed cap");
    }
    if (performance.now() - startAt > 100)
      throw new Error(
        "Stroke plan expired before execution; observe and plan again",
      );
    this.onEvent({
      type: "path",
      points,
      start_epoch_ms: performance.timeOrigin + startAt,
    });
    const execution = await this.runtime.desktop.request({
      op: "path",
      points,
      start_epoch_ms: performance.timeOrigin + startAt,
    });
    this.onEvent({ type: "execution", execution });
    return execution;
  }

  async menuStroke(worldX, worldY, canvas, radius = 50) {
    const x = canvas.left + ((worldX + canvas.W / 2) * canvas.width) / canvas.W;
    const y = canvas.top + ((canvas.H / 2 - worldY) * canvas.height) / canvas.H;
    const reach = Math.max(160, (radius * canvas.height) / canvas.H + 30);
    await this.execute([
      { x, y: Math.max(canvas.top + 1, y - reach), t_ms: 0 },
      { x, y: Math.min(canvas.top + canvas.height - 1, y + reach), t_ms: 350 },
    ]);
    await sleep(1000);
  }

  async startRound() {
    if (this.reader.started)
      throw new Error(
        "One round per session; launch a new session for another round",
      );
    const state = await this.ready();
    if (state.phase === "result")
      throw new Error(
        "Attach selected an already finished round; launch a fresh session",
      );
    const [newGameX, newGameY] = layout.menu.new_game_world;
    const [checkX, checkY] = layout.menu.main_fruit_screen_check;
    const tol = layout.menu.tolerance;
    const duration = layout.globals.arcade_duration_seconds;
    // Two visible menu gestures, independently observed on this pinned build.
    await this.menuStroke(newGameX, newGameY, state.canvas);
    // Startup animations can still intercept the first gesture on a busy
    // software renderer. Retry only while the same main-menu fruit remains;
    // never issue a second gesture after the mode menu or round has opened.
    let modeMenu,
      lastGesture = performance.now(),
      gestures = 1;
    const menuDeadline = performance.now() + 10000;
    do {
      await sleep(150);
      modeMenu = await this.observe();
      if (modeMenu.fruits.filter((f) => !f.moving).length >= 3) break;
      if (this.reader.started)
        throw new Error("Unexpected round during main-menu navigation");
      const main = modeMenu.fruits.some(
        (f) =>
          !f.moving &&
          Math.abs(f.x - checkX) < tol &&
          Math.abs(f.y - checkY) < tol,
      );
      if (main && gestures < 3 && performance.now() - lastGesture > 2000) {
        await this.menuStroke(newGameX, newGameY, modeMenu.canvas);
        lastGesture = performance.now();
        gestures++;
      }
    } while (performance.now() < menuDeadline);
    // Mode fruit are allocated before the visible slide-in unlocks them.
    await sleep(1500);
    modeMenu = await this.observe();
    await writeFile(
      path.join(root, ".local/windows/evidence/start-states.json"),
      JSON.stringify({ main: state, modes: modeMenu }, null, 2),
    );
    const arcade = modeMenu.fruits
      .filter((fruit) => !fruit.moving)
      .sort((a, b) => b.y - a.y)[0];
    if (!arcade || modeMenu.fruits.length < 3)
      throw new Error("Mode menu did not open after the New Game stroke");
    await this.menuStroke(
      arcade.x - modeMenu.canvas.W / 2,
      modeMenu.canvas.H / 2 - arcade.y,
      modeMenu.canvas,
      arcade.r,
    );
    let lastArcadeGesture = performance.now(),
      arcadeGestures = 1;
    const deadline = performance.now() + 25000;
    let scene;
    do {
      await sleep(100);
      scene = await this.observe();
      if (
        scene.remaining > 1 &&
        scene.remaining <= duration &&
        scene.phase !== "result"
      ) {
        this.reader.started = true;
        return this.observe();
      }
      if (
        !(scene.remaining > 0 && scene.remaining <= duration + 5) &&
        scene.fruits.filter((f) => !f.moving).length >= 3 &&
        arcadeGestures < 3 &&
        performance.now() - lastArcadeGesture > 2000
      ) {
        const next = scene.fruits
          .filter((f) => !f.moving)
          .sort((a, b) => b.y - a.y)[0];
        await this.menuStroke(
          next.x - scene.canvas.W / 2,
          scene.canvas.H / 2 - next.y,
          scene.canvas,
          next.r,
        );
        lastArcadeGesture = performance.now();
        arcadeGestures++;
      }
    } while (performance.now() < deadline);
    await writeFile(
      path.join(root, ".local/windows/evidence/start-failure.png"),
      await this.screenshot(),
    );
    throw new Error(
      "Arcade round did not start; inspect .local/windows/evidence/start-failure.png",
    );
  }

  async screenshot({ emit = true } = {}) {
    const result = await this.runtime.desktop.request({ op: "screenshot" });
    const png = Buffer.from(result.data, "base64");
    if (emit) this.onEvent({ type: "screenshot", png });
    return png;
  }
}

export async function createServer(
  runtime,
  mode = "gui",
  { onEvent, preparedRound = false, beforeAction } = {},
) {
  if (mode !== "gui") throw new Error("Only the GUI interface is served here");
  const game = new GameController(runtime, { onEvent });
  await game.ready();
  const server = new McpServer(
    { name: "fruit-ninja-windows-gui", version: "0.2.0" },
    {
      instructions: preparedRound
        ? beforeAction
          ? "The evaluator enters Arcade on your first tool call after model readiness. Then play immediately; never navigate menus, pause, restart or replay."
          : "The evaluator has already started one Arcade round. Play immediately; never navigate menus, restart or replay. All thinking and calls consume real game time."
        : "Play the original Windows game using screenshots and native mouse input. Slice New Game, then Arcade Mode. Avoid bombs and make combos. Thinking, screenshots and strokes consume real time.",
    },
  );
  const record = async (name, args, body) => {
    const began = performance.now();
    game.onEvent({ type: "tool:start", name, args });
    try {
      await beforeAction?.();
      const content = await body();
      game.onEvent({
        type: "tool:end",
        name,
        duration_ms: performance.now() - began,
      });
      return { content };
    } catch (error) {
      game.onEvent({
        type: "tool:end",
        name,
        duration_ms: performance.now() - began,
        error: error.message,
      });
      return {
        isError: true,
        content: [{ type: "text", text: error.message }],
      };
    }
  };
  const annotations = {
    readOnlyHint: false,
    destructiveHint: false,
    openWorldHint: false,
  };
  const image = async () => [
    {
      type: "image",
      mimeType: "image/png",
      data: (await game.screenshot()).toString("base64"),
    },
  ];
  server.registerTool(
    "get_controls",
    {
      description: "Read the common game rules and native input limits.",
      inputSchema: {},
      annotations: { ...annotations, readOnlyHint: !beforeAction },
    },
    (args) =>
      record("get_controls", args, async () => {
        const info = await runtime.helper.request("info");
        const controls = {
          title: gameConfig.title,
          interface: "gui",
          viewport: info.window,
          rules:
            `Slice fruit; avoid bombs. Arcade lasts ${gameConfig.arcade_seconds} game seconds. ` +
            (preparedRound
              ? "The evaluator has already started Arcade; play immediately."
              : "Slice the visible menu fruit to navigate."),
          path: {
            units: "screen pixels",
            max_duration_ms: 1000,
            max_points: 256,
          },
        };
        return [{ type: "text", text: JSON.stringify(controls) }];
      }),
  );
  server.registerTool(
    "get_screenshot",
    {
      description:
        "Capture the live game screen. Time continues while observing.",
      inputSchema: {},
      annotations: { ...annotations, readOnlyHint: !beforeAction },
    },
    (args) => record("get_screenshot", args, image),
  );
  server.registerTool(
    "drag",
    {
      description:
        "Execute one timed mouse-down path using screenshot coordinates and return a fresh game screenshot.",
      inputSchema: {
        points: z
          .array(
            z.object({
              x: z.number(),
              y: z.number(),
              t_ms: z.number().min(0).max(1000),
            }),
          )
          .min(2)
          .max(256),
      },
      annotations,
    },
    (args) =>
      record("drag", args, async () => {
        const execution = await game.execute(args.points);
        return [
          { type: "text", text: JSON.stringify(execution) },
          ...(await image()),
        ];
      }),
  );
  server.registerTool(
    "click",
    {
      description: "Perform one native left click at a screenshot position.",
      inputSchema: { x: z.number(), y: z.number() },
      annotations,
    },
    (args) =>
      record("click", args, async () => {
        const execution = await game.execute([
          { x: args.x, y: args.y, t_ms: 0 },
          { x: args.x, y: args.y, t_ms: 100 },
        ]);
        return [
          { type: "text", text: JSON.stringify(execution) },
          ...(await image()),
        ];
      }),
  );
  return { server, game };
}
