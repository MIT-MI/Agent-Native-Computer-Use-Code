// The Windows-game Builder (cua-world style): optional exploratory tools whose method and notes
// belong to the Builder. Tools return evidence, never verdicts or repairs.
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { Tool as McpTool } from "@modelcontextprotocol/sdk/types.js";
import type { LlmEndpoint } from "./gui-agent.js";
import {
  Desktop,
  deploy,
  randomId,
  snapshotWorkspace,
  startMcp,
  type RunningMcp,
  type Snapshot,
} from "./desktop.js";
import { runLoop, type LoopResult, type Tool } from "./loop.js";

export interface GameConfig {
  title: string;
  application: string;
  version_label: string;
  executable: string;
  window_title: string;
  display: string;
  screen: string;
  game_user: string;
  wine_prefix: string;
  viewport: { width: number; height: number };
  arcade_seconds: number;
}

export interface WindowsConfig {
  game_config: string;
  builder_model_config: string;
  player_model_config: string;
  agent_max_tokens: number;
  output_limit: number;
  max_images: number;
  context_window: number;
  container_label: string;
  container_run_label: string;
  container_prefix: string;
  images: { runtime: string; builder: string; no_re: string };
  template: string;
  budgets: {
    builder_seconds: number;
    review_seconds: number;
    solve_seconds: number;
  };
  smoke_budgets: {
    builder_seconds: number;
    review_seconds: number;
    solve_seconds: number;
  };
}

const here = dirname(fileURLToPath(import.meta.url));
const BUILDER_SYSTEM =
  "You are the Builder. You explore unfamiliar software and build an MCP server for it, using only the tools provided. There is no network.";
const REVIEWER_SYSTEM =
  "You are an independent reviewer of an MCP server built for a game. You use only the tools provided. There is no network.";
const REVIEW_PROMPT =
  "Independently explore this game and its MCP. You have no future task and no Builder history. Return observations and " +
  "suggested next goals through finish. Look for game operations and information a player needs to play well in real time " +
  "that are missing or hard to understand, tools accepting scripts, expressions, command lines or code, generic screen " +
  "primitives such as click, type or screenshot, any change to game memory, rules, timing or files, and anything that " +
  "leaves, restarts or pauses a running round. Play part of a round through call_mcp as the player would and report what " +
  "the MCP achieves: score, how many targets an action reaches and what costs points. Do not repair the " +
  "workspace. Report any desktop changes you make. This is advice, not approval.\n";
const RE_REVIEW =
  "This build is meant to reverse engineer the game and play from the state it recovers. Check whether the MCP reads " +
  "the game's objects, their kinds, positions and motion, the score and the screen from memory, and where it still " +
  "relies on pixels for something the game holds exactly, say so as a next goal.\n";
const NO_RE_REVIEW =
  "This build must not reverse engineer the game, and neither may you: no disassembly, decompilation, debugging, " +
  "tracing or hooking of its executable or libraries, no reading its process memory, and no unpacking or inspecting " +
  "its files. Report any sign that the MCP or the workspace does.\n";

/** With and without reverse engineering: no-re runs as agent on the locked no-re image. */
export type Variant = "re" | "no-re";
/** Keeps the brief's shared text and its `<!-- only VARIANT -->` blocks, which run to the next marker. */
export function briefFor(text: string, variant: Variant) {
  let keep = true;
  const kept: string[] = [];
  for (const line of text.split("\n")) {
    const marker = /^<!-- (?:only (\S+)|end) -->$/.exec(line);
    if (marker) keep = !marker[1] || marker[1] === variant;
    else if (keep && !/^<!-- .* -->$/.test(line)) kept.push(line);
  }
  return kept.join("\n").replace(/\n{3,}/g, "\n\n");
}

