import { createWriteStream } from "node:fs";
import { mkdir } from "node:fs/promises";
import path from "node:path";
import { createHash } from "node:crypto";

/** The evaluator's event log snapshot. A GUI round's log never contains entities recovered from memory. */
export function viewerSnapshot(mode, data, now = Date.now()) {
  const state = data.state;
  return {
    mode,
    model: data.model ?? null,
    now,
    status: data.status ?? "Preparing",
    score: state?.score ?? 0,
    remaining: state?.remaining ?? null,
    phase: state?.phase ?? "menu",
    viewport: state
      ? { width: state.canvas.width, height: state.canvas.height }
      : { width: 1024, height: 768 },
    ...(mode === "mcp" && state
      ? {
          scene: {
            canvas: state.canvas,
            fruits: state.fruits,
            bombs: state.bombs,
          },
          observation_ms: state.observation_ms,
        }
      : {}),
    path: data.path ?? [],
    path_at: data.pathAt ?? 0,
    screenshot: data.screenshot ?? null,
    screenshot_at: data.screenshotAt ?? 0,
    tool: data.tool ?? "Waiting",
    tool_pending: data.pending ?? false,
    tool_ms: data.toolMs ?? null,
    tool_at: data.toolAt ?? now,
    calls: data.calls ?? 0,
    strokes: data.strokes ?? 0,
    execution: data.execution ?? null,
    recent: data.recent ?? [],
    observer_only: true,
  };
}

/** Stream evaluator events to viewer-events.jsonl (no live-panel file writes). */
export async function createViewer(output, mode, model) {
  await mkdir(output, { recursive: true });
  const stream = createWriteStream(path.join(output, "viewer-events.jsonl"));
  const data = { model, status: "Preparing", calls: 0, strokes: 0, recent: [] };
  function event(event) {
    const now = Date.now();
    if (event.type === "state") data.state = event.state;
    if (event.type === "path") {
      data.path = event.points;
      data.pathAt = now;
      data.strokes++;
    }
    if (event.type === "execution") data.execution = event.execution;
    if (event.type === "status") data.status = event.status;
    if (event.type === "screenshot") {
      data.screenshotAt = now;
    }
    if (event.type === "tool:start") {
      data.tool = event.name;
      data.pending = true;
      data.calls++;
      data.toolAt = now;
    }
    if (event.type === "tool:end") {
      data.pending = false;
      data.toolMs = event.duration_ms;
      data.toolAt = now;
      data.recent.unshift({
        name: event.name,
        ms: event.duration_ms,
        error: event.error ?? null,
      });
      data.recent = data.recent.slice(0, 4);
    }
    const logged =
      event.type === "state"
        ? {
            type: "state",
            ...viewerSnapshot(mode, { ...data, state: event.state }, now),
          }
        : event.type === "screenshot"
          ? {
              type: "screenshot",
              bytes: event.png.length,
              sha256: createHash("sha256").update(event.png).digest("hex"),
            }
          : event;
    stream.write(JSON.stringify({ at: now, ...logged }) + "\n");
  }
  return {
    event,
    snapshot: () => viewerSnapshot(mode, data),
    close: async () => {
      await new Promise((resolve, reject) => {
        stream.once("error", reject);
        stream.end(resolve);
      });
    },
  };
}
