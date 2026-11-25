#!/usr/bin/env node
/**
 * Evaluator side of one round, run as root inside the solve desktop. It reads the game's state
 * directly from /proc/<pid>/mem, records the game window, enters Arcade on the player's first
 * tool call, and serves the player's interface on stdio:
 *   --mode gui  the screenshot GUI tools from cli.mjs
 *   --mode mcp  the frozen MCP in /opt/frozen-mcp, run as the desktop agent user and relayed unchanged
 */
import { execFileSync, spawn } from "node:child_process";
import {
  closeSync,
  createWriteStream,
  existsSync,
  openSync,
  readFileSync,
  readSync,
  readdirSync,
} from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { createInterface } from "node:readline";
import { parseArgs } from "node:util";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { GameController, createServer } from "./cli.mjs";
import { connectDesktop } from "./desktop.mjs";
import { layout } from "./state.mjs";
import { createViewer } from "./viewer.mjs";
import { startRecording } from "./recording.mjs";

console.log = (...args) => console.error(...args); // stdout carries only JSON-RPC
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const { values } = parseArgs({
  options: {
    mode: { type: "string" },
    output: { type: "string" },
    model: { type: "string" },
  },
});
const config = JSON.parse(readFileSync("/opt/fn/app.json", "utf8"));
const output = path.resolve(values.output);
const display = config.display;
const save = (name, value) =>
  writeFile(path.join(output, name), JSON.stringify(value, null, 2) + "\n");

/** Reads game memory as root; non-root users cannot enter this process or its files. */
function memoryReader() {
  let pid = null,
    fd = null,
    window = null;
  const find = () => {
    for (const name of readdirSync("/proc")) {
      if (!/^\d+$/.test(name)) continue;
      try {
        if (
          readFileSync(`/proc/${name}/maps`, "utf8").includes(config.executable)
        )
          return Number(name);
      } catch {}
    }
    return null;
  };
  const attach = () => {
    if (pid && existsSync(`/proc/${pid}`)) return pid;
    if (fd !== null) {
      try {
        closeSync(fd);
      } catch {}
      fd = null;
    }
    window = null;
    return (pid = find());
  };
  const geometry = () => {
    if (window) return window;
    try {
      const text = execFileSync(
        "xdotool",
        [
          "search",
          "--onlyvisible",
          "--name",
          config.window_title,
          "getwindowgeometry",
          "--shell",
        ],
        { env: { DISPLAY: display, PATH: process.env.PATH } },
      ).toString();
      const field = (key) =>
        Number(new RegExp(`^${key}=(\\d+)`, "m").exec(text)?.[1]);
      window = {
        left: field("X"),
        top: field("Y"),
        width: field("WIDTH"),
        height: field("HEIGHT"),
      };
      return window;
    } catch {
      return null;
    }
  };
  return {
    async request(line) {
      const [op, address, length] = line.split(" ");
      if (op === "info") {
        const alive = attach();
        return {
          ok: true,
          alive: !!alive,
          pid: alive,
          window: alive ? geometry() : null,
        };
      }
      if (op === "focus") {
        try {
          execFileSync(
            "xdotool",
            [
              "search",
              "--onlyvisible",
              "--name",
              config.window_title,
              "windowfocus",
              "--sync",
            ],
            {
              env: { DISPLAY: display, PATH: process.env.PATH },
              timeout: 5000,
            },
          );
        } catch {}
        return { ok: true };
      }
      if (op === "read") {
        if (!attach()) return { ok: false, error: "target-not-found" };
        try {
          fd ??= openSync(`/proc/${pid}/mem`, "r");
          const bytes = Buffer.alloc(Number(length));
          if (
            readSync(fd, bytes, 0, bytes.length, Number(address)) !==
            bytes.length
          )
            return { ok: false, error: "read-failed" };
          return { ok: true, hex: bytes.toString("hex") };
        } catch {
          return { ok: false, error: "read-failed" };
        }
      }
      return { ok: false, error: "unsupported" };
    },
    close() {
      if (fd !== null) closeSync(fd);
    },
  };
}