const schema = (
  properties: Record<string, unknown> = {},
  required: string[] = [],
) => ({ type: "object", properties, required, additionalProperties: false });
const tool = (
  name: string,
  description: string,
  properties: Record<string, unknown> = {},
  required: string[] = [],
): Tool => ({ name, description, inputSchema: schema(properties, required) });
const STRING = { type: "string" },
  NUMBER = { type: "number" },
  INTEGER = { type: "integer" };
const functionName = (name: string) =>
  name.replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 64);
function waitSeconds(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0)
    throw new Error("wait_seconds must be a finite, nonnegative number");
  return value;
}
const changes = (
  before: Record<string, string> | null,
  after: Record<string, string> | null,
) =>
  before && after
    ? [...new Set([...Object.keys(before), ...Object.keys(after)])]
        .filter((p) => before[p] !== after[p])
        .sort()
    : null;
const listed = (paths: string[] | null, limit = 300) =>
  paths && paths.length > limit
    ? [
        ...paths.slice(0, limit),
        `... ${paths.length - limit} more in the report`,
      ]
    : paths;

export class Builder {
  lastStarted: Snapshot | null = null;
  finishRequested = false;
  /** The first finish with much of the budget left only asks for confirmation. */
  private earlyFinishChecked = false;
  private readonly startedAt = Date.now();
  trials: string[] = [];
  reviews: string[] = [];
  private actionDeadline: number;
  private checkedTools: string | null = null;
  private schemaObservation: unknown = null;

  constructor(
    readonly desktop: Desktop,
    readonly folder: string,
    readonly llm: LlmEndpoint,
    readonly config: WindowsConfig,
    readonly deadline: number,
    readonly reviewSeconds: number,
    readonly variant: Variant = "re",
    /** The player's endpoint, which must accept the MCP's tool schemas. */
    readonly player: LlmEndpoint = llm,
    readonly application = "Fruit Ninja HD (Windows, under Wine)",
  ) {
    this.actionDeadline = deadline;
  }

  private remaining() {
    return Math.max(0, (this.actionDeadline - Date.now()) / 1000);
  }
  private async report(kind: string, value: unknown) {
    const path = resolve(this.folder, `${kind}-${randomId()}.json`);
    await writeFile(path, JSON.stringify(value, null, 2) + "\n");
    return path;
  }
  /** RE builds: evidence (not a verdict) of whether the deployed MCP's files read the game's memory. */
  private async memoryReads(path = "/opt/frozen-mcp") {
    if (this.variant !== "re") return undefined;
    const result = await this.desktop.exec(
      `grep -rlaE '/proc/[^ ]*/mem|fn-mem\\.sock|ReadProcessMemory|process_vm_readv|memtool' ${path} || true`,
      { user: "root" },
    );
    const files = result.stdout.trim().split("\n").filter(Boolean);
    return {
      files_reading_game_memory: files,
      note: files.length
        ? "The MCP's files mention reading the game's memory."
        : "No file of the MCP mentions reading the game's memory; the brief expects play from state recovered by reverse engineering.",
    };
  }
  private scan() {
    return this.desktop.json(["python3", "/opt/fn/scan"], {
      user: "root",
    }) as Promise<Record<string, string>>;
  }
  private gui(op: string, args: unknown[] = []) {
    return this.desktop.json([
      "python3",
      "/opt/fn/xinput",
      op,
      ...args.map(String),
    ]);
  }
  private hideOutput(hidden: boolean) {
    return this.desktop.exec(`chmod ${hidden ? "000" : "755"} /output`, {
      user: "root",
    });
  }

