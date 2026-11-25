// Docker desktops for the Windows game, and the one deployment path shared by try, review,
// finish and solve. A desktop runs the game as the unprivileged "player" user on X display :7.
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import type { Tool } from "@modelcontextprotocol/sdk/types.js";
import { StrictStdioTransport } from "./stdio.js";

export interface ExecResult {
  exit_code: number | null;
  stdout: string;
  stderr: string;
  duration_ms: number;
  timed_out: boolean;
  truncated: boolean;
}
export function run(
  command: string,
  args: string[],
  options: { input?: string; timeoutMs?: number; maxBytes?: number } = {},
): Promise<ExecResult> {
  const maxBytes = options.maxBytes ?? 1024 * 1024;
  return new Promise((resolve, reject) => {
    const started = performance.now();
    const child = spawn(command, args, { stdio: ["pipe", "pipe", "pipe"] });
    let stdout = "",
      stderr = "",
      truncated = false,
      timedOut = false;
    const take = (current: string, chunk: Buffer) => {
      if (current.length + chunk.length > maxBytes) {
        truncated = true;
        return (
          current +
          chunk.toString("utf8", 0, Math.max(0, maxBytes - current.length))
        );
      }
      return current + chunk.toString("utf8");
    };
    child.stdout.on("data", (chunk: Buffer) => (stdout = take(stdout, chunk)));
    child.stderr.on("data", (chunk: Buffer) => (stderr = take(stderr, chunk)));
    const timer = options.timeoutMs
      ? setTimeout(() => {
          timedOut = true;
          child.kill("SIGKILL");
        }, options.timeoutMs)
      : undefined;
    child.once("error", reject);
    child.once("close", (code) => {
      clearTimeout(timer);
      resolve({
        exit_code: code,
        stdout,
        stderr,
        duration_ms: Math.round(performance.now() - started),
        timed_out: timedOut,
        truncated,
      });
    });
    child.stdin.end(options.input);
  });
}
export const docker = (args: string[], options?: Parameters<typeof run>[2]) =>
  run("docker", args, options);
export const randomId = () => Math.random().toString(36).slice(2, 10);
export interface Mount {
  host: string;
  path: string;
  readOnly?: boolean;
}

export class Desktop {
  /** `agent` runs the Builder's commands and the MCP: the game's user, or one shut out of its files and memory. */
  private constructor(
    readonly name: string,
    readonly agent: string,
    readonly gameUser: string,
    readonly display: string,
    readonly winePrefix: string,
  ) {}

  /** Start an offline desktop, copy the game for the player, start X and the game. */
  static async start(options: {
    name: string;
    image: string;
    role: "builder" | "solve";
    mounts: Mount[];
    caps?: string[];
    folder: string;
    agent?: string;
    /** Reverse engineering: serve read-only reads of the game's memory (fn-memd). */
    memory?: boolean;
    label: string;
    runLabel: string;
    display: string;
    winePrefix: string;
    gameUser: string;
  }) {
    const started = await docker([
      "run",
      "-d",
      "--name",
      options.name,
      "--label",
      options.label,
      "--label",
      `${options.runLabel}=${options.name}`,
      "--network",
      "none",
      "--cpus",
      "8",
      "--memory",
      "16g",
      "--memory-swap",
      "16g",
      "--pids-limit",
      "4096",
      "--shm-size",
      "1g",
      // fn-memd (root) needs CAP_SYS_PTRACE to read the game's memory; non-root users gain nothing.
      ...[
        ...new Set([
          ...(options.caps ?? []),
          ...(options.memory ? ["SYS_PTRACE"] : []),
        ]),
      ].flatMap((cap) => ["--cap-add", cap]),
      ...options.mounts.flatMap((m) => [
        "-v",
        `${m.host}:${m.path}${m.readOnly ? ":ro" : ""}`,
      ]),
      options.image,
      "sleep",
      "infinity",
    ]);
    if (started.exit_code !== 0)
      throw new Error(`desktop start failed: ${started.stderr.trim()}`);
    const agent = options.agent ?? options.gameUser;
    if (!/^[a-z][a-z0-9-]*$/.test(agent)) throw new Error("invalid agent user");
    const desktop = new Desktop(
      options.name,
      agent,
      options.gameUser,
      options.display,
      options.winePrefix,
    );
    const setup = await desktop.exec(
      `/opt/fn/desktop-setup ${options.role} ${agent}${options.memory ? " mem" : ""}`,
      {
        user: "root",
        timeoutMs: 300_000,
      },
    );
    await mkdir(options.folder, { recursive: true });
    await writeFile(
      resolve(options.folder, "desktop-setup.json"),
      JSON.stringify(
        {
          image: options.image,
          agent,
          memory: options.memory ?? false,
          ...setup,
        },
        null,
        2,
      ) + "\n",
    );
    if (setup.exit_code !== 0) {
      await desktop.stop();
      throw new Error(
        `desktop setup failed: ${(setup.stdout + setup.stderr).slice(-800)}`,
      );
    }
    return desktop;
  }