await mkdir(output, { recursive: true });
const viewer = await createViewer(output, values.mode, values.model);
const helper = memoryReader();
const desktop = await connectDesktop({
  display,
  logPath: path.join(output, "desktop.log"),
});
const runtime = { helper, desktop, display };
const metadata = {
  mode: values.mode,
  model: values.model,
  clock: "native-realtime",
  start_policy: "first-player-tool",
  referee: "/proc/<pid>/mem as root",
  started_at: new Date().toISOString(),
};
let game,
  server,
  child,
  recording,
  state,
  resultAt,
  roundAt,
  startPromise,
  startError,
  refereeError,
  failures = 0,
  finishedMarked = false;
// Recorded, never enforced: the score before end-of-round bonuses, time spent paused, and restarts.
const round = {
  score: null,
  paused_ms: 0,
  restarts: 0,
  at: null,
  phase: null,
  remaining: null,
};
const onEvent = (event) => {
  viewer.event(event);
  if (event.type !== "state") return;
  state = event.state;
  // The first sample in the result phase still precedes the bonus tally and may hold the last points.
  if (roundAt && !resultAt && state.phase === "result")
    round.score = state.score;
  if (roundAt && !resultAt && state.phase !== "result") {
    const now = Date.now();
    if (round.phase === "paused") round.paused_ms += now - round.at;
    if (round.remaining !== null && state.remaining > round.remaining + 5)
      round.restarts++;
    Object.assign(round, {
      score: state.score,
      at: now,
      phase: state.phase,
      remaining: state.remaining,
    });
  }
  if (state.phase === "result") {
    resultAt ??= Date.now();
    if (game) game.inputClosed = true;
  }
};
const beginRound = () =>
  (startPromise ??= (async () => {
    viewer.event({ type: "status", status: "Preparing Arcade" });
    const scene = await game.startRound();
    roundAt = Date.now();
    Object.assign(metadata, {
      round_started_at: new Date(roundAt).toISOString(),
      gameplay_start_video_s: (roundAt - recording.startedAt) / 1000,
      initial_remaining: scene.remaining,
      initial_score: scene.score,
    });
    await save("metadata.json", metadata);
    return scene;
  })());