  tools(): Tool[] {
    return [
      tool(
        "exec",
        "Run a shell command as the desktop user; wait at most wait_seconds. Return output head/tail, byte size, PID, log_path, finished and exit_code. A running command keeps running; use exec to read its log or stop it (kill -- -PID).",
        { command: STRING, wait_seconds: NUMBER },
        ["command", "wait_seconds"],
      ),
      tool(
        "read_file",
        "Read a desktop file or a harness report. Images return as images; text uses character offset and length, with next_offset and eof. The default range fits the output allowance.",
        { path: STRING, offset: INTEGER, length: INTEGER },
        ["path"],
      ),
      tool(
        "write_file",
        "Write a UTF-8 file on the desktop as the desktop user, creating directories.",
        { path: STRING, content: STRING },
        ["path", "content"],
      ),
      tool("screenshot", "Observe the current desktop."),
      ...["click", "right_click", "double_click"].map((name) =>
        tool(
          name,
          `Send a desktop ${name.replace("_", " ")}.`,
          { x: INTEGER, y: INTEGER },
          ["x", "y"],
        ),
      ),
      tool(
        "drag",
        "Drag with the left button held.",
        { x1: INTEGER, y1: INTEGER, x2: INTEGER, y2: INTEGER },
        ["x1", "y1", "x2", "y2"],
      ),
      tool("type_text", "Type literal text.", { text: STRING }, ["text"]),
      tool(
        "key",
        'Press keys together, e.g. ["ctrl", "s"] or ["Escape"].',
        { keys: { type: "array", items: STRING } },
        ["keys"],
      ),
      tool(
        "scroll",
        "Scroll the desktop.",
        {
          direction: { type: "string", enum: ["up", "down", "left", "right"] },
          count: INTEGER,
        },
        ["direction", "count"],
      ),
      tool(
        "try",
        "Snapshot /output, deploy only /output/mcp as the player gets it, start it and run calls. Wait at most wait_seconds for MCP answers, then stop it. Choose whether to restart the game afterwards. Return call results, changed paths after calls and after an optional restart, startup errors, stderr tail and a screenshot; full diffs stay in the report.",
        {
          restart: { type: "boolean" },
          wait_seconds: NUMBER,
          calls: {
            type: "array",
            items: {
              type: "object",
              properties: { name: STRING, arguments: { type: "object" } },
              required: ["name", "arguments"],
              additionalProperties: false,
            },
          },
        },
        ["restart", "wait_seconds"],
      ),
      tool(
        "review",
        `Ask a fresh, independent reviewer for observations and next goals on this desktop. Uses up to ${this.reviewSeconds} seconds of your remaining time.`,
      ),
      tool(
        "finish",
        "Deliver the current workspace if its MCP starts, otherwise the last snapshot that started. With much of your budget left, the first finish asks you to confirm instead. If nothing is deliverable, report why and continue; a second finish ends the run anyway.",
        { notes: STRING },
      ),
    ];
  }

  async invoke(name: string, args: Record<string, any>): Promise<unknown> {
    switch (name) {
      case "exec":
        return this.exec(args.command, args.wait_seconds);
      case "read_file":
        return this.readFile(args.path, args.offset ?? 0, args.length);
      case "write_file":
        return this.writeFile(args.path, args.content);
      case "screenshot":
        return this.gui("screenshot");
      case "click":
      case "right_click":
      case "double_click":
        return this.gui(name, [args.x, args.y]);
      case "drag":
        return this.gui("drag", [args.x1, args.y1, args.x2, args.y2]);
      case "type_text":
        return this.gui("type", [args.text]);
      case "key":
        return this.gui("key", args.keys);
      case "scroll":
        return this.gui("scroll", [args.direction, args.count]);
      case "try":
        return this.tryBuild(args.calls ?? [], args.restart, args.wait_seconds);
      case "review":
        return this.review();
      case "finish":
        return this.finish(args.notes);
    }
    throw new Error(`Unknown tool ${name}`);
  }

