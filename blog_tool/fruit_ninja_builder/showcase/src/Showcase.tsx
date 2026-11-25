import React from "react";
import {
  AbsoluteFill,
  Img,
  OffthreadVideo,
  Sequence,
  interpolate,
  Easing,
  staticFile,
  useCurrentFrame,
} from "remotion";
import { loadFont as loadInter } from "@remotion/google-fonts/Inter";
import { loadFont as loadMono } from "@remotion/google-fonts/JetBrainsMono";
import data from "../data.json";

const { fontFamily: SANS } = loadInter("normal", { weights: ["400", "500", "600", "700", "800"], subsets: ["latin"] });
const { fontFamily: MONO } = loadMono("normal", { weights: ["400", "600"], subsets: ["latin"] });

// Palette: white page, MCP blue, computer-use orange.
const C = {
  bg: "#ffffff",
  fg: "#1d1d1f",
  fg2: "#6e6e73",
  panel: "#f5f5f7",
  line: "#d2d2d7",
  accent: "#a8874a",
  accentSoft: "#f6efe2",
  accentInk: "#7a5f2c",
  mcp: "#3b76d9",
  mcpLight: "#b1c8f0",
  mcpMid: "#5a84d4",
  gui: "#e8710a",
  guiInk: "#b4500a",
};
const SHADOW = "0 1px 2px rgba(0,0,0,0.05), 0 12px 32px rgba(0,0,0,0.08)";

type Key = "gui" | "no_re_mcp" | "re_mcp";
type Group = {
  key: Key;
  label: string;
  video: string;
  result: string;
  video_start_s: number;
  round_s: number;
  in_round: number;
  final: number;
  bonus: number;
  scores: number[][];
  calls: (number | string | null)[][];
};
const GROUPS = data.groups as unknown as Group[];
const STYLE: Record<Key, { bar: string; ink: string; dot: string; ring?: string }> = {
  gui: { bar: C.gui, ink: C.guiInk, dot: C.gui },
  no_re_mcp: { bar: C.mcpLight, ink: C.mcpMid, dot: C.mcpLight, ring: C.mcp },
  re_mcp: { bar: C.mcp, ink: C.mcp, dot: C.mcp },
};

// Timeline (30 fps).
const FPS = 30;
const SPEED = 2;
const INTRO = { from: 0, len: 150 };
const CHART = { from: 150, len: 210 };
const TOOLS = { from: 360, len: 180 };
const REPLAY_SOURCE_S = 76; // every recording covers the round and its settled result screen
const REPLAY = { from: 540, len: Math.round((REPLAY_SOURCE_S / SPEED) * FPS) };
const HOLD = { from: REPLAY.from + REPLAY.len, len: 90 };
export const TOTAL_FRAMES = HOLD.from + HOLD.len;

// Layout.
const MARGIN = 72;
const GAP = 48;
const PANEL_W = (1920 - 2 * MARGIN - 2 * GAP) / 3;
const MEDIA_H = (PANEL_W * 3) / 4;
const panelX = (i: number) => MARGIN + i * (PANEL_W + GAP);

