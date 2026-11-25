// One round in a fresh solve desktop. The player talks to native/eval-session.mjs over stdio;
// that evaluator reads the score from game memory, records the game window and starts Arcade on
// the first tool call. MCP players use our loop; the GUI player keeps its screenshot-and-drag loop.
import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { runAgent, type LlmEndpoint } from "./gui-agent.js";
import { StrictStdioTransport } from "./stdio.js";
import type { GameConfig, WindowsConfig } from "./builder.js";
import { Desktop, deploy, randomId } from "./desktop.js";
import { runLoop } from "./loop.js";

const SOLVER_SYSTEM =
  "You complete a task in a real-time desktop game using only the MCP tools listed. Read the MCP server instructions " +
  "below, if any. The game keeps running while you think and between calls. Tool arguments are JSON data. When the " +
  "task is complete, reply with a short report and no tool call.";

/** Task text shared by both interfaces apart from the interface paragraph. */
export function taskText(
  mode: "mcp" | "gui",
  size: { width: number; height: number },
  arcadeSeconds = 60,
) {
  const common = `Play ONE Arcade round of the original Windows Fruit Ninja HD. Maximize final score, avoid bombs, and make combos. You are the evaluated player. Use ONLY tools from the fruit_ninja MCP server. Do not use shell, files, network, browser, other servers, agents, OCR or accessibility. Your first tool call triggers the evaluator's common menu initialization and starts the ${arcadeSeconds}-game-second clock, then executes the requested tool. Call a tool immediately. Never restart, replay, pause, quit, or navigate menus. After initialization, time runs during thinking and tool calls. Keep playing until the result screen, then finish with a short score report. Do not stop early to narrate.`;
  const gui = `You have ONLY raw game screenshots and native mouse input. Start with get_screenshot. drag executes one timed mouse-down path in screenshot pixel coordinates and returns a fresh screenshot. Choose paths yourself from these images. The viewport is ${size.width} by ${size.height}; use coordinates inside it. Paths need 2..256 points, first t_ms=0, strictly increasing t_ms up to 1000, speed <=5000 px/s. Latency matters: use efficient thinking and broad useful coverage strokes of about 600–900ms, with few vertices, while avoiding visible bombs. Fruit moves between screenshots and actions. Every drag returns your next observation; keep playing without extra narration.`;
  const mcp =
    "Use the fruit_ninja MCP server's tools as its instructions and tool descriptions explain.";
  return `${common}\n${mode === "mcp" ? mcp : gui}`;
}

export interface SolveOptions {
  mode: "mcp" | "gui";
  out: string;
  root: string;
  config: WindowsConfig;
  game: GameConfig;
  llm: LlmEndpoint;
  seconds: number;
  /** Workspace archive whose mcp/ is the delivered MCP (MCP mode). */
  mcpArchive?: string;
  /** Desktop image and the user the MCP runs as (an MCP built without RE keeps its Builder's lock). */
  image?: string;
  agent?: string;
  /** An MCP built with RE reads the game's memory through fn-memd. */
  memory?: boolean;
}