  async exec(command: unknown, wait: unknown) {
    if (typeof command !== "string" || !command.trim())
      throw new Error("command must be a non-empty string");
    const seconds = Math.min(waitSeconds(wait), this.remaining());
    const base = `/tmp/fn-exec/${randomId()}`;
    // Write the script in the foreground (a background job's stdin is /dev/null), then run it in
    // its own session with output on disk; it outlives this call if needed.
    const started = await this.desktop.exec(
      `mkdir -p /tmp/fn-exec && cat > ${base}.sh || exit 1\nsetsid bash -c "bash ${base}.sh > ${base}.log 2>&1 < /dev/null; echo \\$? > ${base}.rc" > /dev/null 2>&1 < /dev/null &\necho $!`,
      { input: command },
    );
    const pid = Number(started.stdout.trim());
    if (seconds > 0)
      await this.desktop.exec(
        `timeout ${seconds.toFixed(1)} bash -c 'while [ ! -f ${base}.rc ]; do sleep 0.2; done'`,
        { timeoutMs: (seconds + 30) * 1000 },
      );
    const peek = await this.desktop.json([
      "python3",
      "/opt/fn/files",
      "peek",
      `${base}.log`,
      String(this.config.output_limit - 4000),
    ]);
    const rc = (
      await this.desktop.exec(`cat ${base}.rc 2>/dev/null`)
    ).stdout.trim();
    return {
      pid,
      finished: rc !== "",
      exit_code: rc === "" ? null : Number(rc),
      bytes: peek.bytes ?? 0,
      output: peek.text ?? peek.error ?? "",
      log_path: `${base}.log`,
    };
  }

  async readFile(path: unknown, offset: unknown, length: unknown) {
    if (typeof path !== "string") throw new Error("path must be a string");
    if (
      !Number.isInteger(offset) ||
      (offset as number) < 0 ||
      (length !== undefined &&
        (!Number.isInteger(length) || (length as number) < 0))
    )
      throw new Error("offset and length must be nonnegative integers");
    const size =
      (length as number | undefined) ?? this.config.output_limit - 4000;
    if (resolve(path).startsWith(resolve(this.folder))) {
      const bytes = await readFile(path);
      if (/\.(png|jpe?g)$/i.test(path))
        return {
          path,
          image: bytes.toString("base64"),
          mimeType: path.endsWith(".png") ? "image/png" : "image/jpeg",
        };
      const text = bytes.toString("utf8");
      const part = text.slice(offset as number, (offset as number) + size);
      return {
        path,
        offset,
        text: part,
        next_offset: (offset as number) + part.length,
        eof: (offset as number) + part.length >= text.length,
      };
    }
    return this.desktop.json([
      "python3",
      "/opt/fn/files",
      "read",
      path,
      String(offset),
      String(size),
    ]);
  }

  async writeFile(path: unknown, content: unknown) {
    if (typeof path !== "string" || typeof content !== "string")
      throw new Error("path and content must be strings");
    const result = await this.desktop.exec(
      ["bash", "-c", 'mkdir -p "$(dirname "$1")" && cat > "$1"', "bash", path],
      { input: content },
    );
    if (result.exit_code !== 0)
      throw new Error(result.stderr.trim() || "write failed");
    return { written: path, bytes: Buffer.byteLength(content) };
  }

  /** Check once per changed tool list that the endpoint accepts the schemas; report its answer verbatim. */
  private async schema(tools: McpTool[]) {
    const key = JSON.stringify(tools);
    if (key === this.checkedTools) return this.schemaObservation;
    const token = this.player.api_key_env
      ? process.env[this.player.api_key_env]
      : undefined;
    let observation: unknown;
    try {
      const response = await fetch(
        this.player.base_url.replace(/\/$/, "") + "/chat/completions",
        {
          method: "POST",
          headers: {
            "content-type": "application/json",
            ...(token ? { authorization: `Bearer ${token}` } : {}),
          },
          body: JSON.stringify({
            model: this.player.model,
            max_tokens: 2048,
            ...(this.player.extra_body ?? {}),
            tool_choice: "none",
            messages: [
              {
                role: "user",
                content:
                  "Describe the available interface briefly. Do not call tools.",
              },
            ],
            tools: tools.map((t) => ({
              type: "function",
              function: {
                name: functionName(t.name),
                description: t.description ?? "",
                parameters: t.inputSchema,
              },
            })),
          }),
          signal: AbortSignal.timeout(120_000),
        },
      );
      const text = await response.text();
      observation = {
        http_status: response.status,
        response: text.slice(0, 1500),
      };
    } catch (error) {
      observation = { error: String(error) };
    }
    this.checkedTools = key;
    this.schemaObservation = observation;
    return observation;
  }