  exec(
    command: string | string[],
    options: {
      user?: string;
      input?: string;
      timeoutMs?: number;
      maxBytes?: number;
    } = {},
  ) {
    const user = options.user ?? this.agent;
    const home = user === "root" ? "/root" : `/home/${user}`;
    const argv =
      typeof command === "string" ? ["bash", "-c", command] : command;
    return docker(
      [
        "exec",
        "-i",
        "-u",
        user,
        "-w",
        user === "root" ? "/" : home,
        "-e",
        `HOME=${home}`,
        "-e",
        `DISPLAY=${this.display}`,
        ...(user === this.gameUser
          ? ["-e", `WINEPREFIX=${this.winePrefix}`]
          : []),
        this.name,
        ...argv,
      ],
      {
        input: options.input,
        timeoutMs: options.timeoutMs ?? 120_000,
        maxBytes: options.maxBytes ?? 64 * 1024 * 1024,
      },
    );
  }

  /** Run a guest helper that prints one JSON object. */
  async json(
    command: string | string[],
    options: Parameters<Desktop["exec"]>[1] = {},
  ) {
    const result = await this.exec(command, options);
    try {
      return JSON.parse(result.stdout);
    } catch {
      throw new Error(
        `exit ${result.exit_code}: ${(result.stdout + result.stderr).trim().slice(-600)}`,
      );
    }
  }

  copyOut(path: string, host: string) {
    return docker(["cp", `${this.name}:${path}`, host], { timeoutMs: 300_000 });
  }
  copyIn(host: string, path: string) {
    return docker(["cp", host, `${this.name}:${path}`], { timeoutMs: 300_000 });
  }
  stop() {
    return docker(["rm", "-f", this.name]);
  }
}

export interface Snapshot {
  archive: string;
  sha256: string;
  bytes: number;
}

/** Archive /output (the whole workspace) to the host. */
export async function snapshotWorkspace(
  desktop: Desktop,
  folder: string,
): Promise<Snapshot> {
  const tag = randomId();
  const remote = `/tmp/fn-workspace-${tag}.tar`;
  const packed = await desktop.exec(`tar -cf ${remote} -C /output .`, {
    user: "root",
    timeoutMs: 300_000,
  });
  if (packed.exit_code !== 0)
    throw new Error(`workspace snapshot failed: ${packed.stderr.trim()}`);
  const archive = resolve(folder, `workspace-${tag}.tar`);
  const copied = await desktop.copyOut(remote, archive);
  await desktop.exec(`rm -f ${remote}`, { user: "root" });
  if (copied.exit_code !== 0)
    throw new Error(`workspace copy failed: ${copied.stderr.trim()}`);
  const bytes = await readFile(archive);
  return {
    archive,
    sha256: createHash("sha256").update(bytes).digest("hex"),
    bytes: bytes.length,
  };
}

/** Install only mcp/ from a workspace archive at /opt/frozen-mcp, read-only to the agent. */
export async function deploy(desktop: Desktop, archive: string) {
  const copied = await desktop.copyIn(archive, "/tmp/fn-deploy.tar");
  if (copied.exit_code !== 0)
    throw new Error(`deploy copy failed: ${copied.stderr.trim()}`);
  const installed = await desktop.exec(
    "set -e; rm -rf /opt/frozen-mcp; mkdir -p /opt/frozen-mcp; tar -xf /tmp/fn-deploy.tar -C /opt/frozen-mcp --strip-components=2 ./mcp; " +
      "rm -f /tmp/fn-deploy.tar; chown -R root:root /opt/frozen-mcp; chmod -R a+rX,go-w /opt/frozen-mcp; chmod 755 /opt/frozen-mcp",
    { user: "root" },
  );
  if (installed.exit_code !== 0)
    throw new Error(
      `deploy failed (is there an mcp/ directory?): ${installed.stderr.trim()}`,
    );
}

export interface RunningMcp {
  client: Client;
  startup: {
    server: unknown;
    instructions: string | null;
    tools: Tool[] | null;
    errors: string[];
  };
  stderrTail(): Promise<{ stderr_tail: string; stderr_log: string }>;
  close(): Promise<void>;
}

/** Start the deployed MCP as the desktop's agent user and list its tools; errors are reported, not thrown. */
export async function startMcp(
  desktop: Desktop,
  folder: string,
  timeoutMs: number,
): Promise<RunningMcp> {
  await mkdir(folder, { recursive: true });
  const stderrLog = resolve(folder, "stderr.log");
  const transport = new StrictStdioTransport(
    "docker",
    ["exec", "-i", "-u", "root", desktop.name, "python3", "/opt/fn/run-mcp"],
    resolve(folder, "stdio.jsonl"),
    stderrLog,
  );
  const client = new Client({ name: "fruit-ninja-harness", version: "1" });
  const startup: RunningMcp["startup"] = {
    server: null,
    instructions: null,
    tools: null,
    errors: [],
  };
  try {
    await client.connect(transport, { timeout: Math.max(1000, timeoutMs) });
    startup.server = client.getServerVersion() ?? null;
    startup.instructions = client.getInstructions() ?? null;
    startup.tools = (
      await client.listTools(undefined, { timeout: Math.max(1000, timeoutMs) })
    ).tools;
  } catch (error) {
    startup.errors.push(
      `startup: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  if (transport.violations.length)
    startup.errors.push(...transport.violations.map((v) => `protocol: ${v}`));
  return {
    client,
    startup,
    async stderrTail() {
      const text = await readFile(stderrLog, "utf8").catch(() => "");
      return { stderr_tail: text.slice(-3000), stderr_log: stderrLog };
    },
    async close() {
      await client.close().catch(() => {});
      await transport.close().catch(() => {});
      await desktop.exec("python3 /opt/fn/run-mcp stop", {
        user: "root",
        timeoutMs: 30_000,
      });
    },
  };
}
