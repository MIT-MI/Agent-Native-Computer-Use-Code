// Windows Fruit Ninja: build an MCP from the game, then play rounds through it or through the GUI.
//   node --import tsx src/cli.ts build --out DIR [--variant re|no-re] [--smoke] [--mock] [--seconds N]
//   node --import tsx src/cli.ts solve --mode mcp|gui --out DIR [--mcp DELIVERY.json|ARCHIVE.tar] [--smoke] [--mock] [--seconds N]
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { parseArgs } from "node:util";
import { fileURLToPath } from "node:url";
import { startMockLlm } from "./mock-llm.js";
import type { LlmEndpoint } from "./gui-agent.js";
import {
  Builder,
  type GameConfig,
  type Variant,
  type WindowsConfig,
} from "./builder.js";
import { Desktop, randomId } from "./desktop.js";
import { smokePolicy } from "./mock.js";
import { solveRound } from "./solve.js";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const { positionals, values } = parseArgs({
  allowPositionals: true,
  options: {
    out: { type: "string" },
    mode: { type: "string" },
    mcp: { type: "string" },
    smoke: { type: "boolean", default: false },
    mock: { type: "boolean", default: false },
    seconds: { type: "string" },
    variant: { type: "string", default: "re" },
  },
});
const config: WindowsConfig = JSON.parse(
  await readFile(resolve(root, "src/config.json"), "utf8"),
);
const gameConfig: GameConfig = JSON.parse(
  await readFile(resolve(root, config.game_config), "utf8"),
);
const budgets = { ...(values.smoke ? config.smoke_budgets : config.budgets) };
// --seconds shortens a test run: the Builder budget for build, the solve budget for solve.
if (values.seconds)
  budgets[
    process.argv.includes("build") ? "builder_seconds" : "solve_seconds"
  ] = Number(values.seconds);
if (!values.out) throw new Error("--out is required");
if (values.variant !== "re" && values.variant !== "no-re")
  throw new Error("--variant re|no-re");
const out = resolve(values.out);
await mkdir(out, { recursive: true });

// The Builder and its reviewer use the builder model; MCP and GUI players use the player model.
const endpoint = async (file: string): Promise<LlmEndpoint> =>
  JSON.parse(await readFile(resolve(root, file), "utf8"));
let llm = await endpoint(config.player_model_config);
let builderLlm = await endpoint(config.builder_model_config);
let mock: Awaited<ReturnType<typeof startMockLlm>> | null = null;
if (values.mock) {
  mock = await startMockLlm(smokePolicy(), { model: "mock" });
  llm = builderLlm = { base_url: mock.base_url, model: "mock" };
}
const commit = async () => {
  const { run } = await import("./desktop.js");
  const head = await run("git", ["-C", root, "rev-parse", "HEAD"]);
  const dirty = await run("git", ["-C", root, "status", "--porcelain"]);
  return { commit: head.stdout.trim(), dirty: dirty.stdout.trim() !== "" };
};
await writeFile(
  resolve(out, "run.json"),
  JSON.stringify(
    {
      command: positionals[0],
      argv: process.argv.slice(2),
      ...(await commit()),
      model: { ...llm, api_key_env: llm.api_key_env ?? null },
      ...(positionals[0] === "build"
        ? {
            builder_model: {
              ...builderLlm,
              api_key_env: builderLlm.api_key_env ?? null,
            },
          }
        : {}),
      budgets,
      images: config.images,
      started_at: new Date().toISOString(),
    },
    null,
    2,
  ) + "\n",
);

try {
  if (positionals[0] === "build") {
    // Without RE the Builder works as another user than the game's, on an image without analysis
    // tools: the game's files, prefix and memory are closed to it; its window and fn-app are not.
    const noRe = values.variant === "no-re";
    const desktop = await Desktop.start({
      name: `${config.container_prefix}-build-${randomId()}`,
      image: noRe ? config.images.no_re : config.images.builder,
      agent: noRe ? "agent" : gameConfig.game_user,
      memory: !noRe,
      role: "builder",
      label: config.container_label,
      runLabel: config.container_run_label,
      display: gameConfig.display,
      winePrefix: gameConfig.wine_prefix,
      gameUser: gameConfig.game_user,
      folder: out,
      mounts: [
        {
          host: resolve(root, config.template),
          path: "/opt/fn-private/template",
          readOnly: true,
        },
      ],
    });
    try {
      const builder = new Builder(
        desktop,
        out,
        builderLlm,
        config,
        Date.now() + budgets.builder_seconds * 1000,
        budgets.review_seconds,
        values.variant as Variant,
        llm,
        gameConfig.application,
      );
      const delivery = await builder.run();
      console.log(
        JSON.stringify(
          {
            variant: delivery.variant,
            delivered_from: delivery.delivered_from,
            last_started: delivery.last_started,
            terminal: delivery.builder.terminal_reason,
            calls: delivery.builder.calls,
            trials: delivery.trials.length,
            reviews: delivery.reviews.length,
          },
          null,
          2,
        ),
      );
    } finally {
      await desktop.stop();
    }
  } else if (positionals[0] === "solve") {
    if (values.mode !== "mcp" && values.mode !== "gui")
      throw new Error("--mode mcp|gui");
    let mcpArchive: string | undefined;
    let variant: Variant = values.variant as Variant;
    if (values.mode === "mcp") {
      if (!values.mcp)
        throw new Error(
          "--mcp DELIVERY.json or a workspace archive is required",
        );
      if (values.mcp.endsWith(".json")) {
        const delivery = JSON.parse(await readFile(values.mcp, "utf8"));
        mcpArchive = delivery.last_started?.archive;
        variant = delivery.variant ?? variant;
      } else mcpArchive = resolve(values.mcp);
      if (!mcpArchive) throw new Error("the delivery has no started MCP");
    }
    // An MCP built without RE plays under the same lock as its Builder.
    const noRe = values.mode === "mcp" && variant === "no-re";
    const result = await solveRound({
      mode: values.mode,
      out,
      root,
      config,
      game: gameConfig,
      llm,
      seconds: budgets.solve_seconds,
      mcpArchive,
      image: noRe ? config.images.no_re : config.images.runtime,
      agent: noRe ? "agent" : gameConfig.game_user,
      memory: values.mode === "mcp" && !noRe,
    });
    console.log(
      JSON.stringify(
        {
          score: result.score,
          in_round_score: result.in_round_score,
          valid: result.valid,
          errors: result.errors,
          failure: result.failure,
          calls: result.agent?.calls ?? result.agent?.tool_calls ?? null,
          terminal:
            result.agent?.terminal_reason ?? result.agent?.stop_reason ?? null,
        },
        null,
        2,
      ),
    );
  } else throw new Error("usage: cli.ts build|solve ...");
} finally {
  await mock?.close();
}