  private async withMcp<T>(
    folder: string,
    timeoutMs: number,
    body: (mcp: RunningMcp) => Promise<T>,
  ) {
    const mcp = await startMcp(this.desktop, folder, timeoutMs);
    try {
      return await body(mcp);
    } finally {
      await mcp.close();
    }
  }

  async tryBuild(
    calls: { name: string; arguments?: Record<string, unknown> }[],
    restart: unknown,
    wait: unknown,
  ) {
    const seconds = Math.min(waitSeconds(wait), this.remaining());
    if (typeof restart !== "boolean")
      throw new Error("restart must be a boolean");
    const folder = resolve(this.folder, `try-${randomId()}`);
    const trial: any = {
      restart_requested: restart,
      wait_seconds: wait,
      calls: [],
      errors: [],
    };
    const before = await this.scan().catch(
      (error) => (trial.errors.push(`state before calls: ${error}`), null),
    );
    try {
      trial.snapshot = await snapshotWorkspace(this.desktop, this.folder);
      await deploy(this.desktop, trial.snapshot.archive);
      trial.game_memory = await this.memoryReads();
      await this.hideOutput(true);
      const until = Date.now() + seconds * 1000;
      await this.withMcp(folder, seconds * 1000, async (mcp) => {
        trial.startup = mcp.startup;
        trial.errors.push(...mcp.startup.errors);
        if (mcp.startup.server) {
          this.lastStarted = trial.snapshot;
          await writeFile(
            resolve(this.folder, "last_started.json"),
            JSON.stringify(this.lastStarted, null, 2) + "\n",
          );
        }
        for (const call of calls) {
          const left = until - Date.now();
          if (left <= 0) {
            trial.calls.push({
              call,
              result: { error: "wait_seconds elapsed; the MCP was stopped" },
            });
            break;
          }
          try {
            trial.calls.push({
              call,
              result: await mcp.client.callTool(
                { name: call.name, arguments: call.arguments ?? {} },
                undefined,
                { timeout: left },
              ),
            });
          } catch (error) {
            trial.calls.push({ call, result: { error: String(error) } });
          }
        }
        if (mcp.startup.tools?.length)
          trial.schema_observation = await this.schema(mcp.startup.tools);
        Object.assign(trial, await mcp.stderrTail());
      });
    } catch (error) {
      trial.errors.push(String(error instanceof Error ? error.message : error));
    } finally {
      await this.hideOutput(false);
    }
    const after = await this.scan().catch(
      (error) => (trial.errors.push(`state after calls: ${error}`), null),
    );
    trial.changed_after_calls = changes(before, after);
    trial.changed_after_restart = null;
    if (restart) {
      trial.restart = await this.desktop
        .json("fn-app restart", { timeoutMs: 180_000 })
        .catch((error) => ({ error: String(error) }));
      trial.changed_after_restart = changes(
        after,
        await this.scan().catch(() => null),
      );
    }
    const shot = await this.gui("screenshot").catch(
      (error) => (trial.errors.push(`screenshot: ${error}`), null),
    );
    const report = await this.report("try", trial);
    this.trials.push(report);
    return {
      report,
      startup: trial.startup
        ? {
            server: trial.startup.server,
            instructions: trial.startup.instructions,
            tools: trial.startup.tools?.map((t: McpTool) => t.name) ?? null,
          }
        : null,
      errors: trial.errors,
      calls: trial.calls,
      schema_observation: trial.schema_observation ?? null,
      ...(trial.game_memory ? { game_memory: trial.game_memory } : {}),
      changed_after_calls: listed(trial.changed_after_calls),
      changed_after_restart: listed(trial.changed_after_restart),
      restart: trial.restart ?? null,
      stderr_tail: trial.stderr_tail ?? null,
      stderr_log: trial.stderr_log ?? null,
      ...(shot ?? {}),
    };
  }