let exitCode = 0;
try {
  if (values.mode === "gui") {
    ({ server, game } = await createServer(runtime, "gui", {
      onEvent,
      preparedRound: true,
      beforeAction: () => beginRound(),
    }));
  } else {
    game = new GameController(runtime, { onEvent });
    await game.ready();
  }
  await game.observe();
  metadata.actor_screenshot = {
    width: state.canvas.width,
    height: state.canvas.height,
  };
  recording = await startRecording({
    display,
    output,
    gameSize: metadata.actor_screenshot,
  });
  await save("metadata.json", metadata);
  let busy = false;
  const poll = setInterval(() => {
    if (busy) return;
    busy = true;
    game
      .observe()
      .then(
        () => {
          failures = 0;
        },
        (error) => {
          if (
            ++failures >= 10 ||
            !/snapshot changed|read-failed/.test(error.message)
          )
            refereeError = error;
        },
      )
      .finally(() => {
        busy = false;
        if (resultAt && !finishedMarked) {
          finishedMarked = true;
          save("round-finished.json", { at: new Date().toISOString() }).catch(
            () => {},
          );
        }
      });
  }, 200);
  if (values.mode === "gui") await server.connect(new StdioServerTransport());
  else {
    // Relay JSON-RPC unchanged; the first tools/call waits for the common Arcade start.
    child = spawn("python3", ["/opt/fn/run-mcp"], {
      stdio: ["pipe", "pipe", "pipe"],
    });
    child.stderr.pipe(createWriteStream(path.join(output, "mcp-stderr.log")));
    child.stdout.pipe(process.stdout);
    child.on("exit", (code, signal) =>
      save("mcp-exit.json", {
        code,
        signal,
        at: new Date().toISOString(),
      }).catch(() => {}),
    );
    let queue = Promise.resolve();
    createInterface({ input: process.stdin }).on("line", (line) => {
      queue = queue.then(async () => {
        let message = null;
        try {
          message = JSON.parse(line);
        } catch {}
        if (message?.method === "tools/call")
          await beginRound().catch((error) => {
            startError ??= error;
          });
        if (child.stdin.writable) child.stdin.write(line + "\n");
      });
    });
  }
  // Result: wait for the round, its end and the settled result screen.
  const readiness = Date.now() + 180_000;
  while (!roundAt && Date.now() < readiness) {
    if (refereeError || startError) throw refereeError ?? startError;
    await sleep(100);
  }
  if (!roundAt)
    throw (
      startError ??
      new Error("Player did not start a round within the readiness budget")
    );
  const deadline = roundAt + 180_000;
  while (!resultAt && Date.now() < deadline) {
    if (refereeError) throw refereeError;
    await sleep(100);
  }
  if (!resultAt)
    throw new Error("Native game did not finish within its wall-time budget");
  game.inputClosed = true;
  let stableAt = Date.now(),
    previous = state.score,
    settled = false;
  const [replayDx, replayDy] = layout.result_screen.replay_offset;
  const replayTol = layout.result_screen.tolerance;
  const settle = Date.now() + 30_000;
  while (Date.now() < settle) {
    await sleep(300);
    if (state.score !== previous) {
      previous = state.score;
      stableAt = Date.now();
    }
    const replay = state.fruits.some(
      (f) =>
        !f.moving &&
        Math.abs(f.x - (state.canvas.W / 2 + replayDx)) < replayTol &&
        Math.abs(f.y - (state.canvas.H / 2 + replayDy)) < replayTol,
    );
    if (replay && Date.now() - stableAt > 2000) {
      settled = true;
      break;
    }
  }
  if (!settled) throw new Error("Native result animation did not settle");
  Object.assign(metadata, {
    native_round_wall_seconds: (resultAt - roundAt) / 1000,
    result_settle_seconds: (Date.now() - resultAt) / 1000,
  });
  await writeFile(
    path.join(output, "result.png"),
    await game.screenshot({ emit: false }),
  );
  await save("metadata.json", metadata);
  await sleep(700);
  clearInterval(poll);
  const snapshot = viewer.snapshot();
  await save("native-result.json", {
    status: "completed",
    score: state.score,
    in_round_score: round.score,
    round_integrity: {
      paused_seconds: round.paused_ms / 1000,
      restarts: round.restarts,
    },
    remaining_game_seconds: state.remaining,
    native_round_wall_seconds: metadata.native_round_wall_seconds,
    result_settle_seconds: metadata.result_settle_seconds,
    tool_calls: snapshot.calls,
    strokes: snapshot.strokes,
    mode: values.mode,
    model: values.model,
  });
} catch (error) {
  exitCode = 1;
  await save("failure.json", {
    at: new Date().toISOString(),
    error: error.stack ?? String(error),
  });
} finally {
  if (!finishedMarked)
    await save("round-finished.json", { at: new Date().toISOString() }).catch(
      () => {},
    );
  const errors = [];
  for (const step of [
    async () => {
      if (child) {
        execFileSync("python3", ["/opt/fn/run-mcp", "stop"]);
        child.kill("SIGKILL");
      }
    },
    () => viewer.close(),
    () => recording?.close(),
    () => server?.close(),
    () => desktop.close(),
    () => helper.close(),
  ]) {
    try {
      await step();
    } catch (error) {
      errors.push(String(error));
    }
  }
  await save("session-closed.json", {
    at: new Date().toISOString(),
    exit_code: exitCode,
    errors,
  });
  process.exit(exitCode);
}
