// Development only: scripted model replies that exercise the whole chain without inference.
import type { MockPolicy } from "./mock-llm.js";

const SERVER = `import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { execFileSync } from "node:child_process";
const xdo = (...args) => execFileSync("xdotool", args.map(String));
const server = new McpServer({ name: "fruit-ninja-smoke", version: "0.0.1" }, { instructions: "Smoke-test MCP: swipe cuts straight across the arena." });
server.registerTool("status", { description: "Report whether the game runs.", inputSchema: {} },
  async () => ({ content: [{ type: "text", text: execFileSync("fn-app", ["status"]).toString() }] }));
server.registerTool("swipe", { description: "Swipe left to right at a screen height y (0-767).", inputSchema: { y: z.number().optional() } },
  async ({ y = 400 }) => { xdo("mousemove", 40, y); xdo("mousedown", 1); for (let x = 100; x <= 980; x += 80) xdo("mousemove", x, y); xdo("mouseup", 1); return { content: [{ type: "text", text: "swiped at y=" + y }] }; });
await server.connect(new StdioServerTransport());
`;

export function smokePolicy(): MockPolicy {
  const steps = { builder: 0, reviewer: 0, solver: 0, gui: 0 };
  return ({ messages }) => {
    const system = String(messages[0]?.content ?? "");
    if (system.includes("You are the Builder")) {
      const plan = [
        {
          name: "exec",
          arguments: {
            command:
              "fn-app status; cat /opt/fn/app.json; kuna --help | head -1",
            wait_seconds: 30,
          },
        },
        {
          name: "write_file",
          arguments: { path: "/output/mcp/server.mjs", content: SERVER },
        },
        {
          name: "write_file",
          arguments: {
            path: "/output/mcp/launch.sh",
            content:
              '#!/bin/sh\ncd "$(dirname "$0")" && exec node server.mjs\n',
          },
        },
        {
          name: "exec",
          arguments: {
            command:
              "chmod +x /output/mcp/launch.sh && mkdir -p /output/mcp/deps && echo scripted > /output/intermediate/notes.txt",
            wait_seconds: 10,
          },
        },
        { name: "screenshot", arguments: {} },
        {
          name: "try",
          arguments: {
            restart: true,
            wait_seconds: 60,
            calls: [
              { name: "status", arguments: {} },
              { name: "swipe", arguments: { y: 400 } },
            ],
          },
        },
        { name: "review", arguments: {} },
        { name: "finish", arguments: { notes: "scripted smoke" } },
        { name: "finish", arguments: { notes: "second finish ends the run" } },
      ][steps.builder++];
      return plan ? { tool_calls: [plan] } : { content: "done" };
    }
    if (system.includes("independent reviewer")) {
      const plan = [
        {
          name: "exec",
          arguments: {
            command: "ls -la /opt/frozen-mcp; ls /output 2>&1 | head -2",
            wait_seconds: 10,
          },
        },
        { name: "call_mcp", arguments: { name: "status", arguments: {} } },
        {
          name: "finish",
          arguments: { observations: "scripted review", next_goals: "none" },
        },
      ][steps.reviewer++];
      return plan ? { tool_calls: [plan] } : { content: "done" };
    }
    if (system.includes("real-time desktop game")) {
      const y = [300, 400, 500, 350, 450][steps.solver++ % 5];
      return { tool_calls: [{ name: "swipe", arguments: { y } }] };
    }
    // GUI player: screenshot first, then horizontal drags.
    if (steps.gui++ === 0)
      return { tool_calls: [{ name: "get_screenshot", arguments: {} }] };
    const y = [300, 400, 500][steps.gui % 3];
    return {
      tool_calls: [
        {
          name: "drag",
          arguments: {
            points: [
              { x: 60, y, t_ms: 0 },
              { x: 960, y, t_ms: 400 },
            ],
          },
        },
      ],
    };
  };
}