  async review() {
    const folder = resolve(this.folder, `review-${randomId()}`);
    await mkdir(folder, { recursive: true });
    const deadline = Math.min(
      this.deadline,
      Date.now() + this.reviewSeconds * 1000,
    );
    const errors: string[] = [];
    const before = await this.scan().catch(
      (error) => (errors.push(`state before review: ${error}`), null),
    );
    let mcp: RunningMcp | null = null;
    let result: LoopResult | null = null;
    const previous = this.actionDeadline;
    try {
      await this.hideOutput(true); // the reviewer is independent of the Builder's notes
      let facts: unknown = { mcp: "No MCP has started yet; review the game." };
      if (this.lastStarted) {
        await deploy(this.desktop, this.lastStarted.archive);
        mcp = await startMcp(this.desktop, resolve(folder, "mcp"), 60_000);
        facts = {
          startup_errors: mcp.startup.errors,
          instructions: mcp.startup.instructions,
          tools: mcp.startup.tools,
        };
      }
      const running = mcp;
      this.actionDeadline = deadline;
      const tools = [
        ...this.tools().filter(
          (t) => !["try", "review", "write_file", "finish"].includes(t.name),
        ),
        tool(
          "call_mcp",
          "Call a tool of the delivered MCP with JSON arguments.",
          { name: STRING, arguments: { type: "object" } },
          ["name", "arguments"],
        ),
        tool(
          "finish",
          "Return your observations and suggested next goals.",
          { observations: STRING, next_goals: STRING },
          ["observations"],
        ),
      ];
      result = await runLoop({
        role: "reviewer",
        llm: this.llm,
        maxTokens: this.config.agent_max_tokens,
        outputLimit: this.config.output_limit,
        maxImages: this.config.max_images,
        contextWindow: this.config.context_window,
        system: REVIEWER_SYSTEM,
        user:
          REVIEW_PROMPT +
          (this.variant === "no-re" ? NO_RE_REVIEW : RE_REVIEW) +
          JSON.stringify(facts),
        tools,
        deadline,
        folder,
        invoke: async (name, args) => {
          if (name === "finish") return args;
          if (name === "call_mcp")
            return running
              ? running.client.callTool(
                  {
                    name: String(args.name),
                    arguments: (args.arguments ?? {}) as Record<
                      string,
                      unknown
                    >,
                  },
                  undefined,
                  { timeout: Math.max(1000, deadline - Date.now()) },
                )
              : { isError: true, error: "No MCP started." };
          return this.invoke(name, args);
        },
        isTerminal: (name) => name === "finish",
      });
    } catch (error) {
      errors.push(String(error instanceof Error ? error.message : error));
    } finally {
      this.actionDeadline = previous;
      if (mcp) await mcp.close();
      await this.hideOutput(false);
    }
    const desktopChanges = changes(before, await this.scan().catch(() => null));
    const advice = result?.terminal_result ?? {
      stopped_because: result?.terminal_reason ?? "error",
      transcript: result?.conversation ?? null,
    };
    const record = {
      result,
      advice,
      errors,
      desktop_changes: desktopChanges,
      stderr: mcp ? await mcp.stderrTail() : null,
    };
    const report = await this.report("review", record);
    this.reviews.push(report);
    return {
      report,
      incomplete: result?.terminal_reason !== "terminated",
      advice,
      errors,
      desktop_changes: listed(desktopChanges),
      transcript: result?.conversation ?? null,
      ...(mcp ? await mcp.stderrTail() : {}),
    };
  }

