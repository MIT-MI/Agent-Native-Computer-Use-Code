// One loop for our agents (Builder, reviewer, MCP solver), cua-world style:
// no call caps, remaining time in every observation, head/tail truncation with the full output on
// disk, a reminder instead of an ending when a Builder or reviewer reply has no tool call, retries
// within the budget, and summary compaction against the endpoint's context window.
import { appendFile, mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import type { LlmEndpoint } from "./gui-agent.js";

export interface Tool {
  name: string;
  description?: string;
  inputSchema: unknown;
}
export interface LoopOptions {
  role: "builder" | "reviewer" | "solver";
  llm: LlmEndpoint;
  maxTokens: number;
  outputLimit: number;
  maxImages: number;
  contextWindow: number;
  system: string;
  user: string;
  tools: Tool[];
  invoke: (name: string, args: Record<string, unknown>) => Promise<unknown>;
  /** A tool result that ends the loop, e.g. an accepted finish. */
  isTerminal?: (name: string, value: any) => boolean;
  shouldStop?: () => boolean;
  /** Absolute Date.now() deadline in milliseconds. */
  deadline: number;
  folder: string;
}
export interface LoopResult {
  terminal_reason: string;
  terminal_result: unknown;
  final_message: string | null;
  steps: number;
  tool_calls: number;
  tool_errors: number;
  calls: Record<string, number>;
  usage: Record<string, number>;
  latency_ms: number[];
  compactions: number;
  error: string | null;
  conversation: string;
}

const SUMMARY_PROMPT =
  "Your context is about to be compacted. Write a summary a fresh conversation can continue from: the goal, " +
  "evidence paths, learned facts, current hypotheses, what was last delivered or started, and next steps.";
const functionName = (name: string) =>
  name.replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 64);
const remaining = (deadline: number) =>
  Math.max(0, Math.round((deadline - Date.now()) / 1000));

function addUsage(total: Record<string, number>, usage: unknown, prefix = "") {
  if (!usage || typeof usage !== "object") return;
  for (const [key, value] of Object.entries(usage))
    if (typeof value === "number")
      total[prefix + key] = (total[prefix + key] ?? 0) + value;
    else if (value && typeof value === "object")
      addUsage(total, value, `${key}.`);
}

/** Pull images out of a tool result; the model receives them as image parts. */
function splitImages(
  value: unknown,
  images: { data: string; mimeType: string }[],
): unknown {
  if (Array.isArray(value))
    return value.map((item) => splitImages(item, images));
  if (!value || typeof value !== "object") return value;
  const record = value as Record<string, unknown>;
  if (typeof record.image === "string" && typeof record.mimeType === "string") {
    images.push({ data: record.image, mimeType: record.mimeType });
    const { image: _image, ...rest } = record;
    return { ...rest, image: "[attached below]" };
  }
  if (Array.isArray(record.content))
    return {
      ...record,
      content: (record.content as any[]).map((block) => {
        if (block?.type === "image" && typeof block.data === "string") {
          images.push({
            data: block.data,
            mimeType: block.mimeType ?? "image/png",
          });
          return {
            type: "image",
            mimeType: block.mimeType,
            data: "[attached below]",
          };
        }
        return block;
      }),
    };
  return Object.fromEntries(
    Object.entries(record).map(([key, item]) => [
      key,
      splitImages(item, images),
    ]),
  );
}