export async function solveRound(options: SolveOptions) {
  const out = resolve(options.out);
  await mkdir(resolve(out, "native-evidence"), { recursive: true });
  await mkdir(resolve(options.root, ".local/windows/evidence"), {
    recursive: true,
  });
  const name = `${options.config.container_prefix}-solve-${options.mode}-${randomId()}`;
  const record: any = {
    mode: options.mode,
    model: options.llm.model,
    image: options.image ?? options.config.images.runtime,
    agent_user: options.agent ?? options.game.game_user,
    started_at: new Date().toISOString(),
    errors: [],
  };
  const desktop = await Desktop.start({
    name,
    image: options.image ?? options.config.images.runtime,
    agent: options.agent ?? options.game.game_user,
    memory: options.memory ?? false,
    role: "solve",
    label: options.config.container_label,
    runLabel: options.config.container_run_label,
    display: options.game.display,
    winePrefix: options.game.wine_prefix,
    gameUser: options.game.game_user,
    folder: out,
    caps: ["SYS_PTRACE"],
    mounts: [
      {
        host: resolve(options.root),
        path: "/opt/fn-eval/repo",
        readOnly: true,
      },
      { host: out, path: "/opt/fn-eval/out" },
      {
        host: resolve(out, "native-evidence"),
        path: "/opt/fn-eval/repo/.local/windows/evidence",
      },
      {
        host: resolve(options.root, options.config.template),
        path: "/opt/fn-private/template",
        readOnly: true,
      },
    ],
  });
  let client: Client | null = null;
  let transport: StrictStdioTransport | null = null;
  const finished = () => existsSync(resolve(out, "round-finished.json"));
  try {
    if (options.mode === "mcp") {
      if (!options.mcpArchive)
        throw new Error("MCP mode needs a delivered workspace archive");
      await deploy(desktop, options.mcpArchive);
      record.mcp = {
        archive: options.mcpArchive,
        sha256: createHash("sha256")
          .update(await readFile(options.mcpArchive))
          .digest("hex"),
      };
      // Recorded for the owner to inspect, never a verdict.
      const grep = async (pattern: string) =>
        (
          await desktop.exec(
            `grep -rlaE '${pattern}' /opt/frozen-mcp || true`,
            { user: "root" },
          )
        ).stdout
          .trim()
          .split("\n")
          .filter(Boolean);
      record.observations = {
        memory_write_apis: await grep(
          "WriteProcessMemory|VirtualProtectEx|process_vm_writev|PTRACE_POKE",
        ),
        memory_read_apis: await grep(
          "ReadProcessMemory|process_vm_readv|PTRACE_PEEK|/proc/[^ ]*/mem|fn-mem\\.sock|frida|gdb",
        ),
      };
    }
    transport = new StrictStdioTransport(
      "docker",
      [
        "exec",
        "-i",
        "-u",
        "root",
        name,
        "node",
        "/opt/fn-eval/repo/native/eval-session.mjs",
        "--mode",
        options.mode,
        "--output",
        "/opt/fn-eval/out",
        "--model",
        `${options.llm.model} / ${(options.llm.extra_body as any)?.reasoning_effort ?? "default"}`,
      ],
      resolve(out, "stdio.jsonl"),
      resolve(out, "session-stderr.log"),
    );
    client = new Client({ name: "fruit-ninja-player", version: "1" });
    await client.connect(transport, { timeout: 300_000 });
    const tools = (await client.listTools(undefined, { timeout: 120_000 }))
      .tools;
    const instructions = client.getInstructions() ?? "";
    record.interface = { tools: tools.map((t) => t.name), instructions };
    const size = JSON.parse(
      await readFile(resolve(out, "metadata.json"), "utf8"),
    ).actor_screenshot;
    const deadline = Date.now() + options.seconds * 1000;
    const call = (tool: string, args: Record<string, unknown>) =>
      client!.callTool({ name: tool, arguments: args }, undefined, {
        timeout: Math.max(1000, deadline - Date.now()),
      }) as Promise<CallToolResult>;
    const arcadeSeconds = options.game?.arcade_seconds ?? 60;
    if (options.mode === "mcp") {
      record.agent = await runLoop({
        role: "solver",
        llm: options.llm,
        maxTokens: options.config.agent_max_tokens,
        outputLimit: options.config.output_limit,
        maxImages: options.config.max_images,
        contextWindow: options.config.context_window,
        system:
          SOLVER_SYSTEM +
          (instructions ? `\n\nMCP server instructions:\n${instructions}` : ""),
        user: `Task:\n${taskText("mcp", size, arcadeSeconds)}`,
        tools: tools as any,
        invoke: call,
        shouldStop: finished,
        deadline,
        folder: resolve(out, "actor"),
      });
    } else {
      // GUI keeps its screenshot-and-drag loop, prompt and model settings.
      record.agent = await runAgent({
        llm: options.llm,
        system: "You are playing a realtime game through tools.",
        user: taskText("gui", size, arcadeSeconds),
        session: { tools, call },
        traceDir: resolve(out, "actor"),
        maxSteps: 3000,
        deadlineMs: options.seconds * 1000,
        vision: true,
        shouldStop: finished,
      });
    }
  } catch (error) {
    record.errors.push(String(error instanceof Error ? error.stack : error));
  }
  // The evaluator plays the round out and finalizes the video whatever the player did.
  const closeBy = Date.now() + 300_000;
  while (
    !existsSync(resolve(out, "session-closed.json")) &&
    Date.now() < closeBy
  )
    await sleep(250);
  if (!existsSync(resolve(out, "session-closed.json")))
    record.errors.push("evaluator session did not close within its budget");
  await client?.close().catch(() => {});
  await transport?.close().catch(() => {});
  await desktop.exec(
    `chown -R ${process.getuid!()}:${process.getgid!()} /opt/fn-eval/out`,
    { user: "root" },
  );
  await desktop.stop();
  const read = (file: string) =>
    readFile(resolve(out, file), "utf8")
      .then((t) => JSON.parse(t))
      .catch(() => null);
  const native = await read("native-result.json");
  const failure = await read("failure.json");
  const video = existsSync(resolve(out, "video.mp4"));
  Object.assign(record, {
    finished_at: new Date().toISOString(),
    score: native?.score ?? null,
    in_round_score: native?.in_round_score ?? null,
    round_integrity: native?.round_integrity ?? null,
    native,
    failure: failure?.error ?? null,
    video,
    transport_violations: transport?.violations ?? [],
    valid:
      native?.status === "completed" &&
      !failure &&
      record.errors.length === 0 &&
      video,
  });
  await writeFile(
    resolve(out, "result.json"),
    JSON.stringify(record, null, 2) + "\n",
  );
  return record;
}
