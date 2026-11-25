// The shared agent loop: guidance-style control flow, evidence on disk and bounded context.
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdtemp, readFile } from "node:fs/promises";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { runLoop, type LoopOptions } from "../src/loop.js";
import { startMockLlm, type MockPolicy } from "../src/mock-llm.js";

const PNG =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jWZkAAAAASUVORK5CYII=";
const tools = ["act", "finish"].map((name) => ({
  name,
  inputSchema: { type: "object", properties: {} },
}));
const conversation = async (folder: string) =>
  (await readFile(join(folder, "conversation.jsonl"), "utf8"))
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line));

async function loop(
  policy: MockPolicy,
  overrides: Partial<LoopOptions> & Pick<LoopOptions, "invoke">,
) {
  const mock = await startMockLlm(policy, { model: "mock" });
  const folder = await mkdtemp(join(tmpdir(), "fnb-loop-"));
  try {
    const result = await runLoop({
      role: "builder",
      llm: { base_url: mock.base_url, model: "mock" },
      maxTokens: 1000,
      outputLimit: 400,
      maxImages: 1,
      contextWindow: 1_000_000,
      system: "system",
      user: "user",
      tools,
      deadline: Date.now() + 30_000,
      folder,
      isTerminal: (name) => name === "finish",
      ...overrides,
    });
    return { result, folder, messages: await conversation(folder) };
  } finally {
    await mock.close();
  }
}

test("long tool output keeps head and tail, saves the full text and reports remaining time", async () => {
  const long = "H".repeat(1000) + "M".repeat(5000) + "T".repeat(1000);
  let step = 0;
  const { result, messages } = await loop(
    () => ({ tool_calls: [{ name: step++ ? "finish" : "act" }] }),
    {
      invoke: async (name) => (name === "act" ? long : { accepted: true }),
    },
  );
  assert.equal(result.terminal_reason, "terminated");
  const output = messages.find((m) => m.role === "tool").content as string;
  assert.ok(
    output.startsWith('"HHH') && output.includes('TTT"'),
    "head and tail survive",
  );
  assert.match(
    output,
    /characters omitted; full output: \S+outputs\/1-1\.json/,
  );
  assert.match(output, /\[remaining_seconds: \d+\]$/);
  assert.ok(output.length < 600);
  const saved = await readFile(
    output.match(/full output: (\S+\.json)/)![1],
    "utf8",
  );
  assert.equal(JSON.parse(saved), long);
});

test("a Builder reply without a tool call is reminded, not ended", async () => {
  let step = 0;
  const { result, messages } = await loop(
    () =>
      step++ === 0
        ? { content: "thinking aloud" }
        : { tool_calls: [{ name: "finish" }] },
    {
      invoke: async () => ({ accepted: true }),
    },
  );
  assert.equal(result.terminal_reason, "terminated");
  assert.ok(
    messages.some(
      (m) => m.role === "user" && /No tool was called/.test(m.content),
    ),
  );
});

test("a solver ends on its final message and sees no evidence paths", async () => {
  let step = 0;
  const { result, messages } = await loop(
    () =>
      step++ === 0
        ? { tool_calls: [{ name: "act" }] }
        : { content: "round over" },
    {
      role: "solver",
      invoke: async () => "x".repeat(2000),
      isTerminal: undefined,
    },
  );
  assert.equal(result.terminal_reason, "final_message");
  assert.equal(result.final_message, "round over");
  const output = messages.find((m) => m.role === "tool").content as string;
  assert.match(output, /the full output is retained in evidence/);
  assert.doesNotMatch(output, /outputs\//);
});

test("tool images reach the model as image parts; older ones become saved paths", async () => {
  const seen: number[] = [];
  let step = 0;
  const { result } = await loop(
    ({ messages }) => {
      seen.push(
        messages
          .flatMap((m: any) => (Array.isArray(m.content) ? m.content : []))
          .filter((p: any) => p.type === "image_url").length,
      );
      for (const m of messages)
        if (Array.isArray(m.content))
          for (const part of m.content) assert.equal("path" in part, false);
      return { tool_calls: [{ name: step++ < 2 ? "act" : "finish" }] };
    },
    {
      invoke: async (name) =>
        name === "act"
          ? { image: PNG, mimeType: "image/png" }
          : { accepted: true },
    },
  );
  assert.equal(result.terminal_reason, "terminated");
  assert.deepEqual(
    seen,
    [0, 1, 1],
    "maxImages=1 keeps only the newest image inline",
  );
});

test("tool errors and unknown tools come back as observations", async () => {
  let step = 0;
  const { result, messages } = await loop(
    () => ({ tool_calls: [{ name: ["act", "nope", "finish"][step++] }] }),
    {
      invoke: async (name) => {
        if (name === "act") throw new Error("boom");
        return { accepted: true };
      },
    },
  );
  assert.equal(result.tool_errors, 2);
  const outputs = messages
    .filter((m) => m.role === "tool")
    .map((m) => m.content);
  assert.match(outputs[0], /boom/);
  assert.match(outputs[1], /Unknown tool nope/);
});

test("transient endpoint failures are retried within the budget", async () => {
  let calls = 0;
  const server = createServer((request, response) => {
    request.resume().on("end", () => {
      if (++calls === 1) return response.writeHead(503).end("busy");
      const reply = {
        choices: [
          {
            finish_reason: "stop",
            message: { role: "assistant", content: "done" },
          },
        ],
        usage: { prompt_tokens: 1, completion_tokens: 1 },
      };
      response
        .writeHead(200, { "content-type": "application/json" })
        .end(JSON.stringify(reply));
    });
  });
  await new Promise<void>((done) => server.listen(0, "127.0.0.1", done));
  const folder = await mkdtemp(join(tmpdir(), "fnb-loop-"));
  try {
    const result = await runLoop({
      role: "solver",
      llm: {
        base_url: `http://127.0.0.1:${(server.address() as AddressInfo).port}/v1`,
        model: "m",
      },
      maxTokens: 10,
      outputLimit: 400,
      maxImages: 1,
      contextWindow: 1000,
      system: "s",
      user: "u",
      tools,
      invoke: async () => null,
      deadline: Date.now() + 20_000,
      folder,
    });
    assert.equal(result.terminal_reason, "final_message");
    assert.equal(calls, 2);
  } finally {
    server.close();
  }
});