export async function runLoop(options: LoopOptions): Promise<LoopResult> {
  const folder = options.folder;
  await mkdir(resolve(folder, "outputs"), { recursive: true });
  await mkdir(resolve(folder, "images"), { recursive: true });
  const conversation = resolve(folder, "conversation.jsonl");
  const requests = resolve(folder, "requests.jsonl");
  const key = options.llm.api_key_env
    ? process.env[options.llm.api_key_env]
    : undefined;
  if (options.llm.api_key_env && !key)
    throw new Error(`${options.llm.api_key_env} is not set`);
  const names = new Map(
    options.tools.map((tool) => [functionName(tool.name), tool.name]),
  );
  const functions = options.tools.map((tool) => ({
    type: "function",
    function: {
      name: functionName(tool.name),
      description: tool.description ?? "",
      parameters: tool.inputSchema,
    },
  }));
  await writeFile(
    resolve(folder, "prompt.json"),
    JSON.stringify(
      {
        role: options.role,
        model: options.llm.model,
        system: options.system,
        user: options.user,
        tools: functions,
      },
      null,
      2,
    ) + "\n",
  );
  const result: LoopResult = {
    terminal_reason: "time_budget",
    terminal_result: null,
    final_message: null,
    steps: 0,
    tool_calls: 0,
    tool_errors: 0,
    calls: {},
    usage: {},
    latency_ms: [],
    compactions: 0,
    error: null,
    conversation,
  };
  let messages: any[] = [];
  let images = 0;
  // Saved image paths live beside the parts sent to the endpoint, never inside them.
  const imagePath = new WeakMap<object, string>();
  const push = async (message: any) => {
    messages.push(message);
    const logged = Array.isArray(message.content)
      ? {
          ...message,
          content: message.content.map((part: any) =>
            part.type === "image_url"
              ? { type: "image_url", path: imagePath.get(part) }
              : part,
          ),
        }
      : message;
    await appendFile(conversation, JSON.stringify(logged) + "\n");
  };
  await push({ role: "system", content: options.system });
  await push({ role: "user", content: options.user });
  let lastTokens = 0;

  const post = async (body: any, purpose: string) => {
    for (;;) {
      if (Date.now() >= options.deadline) return null;
      const started = Date.now();
      let status = 0,
        text = "",
        error: string | null = null;
      try {
        const response = await fetch(
          options.llm.base_url.replace(/\/$/, "") + "/chat/completions",
          {
            method: "POST",
            headers: {
              "content-type": "application/json",
              ...(key ? { authorization: `Bearer ${key}` } : {}),
            },
            body: JSON.stringify(body),
            signal: AbortSignal.timeout(
              Math.max(
                1000,
                Math.min(
                  options.llm.request_timeout_ms ?? 300_000,
                  options.deadline - Date.now(),
                ),
              ),
            ),
          },
        );
        status = response.status;
        text = await response.text();
      } catch (caught) {
        error = String(caught);
      }
      const latency = Date.now() - started;
      await appendFile(
        requests,
        JSON.stringify({
          purpose,
          step: result.steps,
          status,
          latency_ms: latency,
          error,
          body_bytes: JSON.stringify(body).length,
          response: status === 200 ? undefined : text.slice(0, 2000),
        }) + "\n",
      );
      if (status === 200) {
        result.latency_ms.push(latency);
        return JSON.parse(text);
      }
      const length =
        status === 413 ||
        (status === 400 && /context|length|too long|maximum/i.test(text));
      if (length) return { overflow: true };
      if (error || status === 408 || status === 429 || status >= 500) {
        await sleep(Math.min(5000, Math.max(0, options.deadline - Date.now())));
        continue;
      }
      throw new Error(`endpoint HTTP ${status}: ${text.slice(0, 500)}`);
    }
  };
  const compact = async () => {
    const plain = messages.map((message) =>
      Array.isArray(message.content)
        ? {
            ...message,
            content: message.content.map((part: any) =>
              part.type === "image_url"
                ? { type: "text", text: `[image: ${imagePath.get(part)}]` }
                : part,
            ),
          }
        : message,
    );
    const reply = await post(
      {
        model: options.llm.model,
        messages: [...plain, { role: "user", content: SUMMARY_PROMPT }],
        max_tokens: options.maxTokens,
        ...(options.llm.temperature === undefined
          ? {}
          : { temperature: options.llm.temperature }),
        ...(options.llm.extra_body ?? {}),
      },
      "summary",
    );
    const summary = reply?.choices?.[0]?.message?.content;
    if (!summary) throw new Error("compaction produced no summary");
    result.compactions++;
    messages = [];
    await push({ role: "system", content: options.system });
    await push({ role: "user", content: options.user });
    await push({
      role: "user",
      content: `Summary of previous work:\n${summary}\n[remaining_seconds: ${remaining(options.deadline)}]`,
    });
    lastTokens = 0;
  };

  try {
    for (;;) {
      if (options.shouldStop?.()) {
        result.terminal_reason = "stopped";
        break;
      }
      if (Date.now() >= options.deadline) {
        result.terminal_reason = "time_budget";
        break;
      }
      if (lastTokens + options.maxTokens > options.contextWindow)
        await compact();
      result.steps++;
      const body = {
        model: options.llm.model,
        messages,
        tools: functions,
        tool_choice: "auto",
        max_tokens: options.maxTokens,
        ...(options.llm.temperature === undefined
          ? {}
          : { temperature: options.llm.temperature }),
        ...(options.llm.extra_body ?? {}),
      };
      let reply = await post(body, "turn");
      if (reply?.overflow) {
        await compact();
        reply = await post({ ...body, messages }, "turn");
      }
      if (!reply || reply.overflow) {
        result.terminal_reason = "time_budget";
        break;
      }
      addUsage(result.usage, reply.usage);
      lastTokens =
        (reply.usage?.prompt_tokens ?? 0) +
        (reply.usage?.completion_tokens ?? 0);
      const message = reply.choices?.[0]?.message ?? {};
      await push({
        role: "assistant",
        content: message.content ?? null,
        ...(message.reasoning_content
          ? { reasoning_content: message.reasoning_content }
          : {}),
        ...(message.tool_calls?.length
          ? { tool_calls: message.tool_calls }
          : {}),
      });
      if (!message.tool_calls?.length) {
        if (options.role === "solver") {
          result.terminal_reason = "final_message";
          result.final_message = message.content ?? null;
          break;
        }
        await push({
          role: "user",
          content: `No tool was called. Continue with a tool call; finish delivers your work. [remaining_seconds: ${remaining(options.deadline)}]`,
        });
        continue;
      }
      const attached: { data: string; mimeType: string }[] = [];
      let terminal = false;
      for (const call of message.tool_calls) {
        const name = names.get(call.function?.name) ?? call.function?.name;
        let value: unknown;
        try {
          if (!names.has(call.function?.name))
            throw new Error(`Unknown tool ${call.function?.name}`);
          const args = call.function?.arguments
            ? JSON.parse(call.function.arguments)
            : {};
          if (!args || typeof args !== "object" || Array.isArray(args))
            throw new Error("Tool arguments must be a JSON object");
          value = await options.invoke(name, args);
          if (options.isTerminal?.(name, value)) terminal = true;
        } catch (caught) {
          value = {
            isError: true,
            error: caught instanceof Error ? caught.message : String(caught),
          };
        }
        result.tool_calls++;
        result.calls[name] = (result.calls[name] ?? 0) + 1;
        if ((value as any)?.isError) result.tool_errors++;
        const images: { data: string; mimeType: string }[] = [];
        let text = JSON.stringify(splitImages(value, images)) ?? "null";
        attached.push(...images);
        const index = `${result.steps}-${result.tool_calls}`;
        await writeFile(resolve(folder, "outputs", `${index}.json`), text);
        if (text.length > options.outputLimit) {
          const notice =
            options.role === "solver"
              ? `\n[... ${text.length - options.outputLimit} characters omitted; the full output is retained in evidence ...]\n`
              : `\n[... ${text.length - options.outputLimit} characters omitted; full output: ${resolve(folder, "outputs", `${index}.json`)} ...]\n`;
          const half = Math.floor((options.outputLimit - notice.length) / 2);
          text = text.slice(0, half) + notice + text.slice(-half);
        }
        await push({
          role: "tool",
          tool_call_id: call.id,
          content: `${text}\n[remaining_seconds: ${remaining(options.deadline)}]`,
        });
        if (terminal) {
          result.terminal_reason = "terminated";
          result.terminal_result = value;
          break;
        }
      }
      if (attached.length) {
        const parts: any[] = [
          { type: "text", text: "Images from the tool results above." },
        ];
        for (const image of attached) {
          const path = resolve(folder, "images", `${++images}.png`);
          await writeFile(path, Buffer.from(image.data, "base64"));
          const part = {
            type: "image_url",
            image_url: { url: `data:${image.mimeType};base64,${image.data}` },
          };
          imagePath.set(part, path);
          parts.push(part);
        }
        await push({ role: "user", content: parts });
        // Keep the most recent images inline; older ones become their saved paths.
        let seen = 0;
        for (let i = messages.length - 1; i >= 0; i--) {
          if (!Array.isArray(messages[i].content)) continue;
          messages[i] = {
            ...messages[i],
            content: messages[i].content.map((part: any) =>
              part.type === "image_url" && ++seen > options.maxImages
                ? {
                    type: "text",
                    text: `[earlier image: ${imagePath.get(part)}]`,
                  }
                : part,
            ),
          };
        }
      }
      if (terminal) break;
    }
  } catch (caught) {
    result.terminal_reason = "endpoint_error";
    result.error = String(caught instanceof Error ? caught.message : caught);
  }
  await writeFile(
    resolve(folder, "loop-result.json"),
    JSON.stringify(result, null, 2) + "\n",
  );
  return result;
}