  async finish(notes?: unknown) {
    const budget = (this.deadline - this.startedAt) / 1000;
    if (!this.earlyFinishChecked && this.remaining() > 0.15 * budget) {
      this.earlyFinishChecked = true;
      const minutes = Math.round(this.remaining() / 60);
      return {
        accepted: false,
        run_continues: true,
        remaining_minutes: minutes,
        ...(this.variant === "re"
          ? {
              game_memory: await this.memoryReads("/output/mcp").catch(
                () => undefined,
              ),
            }
          : {}),
        note:
          `${minutes} minutes of your budget remain. Time left at finish is time not spent improving play: ` +
          "measure the MCP over whole rounds again and improve its weakest part. Call finish again to deliver now.",
      };
    }
    const result: any = {
      notes: notes ?? null,
      delivered_from: "last_started",
      errors: [],
    };
    try {
      if (this.remaining() > 0) {
        const snapshot = await snapshotWorkspace(this.desktop, this.folder);
        await deploy(this.desktop, snapshot.archive);
        result.game_memory = await this.memoryReads();
        await this.hideOutput(true);
        await this.withMcp(
          resolve(this.folder, `finish-${randomId()}`),
          Math.min(60, this.remaining()) * 1000,
          async (mcp) => {
            result.errors.push(...mcp.startup.errors);
            Object.assign(result, await mcp.stderrTail());
            if (mcp.startup.server) {
              this.lastStarted = snapshot;
              result.delivered_from = "current_workspace";
              result.tools = mcp.startup.tools?.map((t) => t.name) ?? null;
            }
          },
        );
      }
    } catch (error) {
      result.errors.push(
        String(error instanceof Error ? error.message : error),
      );
    } finally {
      await this.hideOutput(false);
    }
    if (this.lastStarted)
      await writeFile(
        resolve(this.folder, "last_started.json"),
        JSON.stringify(this.lastStarted, null, 2) + "\n",
      );
    result.last_started = this.lastStarted;
    result.accepted = this.lastStarted !== null || this.finishRequested;
    this.finishRequested = true;
    if (!this.lastStarted) {
      result.delivered_from = null;
      result.delivery_status = `Nothing deliverable: ${result.errors.join("; ") || "no MCP started"}`;
      result.run_continues = !result.accepted;
    }
    return result;
  }

  async run() {
    const brief = briefFor(
      await readFile(resolve(here, "CONTRACT.md"), "utf8"),
      this.variant,
    ).replace(
      "{tool_output_characters}",
      this.config.output_limit.toLocaleString("en-US"),
    );
    const result = await runLoop({
      role: "builder",
      llm: this.llm,
      maxTokens: this.config.agent_max_tokens,
      outputLimit: this.config.output_limit,
      maxImages: this.config.max_images,
      contextWindow: this.config.context_window,
      system: BUILDER_SYSTEM,
      user: `${brief}\nApplication: ${this.application}`,
      tools: this.tools(),
      deadline: this.deadline,
      folder: this.folder,
      invoke: (name, args) => this.invoke(name, args),
      isTerminal: (name, value) =>
        name === "finish" && value?.accepted === true,
    });
    const workspace = await snapshotWorkspace(this.desktop, this.folder).catch(
      (error) => ({ error: String(error) }),
    );
    const delivery = {
      variant: this.variant,
      builder: result,
      last_started: this.lastStarted,
      delivered_from: this.lastStarted
        ? result.terminal_reason === "terminated"
          ? (result.terminal_result as any)?.delivered_from
          : "last_started"
        : null,
      final_workspace: workspace,
      trials: this.trials,
      reviews: this.reviews,
    };
    await writeFile(
      resolve(this.folder, "delivery.json"),
      JSON.stringify(delivery, null, 2) + "\n",
    );
    return delivery;
  }
}