const fade = (frame: number, len: number, edge = 10) =>
  interpolate(frame, [0, edge, len - edge, len], [0, 1, 1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
const clock = (s: number) => {
  const t = Math.max(0, Math.floor(s));
  return `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
};
const scoreAt = (g: Group, t: number) => {
  let s = 0;
  for (const [at, v] of g.scores) if (at <= t) s = v;
  return s;
};
const callsAt = (g: Group, t: number) => g.calls.filter((c) => (c[0] as number) <= t).length;
const actionAt = (g: Group, t: number) => {
  if (t >= g.round_s) return "round over";
  const active = g.calls.find((c) => (c[0] as number) <= t && (c[1] === null || t < (c[1] as number)));
  if (!active) return "thinking";
  const name = active[2] as string;
  return name === "get_screenshot" ? "screenshot" : name;
};
const maxFinal = Math.max(...GROUPS.map((g) => g.final));

const Dot: React.FC<{ k: Key; size?: number }> = ({ k, size = 14 }) => (
  <span
    style={{
      display: "inline-block",
      width: size,
      height: size,
      borderRadius: size,
      background: STYLE[k].dot,
      boxShadow: STYLE[k].ring ? `inset 0 0 0 2px ${STYLE[k].ring}` : undefined,
      marginRight: 12,
      flex: "none",
    }}
  />
);

const Header: React.FC<{ subtitle?: string }> = ({ subtitle }) => (
  <div style={{ position: "absolute", top: 46, left: 0, right: 0, textAlign: "center" }}>
    <div style={{ fontSize: 66, fontWeight: 800, letterSpacing: "-0.03em", lineHeight: 1.02, color: C.fg }}>Fruit Ninja</div>
    <div style={{ marginTop: 12, fontSize: 23, color: C.fg2, letterSpacing: "-0.01em" }}>
      {subtitle ?? `Kimi K3 builds each MCP server and plays the same game, round ${data.round} of 3`}
    </div>
  </div>
);

const Label: React.FC<{ g: Group; right?: string }> = ({ g, right }) => (
  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", height: 40 }}>
    <div style={{ display: "flex", alignItems: "center", fontSize: 28, fontWeight: 600, color: C.fg }}>
      <Dot k={g.key} />
      {g.label}
    </div>
    {right !== undefined && (
      <div style={{ fontSize: 22, color: C.fg2, fontVariantNumeric: "tabular-nums" }}>{right}</div>
    )}
  </div>
);

const Media: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <div
    style={{
      width: PANEL_W,
      height: MEDIA_H,
      borderRadius: 20,
      overflow: "hidden",
      background: C.panel,
      boxShadow: SHADOW,
      marginTop: 10,
    }}
  >
    {children}
  </div>
);

// 1. The three result screens.
const Intro: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <AbsoluteFill style={{ opacity: fade(frame, INTRO.len) }}>
      <Header />
      {GROUPS.map((g, i) => {
        const rise = interpolate(frame, [6 + i * 6, 26 + i * 6], [24, 0], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
          easing: Easing.out(Easing.cubic),
        });
        return (
          <div key={g.key} style={{ position: "absolute", left: panelX(i), top: 218 + rise, width: PANEL_W }}>
            <Label g={g} />
            <Media>
              <Img src={staticFile(g.result)} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
            </Media>
            <div style={{ marginTop: 20, fontSize: 25, color: C.fg2, display: "flex", gap: 36 }}>
              <span>
                in play <b style={{ color: C.fg, fontVariantNumeric: "tabular-nums" }}>{g.in_round}</b>
              </span>
              <span>
                bonus <b style={{ color: C.fg, fontVariantNumeric: "tabular-nums" }}>{g.bonus}</b>
              </span>
            </div>
          </div>
        );
      })}
    </AbsoluteFill>
  );
};

// 2. Bars: points in play first, the primary measure.
const ratio = (a: number, b: number) => {
  const r = a / Math.max(b, 1e-9);
  return r >= 10 ? `${Math.round(r)}×` : `${(Math.round(r * 10) / 10).toFixed(1)}×`;
};
const Chart: React.FC = () => {
  const frame = useCurrentFrame();
  const S = data.summary as Record<Key, { in_round_mean: number; final_mean: number }>;
  const byKey = Object.fromEntries(GROUPS.map((g) => [g.key, g])) as Record<Key, Group>;
  const rows: { title: string; values: Record<Key, number>; decimals: number }[] = [
    {
      title: `Points in play, round ${data.round}`,
      values: { gui: byKey.gui.in_round, no_re_mcp: byKey.no_re_mcp.in_round, re_mcp: byKey.re_mcp.in_round },
      decimals: 0,
    },
    {
      title: "Points in play, average of 3 rounds",
      values: { gui: S.gui.in_round_mean, no_re_mcp: S.no_re_mcp.in_round_mean, re_mcp: S.re_mcp.in_round_mean },
      decimals: 1,
    },
    {
      title: "Final score with bonuses, average of 3 rounds",
      values: { gui: S.gui.final_mean, no_re_mcp: S.no_re_mcp.final_mean, re_mcp: S.re_mcp.final_mean },
      decimals: 1,
    },
  ];
  const TRACK = 820;
  return (
    <AbsoluteFill style={{ opacity: fade(frame, CHART.len) }}>
      <Header />
      <div style={{ position: "absolute", left: 210, top: 236, width: 1500 }}>
        {rows.map((row, r) => {
          const max = Math.max(...Object.values(row.values));
          const appear = interpolate(frame, [8 + r * 22, 22 + r * 22], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
          return (
            <div key={row.title} style={{ display: "flex", alignItems: "center", marginBottom: 46, opacity: appear }}>
              <div style={{ width: 1120 }}>
                <div style={{ fontSize: 20, color: C.fg2, marginBottom: 10 }}>{row.title}</div>
                {GROUPS.map((g, i) => {
                  const grow = interpolate(frame, [12 + r * 22 + i * 4, 40 + r * 22 + i * 4], [0, 1], {
                    extrapolateLeft: "clamp",
                    extrapolateRight: "clamp",
                    easing: Easing.out(Easing.cubic),
                  });
                  const v = row.values[g.key];
                  return (
                    <div key={g.key} style={{ display: "flex", alignItems: "center", height: 34 }}>
                      <div style={{ width: 196, fontSize: 19, fontWeight: 600, color: STYLE[g.key].ink }}>{g.label}</div>
                      <div style={{ width: TRACK, height: 14, borderRadius: 7, background: C.panel, overflow: "hidden" }}>
                        <div style={{ width: `${Math.max(1.2, (v / max) * 100) * grow}%`, height: "100%", borderRadius: 7, background: STYLE[g.key].bar }} />
                      </div>
                      <div style={{ width: 90, textAlign: "right", fontSize: 21, fontWeight: 600, color: C.fg, fontVariantNumeric: "tabular-nums" }}>
                        {v.toFixed(row.decimals)}
                      </div>
                    </div>
                  );
                })}
              </div>
              <div style={{ marginLeft: 40 }}>
                <div style={{ fontSize: 42, fontWeight: 800, letterSpacing: "-0.035em", color: C.fg, lineHeight: 1.1 }}>
                  {ratio(row.values.re_mcp, row.values.gui)}
                  <span style={{ fontSize: 19, fontWeight: 500, letterSpacing: 0, color: C.fg2 }}> vs computer use</span>
                </div>
                <div style={{ fontSize: 30, fontWeight: 700, letterSpacing: "-0.02em", color: C.mcp, lineHeight: 1.3 }}>
                  {ratio(row.values.re_mcp, row.values.no_re_mcp)}
                  <span style={{ fontSize: 19, fontWeight: 500, letterSpacing: 0, color: C.fg2 }}> vs MCP without RE</span>
                </div>
              </div>
            </div>
          );
        })}
        <div style={{ fontSize: 18, color: C.fg2, marginTop: -8 }}>Ratios compare the MCP server built with reverse engineering against each other group.</div>
      </div>
    </AbsoluteFill>
  );
};

// 3. The two servers' tools, as the player saw them.
const TOOL_TEXT: Record<string, string> = {
  "re_mcp:get_state": "Every object in flight, the score and the mode, read from memory",
  "re_mcp:slash": "One straight slash between two points",
  "re_mcp:auto_play": "Plays on its own: reads memory many times a second, leads fruit, seeks combos, avoids bombs",
  "re_mcp:start_mode": "Navigates the menus to a mode",
  "re_mcp:restart_round": "Retries from the game-over screen",
  "no_re_mcp:get_state": "Screen state, score and lives, read from the screen",
  "no_re_mcp:start_round": "Navigates the menus; leaves a running round alone",
  "no_re_mcp:play": "Plays on its own: tracks fruit and bombs on screen, slices, avoids bombs",
  "no_re_mcp:swipe": "One blade stroke between two points",
  "no_re_mcp:stop_play": "Stops the play loop",
  "no_re_mcp:game_control": "Starts, stops or restarts the game process",
};
const AUTONOMOUS = new Set(["auto_play", "play"]);
const Tools: React.FC = () => {
  const frame = useCurrentFrame();
  const columns: { key: Key; pill: string; how: string; tools: { name: string; description?: string }[] }[] = [
    { key: "no_re_mcp", pill: "Built without reverse engineering", how: "Sees the game through screen captures", tools: data.tools_no_re_mcp },
    { key: "re_mcp", pill: "Built with reverse engineering", how: "Reads the game's memory", tools: data.tools_re_mcp },
  ];
  return (
    <AbsoluteFill style={{ opacity: fade(frame, TOOLS.len) }}>
      <Header subtitle="Two MCP servers, each built by Kimi K3 in a 3-hour session" />
      {columns.map((col, c) => (
        <div key={col.key} style={{ position: "absolute", left: 150 + c * 840, top: 230, width: 780 }}>
          <span
            style={{
              display: "inline-block",
              padding: "4px 14px",
              borderRadius: 999,
              background: C.accentSoft,
              color: C.accentInk,
              fontSize: 17,
              fontWeight: 700,
              letterSpacing: "0.04em",
              textTransform: "uppercase",
            }}
          >
            {col.pill}
          </span>
          <div style={{ display: "flex", alignItems: "baseline", gap: 18, marginTop: 10 }}>
            <span style={{ fontSize: 60, fontWeight: 800, letterSpacing: "-0.03em", color: C.fg }}>{col.tools.length} tools</span>
            <span style={{ display: "flex", alignItems: "center", fontSize: 22, color: C.fg2 }}>
              <Dot k={col.key} size={12} />
              {col.how}
            </span>
          </div>
          <div style={{ marginTop: 22, display: "grid", gap: 12 }}>
            {col.tools.map((t, i) => {
              const show = interpolate(frame, [10 + c * 8 + i * 5, 22 + c * 8 + i * 5], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
              const auto = AUTONOMOUS.has(t.name);
              return (
                <div
                  key={t.name}
                  style={{
                    opacity: show,
                    transform: `translateY(${(1 - show) * 10}px)`,
                    display: "flex",
                    alignItems: "center",
                    gap: 18,
                    padding: "12px 16px",
                    borderRadius: 16,
                    background: auto ? C.accentSoft : C.bg,
                    border: `1.5px solid ${auto ? C.accent : "transparent"}`,
                    boxShadow: SHADOW,
                  }}
                >
                  <code style={{ fontFamily: MONO, fontSize: 19, fontWeight: 600, padding: "3px 9px", borderRadius: 5, background: C.panel, color: C.fg, flex: "none" }}>
                    {t.name}
                  </code>
                  <span style={{ fontSize: 19, lineHeight: 1.35, color: C.fg }}>
                    {TOOL_TEXT[`${col.key}:${t.name}`] ?? t.description ?? ""}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      ))}
      <div style={{ position: "absolute", left: 0, right: 0, top: 930, textAlign: "center", fontSize: 20, color: C.fg2 }}>
        In each round shown, the player called only the highlighted tool, which then played the whole round.
      </div>
    </AbsoluteFill>
  );
};

// 4. The three rounds side by side, synchronised from the round start, at 2x.
const Stats: React.FC<{ g: Group; t: number }> = ({ g, t }) => {
  const score = scoreAt(g, t);
  const action = actionAt(g, t);
  const busy = action !== "thinking" && action !== "round over";
  return (
    <div style={{ marginTop: 18 }}>
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
        <span style={{ fontSize: 19, color: C.fg2 }}>score</span>
        <span style={{ fontSize: 44, fontWeight: 700, letterSpacing: "-0.02em", color: C.fg, fontVariantNumeric: "tabular-nums" }}>{score}</span>
      </div>
      <div style={{ height: 6, borderRadius: 3, background: C.panel, overflow: "hidden", marginTop: 6 }}>
        <div style={{ width: `${(score / maxFinal) * 100}%`, height: "100%", borderRadius: 3, background: STYLE[g.key].bar }} />
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", marginTop: 12, fontSize: 19, color: C.fg2 }}>
        <span>
          tool calls <b style={{ color: C.fg, fontVariantNumeric: "tabular-nums" }}>{callsAt(g, t)}</b>
        </span>
        <span style={{ fontFamily: MONO, fontWeight: busy ? 600 : 400, color: busy ? STYLE[g.key].ink : C.fg2 }}>{action}</span>
      </div>
    </div>
  );
};

const TopClock: React.FC<{ t: number }> = ({ t }) => (
  <div style={{ position: "absolute", top: 70, left: 0, right: 0, display: "flex", justifyContent: "center", alignItems: "center", gap: 16 }}>
    <span style={{ fontSize: 64, fontWeight: 700, letterSpacing: "-0.02em", color: C.fg, fontVariantNumeric: "tabular-nums" }}>{clock(t)}</span>
    <span style={{ fontSize: 18, fontWeight: 600, color: C.fg2, background: C.panel, borderRadius: 999, padding: "3px 10px" }}>2×</span>
  </div>
);

const Replay: React.FC = () => {
  const frame = useCurrentFrame();
  const t = (frame / FPS) * SPEED;
  return (
    <AbsoluteFill style={{ opacity: interpolate(frame, [0, 10], [0, 1], { extrapolateRight: "clamp" }) }}>
      <TopClock t={t} />
      {GROUPS.map((g, i) => (
        <div key={g.key} style={{ position: "absolute", left: panelX(i), top: 200, width: PANEL_W }}>
          <Label g={g} right={clock(Math.min(t, g.round_s))} />
          <Media>
            <OffthreadVideo
              src={staticFile(g.video)}
              startFrom={Math.round(g.video_start_s * FPS)}
              playbackRate={SPEED}
              muted
              style={{ width: "100%", height: "100%", objectFit: "cover" }}
            />
          </Media>
          <Stats g={g} t={t} />
        </div>
      ))}
    </AbsoluteFill>
  );
};

// 5. Hold on the settled result screens.
const Hold: React.FC = () => {
  const t = REPLAY_SOURCE_S;
  return (
    <AbsoluteFill>
      <TopClock t={t} />
      {GROUPS.map((g, i) => (
        <div key={g.key} style={{ position: "absolute", left: panelX(i), top: 200, width: PANEL_W }}>
          <Label g={g} right={clock(g.round_s)} />
          <Media>
            <Img src={staticFile(g.result)} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
          </Media>
          <Stats g={g} t={t} />
        </div>
      ))}
    </AbsoluteFill>
  );
};

export const Showcase: React.FC = () => (
  <AbsoluteFill style={{ background: C.bg, fontFamily: SANS, color: C.fg }}>
    <Sequence from={INTRO.from} durationInFrames={INTRO.len}>
      <Intro />
    </Sequence>
    <Sequence from={CHART.from} durationInFrames={CHART.len}>
      <Chart />
    </Sequence>
    <Sequence from={TOOLS.from} durationInFrames={TOOLS.len}>
      <Tools />
    </Sequence>
    <Sequence from={REPLAY.from} durationInFrames={REPLAY.len}>
      <Replay />
    </Sequence>
    <Sequence from={HOLD.from} durationInFrames={HOLD.len}>
      <Hold />
    </Sequence>
  </AbsoluteFill>
);
