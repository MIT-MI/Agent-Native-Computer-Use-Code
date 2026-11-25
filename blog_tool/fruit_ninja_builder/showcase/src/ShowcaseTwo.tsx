import React from "react";
import { AbsoluteFill, Easing, Img, OffthreadVideo, Sequence, interpolate, spring, staticFile, useCurrentFrame } from "remotion";
import { loadFont as loadInter } from "@remotion/google-fonts/Inter";
import { loadFont as loadMono } from "@remotion/google-fonts/JetBrainsMono";
import data from "../data.json";
import buildData from "../build.json";

// Two-way version of the showcase: Computer Use against the MCP server the model built.
const { fontFamily: SANS } = loadInter("normal", { weights: ["400", "500", "600", "700"], subsets: ["latin"] });
const { fontFamily: MONO } = loadMono("normal", { weights: ["400", "700"], subsets: ["latin"] });

const C = { page: "#FFFFFF", card: "#F5F5F7", text: "#1D1D1F", muted: "#86868B", faint: "#D2D2D7", gui: "#FF9500", re: "#007AFF", good: "#34C759" };

type Key = "gui" | "re_mcp";
type Group = { key: string; label: string; video: string; result: string; video_start_s: number; round_s: number; in_round: number; final: number; bonus: number; scores: number[][]; calls: (number | string | null)[][] };
const ALL = data.groups as unknown as Group[];
const GROUPS = (["gui", "re_mcp"] as Key[]).map((k) => ALL.find((g) => g.key === k)!);
const COLUMNS: { name: string; color: string }[] = [
  { name: "Computer Use", color: C.gui },
  { name: "MCP", color: C.re },
];
const SUMMARY = data.summary as Record<string, { in_round_mean: number; final_mean: number }>;

type BuildCall = { t: number; stage: number; tool: string; args: string };
type Build = { model: string; minutes: number; calls: BuildCall[]; shots: { t: number; file: string }[]; tries: { t: number; tools: number }[]; tools: number; moments: { t: number; stage: number; text: string }[] };
const build = buildData as unknown as Build;

// Timeline (30 fps).
const FPS = 30;
const SPEED = 2;
const COMPARE = { from: 0, len: 150 };
const STATS = { from: 150, len: 150 };
const BUILD = { from: 300, len: 390 };
const BUILD_LEAD = 20, BUILD_PLAY = 330;
const REPLAY_SOURCE_S = 76;
const REPLAY = { from: BUILD.from + BUILD.len, len: Math.round((REPLAY_SOURCE_S / SPEED) * FPS) };
const HOLD = { from: REPLAY.from + REPLAY.len, len: 90 };
export const TOTAL_FRAMES_TWO = HOLD.from + HOLD.len;

// Layout: 4:3 cards.
const CH = 531, CW = 708, TOP = 236, GAP = 60;
const X = [(1920 - 2 * CW - GAP) / 2, (1920 - 2 * CW - GAP) / 2 + CW + GAP];
const BW = 850, BX = [80, 990];
const clamp = { extrapolateLeft: "clamp", extrapolateRight: "clamp" } as const;
const ease = (f: number, a: number, b: number) => interpolate(f, [a, b], [0, 1], { ...clamp, easing: Easing.bezier(0.25, 0.1, 0.25, 1) });
const pop = (f: number, delay = 0) => spring({ frame: f - delay, fps: FPS, config: { damping: 12, stiffness: 140, mass: 0.7 } });
const times = (r: number) => (r >= 10 ? `${Math.round(r)}×` : `${r.toFixed(1)}×`);
const mmss = (s: number) => {
  const t = Math.max(0, Math.floor(s));
  return `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
};
const scoreAt = (g: Group, t: number) => {
  let s = 0;
  for (const [at, v] of g.scores) if (at <= t) s = v;
  return s;
};
const maxFinal = Math.max(...GROUPS.map((g) => g.final));

// ---------- pieces ----------
const Heading: React.FC<{ subtitle?: string }> = ({ subtitle }) => (
  <div style={{ position: "absolute", top: 30, width: "100%", textAlign: "center", fontFamily: SANS }}>
    <div style={{ fontSize: 68, fontWeight: 700, letterSpacing: -2, color: C.text }}>Fruit Ninja</div>
    <div style={{ fontSize: 24, color: C.muted, marginTop: 2 }}>{subtitle ?? `Kimi K3 on the same game, round ${data.round} of 3`}</div>
  </div>
);

const Label: React.FC<{ left: number; width: number; name: string; color: string; time?: string; done?: boolean }> = ({ left, width, name, color, time, done }) => (
  <div style={{ position: "absolute", left: left + 4, top: TOP - 56, width: width - 8, display: "flex", alignItems: "center", gap: 12 }}>
    <div style={{ width: 13, height: 13, borderRadius: 7, background: color }} />
    <div style={{ fontSize: 28, fontWeight: 600, color: C.text, letterSpacing: -0.3, whiteSpace: "nowrap" }}>{name}</div>
    {time ? <div style={{ marginLeft: "auto", fontSize: 28, fontWeight: done ? 700 : 500, color: done ? C.text : C.muted, fontVariantNumeric: "tabular-nums" }}>{time}</div> : null}
  </div>
);

const Card: React.FC<{ left: number; width?: number; children: React.ReactNode }> = ({ left, width = CW, children }) => (
  <div style={{ position: "absolute", left, top: TOP, width, height: CH, borderRadius: 24, background: C.card, overflow: "hidden" }}>{children}</div>
);

const Badge: React.FC<{ f: number }> = ({ f }) => (
  <div style={{ width: 60, height: 60, borderRadius: 30, background: C.good, transform: `scale(${pop(f)})`, display: "flex", alignItems: "center", justifyContent: "center", boxShadow: "0 10px 30px rgba(0,0,0,0.15)" }}>
    <svg width={30} height={30} viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth={3.6} strokeLinecap="round" strokeLinejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5" /></svg>
  </div>
);

const Pill: React.FC<{ label: string; value: number; f: number; delay: number }> = ({ label, value, f, delay }) => {
  const s = pop(f, delay);
  return (
    <div style={{ display: "inline-flex", alignItems: "center", gap: 8, padding: "7px 16px", borderRadius: 30, background: C.card, fontSize: 20, fontWeight: 500, color: C.muted, opacity: Math.min(1, s), transform: `translateY(${(1 - s) * 14}px)` }}>
      {label}
      <b style={{ color: C.text, fontWeight: 700, fontVariantNumeric: "tabular-nums" }}>{value}</b>
    </div>
  );
};

// ---------- scene 1: the two result screens ----------
const Compare: React.FC = () => {
  const frame = useCurrentFrame();
  const local = frame + 40;
  return (
    <AbsoluteFill style={{ opacity: 1 - ease(frame, COMPARE.len - 12, COMPARE.len) }}>
      <Heading />
      {GROUPS.map((g, i) => (
        <React.Fragment key={g.key}>
          <Label left={X[i]} width={CW} name={COLUMNS[i].name} color={COLUMNS[i].color} />
          <Card left={X[i]}><Img src={staticFile(g.result)} style={{ width: "100%", height: "100%", objectFit: "cover" }} /></Card>
          <div style={{ position: "absolute", left: X[i], top: TOP + CH + 26, width: CW }}>
            <div style={{ fontSize: 26, fontWeight: 700, color: C.text, marginBottom: 14, fontVariantNumeric: "tabular-nums", opacity: ease(local, 20, 34) }}>{`${g.final} points`}</div>
            <div style={{ display: "flex", gap: 10 }}>
              <Pill label="in play" value={g.in_round} f={local} delay={30} />
              <Pill label="bonus" value={g.bonus} f={local} delay={32} />
            </div>
          </div>
        </React.Fragment>
      ))}
    </AbsoluteFill>
  );
};

// ---------- scene 2: the numbers, each with its ratio ----------
const Stats: React.FC = () => {
  const local = useCurrentFrame();
  const [gui, re] = GROUPS;
  const rows = [
    { label: `Points in play, round ${data.round}`, values: [gui.in_round, re.in_round], decimals: 0, word: "more points in play" },
    { label: "Points in play, average of 3 rounds", values: [SUMMARY.gui.in_round_mean, SUMMARY.re_mcp.in_round_mean], decimals: 1, word: "more points in play" },
    { label: "Final score with bonuses, average of 3 rounds", values: [SUMMARY.gui.final_mean, SUMMARY.re_mcp.final_mean], decimals: 1, word: "higher final score" },
  ];
  return (
    <AbsoluteFill style={{ opacity: Math.min(ease(local, 0, 10), 1 - ease(local, STATS.len - 12, STATS.len)) }}>
      <Heading />
      <div style={{ position: "absolute", top: 250, left: 150, right: 150, display: "flex", flexDirection: "column", gap: 58 }}>
        {rows.map((row, r) => {
          const max = Math.max(...row.values);
          const grow = ease(local, 4 + r * 6, 26 + r * 6);
          return (
            <div key={row.label} style={{ display: "flex", alignItems: "center", gap: 50, opacity: ease(local, r * 6, 10 + r * 6) }}>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 24, color: C.muted, marginBottom: 12 }}>{row.label}</div>
                {COLUMNS.map(({ name, color }, i) => (
                  <div key={name} style={{ display: "flex", alignItems: "center", gap: 22, height: 44 }}>
                    <div style={{ width: 210, fontSize: 24, fontWeight: 600, color }}>{name}</div>
                    <div style={{ flex: 1, height: 18, borderRadius: 9, background: "#EFEFF4" }}>
                      <div style={{ width: `${Math.max(0.8, (row.values[i] / max) * 100) * grow}%`, height: "100%", borderRadius: 9, background: color }} />
                    </div>
                    <div style={{ width: 130, fontSize: 28, fontWeight: 700, color: C.text, textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{row.values[i].toFixed(row.decimals)}</div>
                  </div>
                ))}
              </div>
              <div style={{ width: 330, opacity: ease(local, 22 + r * 6, 36 + r * 6) }}>
                <div style={{ fontSize: 76, fontWeight: 700, letterSpacing: -3, lineHeight: 1, color: C.text, fontVariantNumeric: "tabular-nums" }}>{times(row.values[1] / row.values[0])}</div>
                <div style={{ fontSize: 24, color: C.muted, marginTop: 6 }}>{row.word}</div>
              </div>
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};

// ---------- scene 3: how the model built its MCP server ----------
const STEPS = [
  { name: "Analyze", fill: "#DCDCE1", ink: "#8E8E93" },
  { name: "Test", fill: "#F1E6D0", ink: "#B08D4C" },
  { name: "Build", fill: "#D6BD8C", ink: "#8A6B2F" },
  { name: "Check", fill: "#BFE3CF", ink: "#2E9E66" },
];
const ROW = 32;
const weight = (c: BuildCall) => (c.t >= 20 && c.t < 45 ? 2 : 1);
const CUM = (() => { let s = 0; return build.calls.map((c) => (s += weight(c))); })();
const TOTAL_WEIGHT = CUM[CUM.length - 1] || 1;
const callsAt = (local: number) => {
  const w = interpolate(local, [BUILD_LEAD, BUILD_LEAD + BUILD_PLAY], [0, TOTAL_WEIGHT], clamp);
  let n = 0;
  while (n < CUM.length && CUM[n] <= w) n++;
  return n;
};
const frameOfBuildTime = (t: number) => {
  const n = build.calls.findIndex((c) => c.t >= t);
  return BUILD_LEAD + ((n < 0 ? TOTAL_WEIGHT : CUM[n]) / TOTAL_WEIGHT) * BUILD_PLAY;
};
const countUpTo = <T extends { t: number }>(rows: T[], t: number) => rows.filter((r) => r.t <= t).length;
const SPANS = (() => {
  const out: { from: number; to: number; stage: number }[] = [];
  build.calls.forEach((c, i) => {
    const to = i + 1 < build.calls.length ? build.calls[i + 1].t : build.minutes;
    const last = out[out.length - 1];
    if (last && last.stage === c.stage) last.to = to;
    else out.push({ from: out.length ? c.t : 0, to, stage: c.stage });
  });
  return out;
})();
const QUOTES = (build.moments ?? []).map((m) => ({ ...m, frame: frameOfBuildTime(m.t) }));

const BuildScene: React.FC = () => {
  const local = useCurrentFrame();
  const n = callsAt(local);
  const done = n === build.calls.length;
  const t = done ? build.minutes : n ? build.calls[n - 1].t : 0;
  const current = n ? build.calls[n - 1] : null;
  const shotIndex = countUpTo(build.shots, t);
  const shot = build.shots.length ? build.shots[Math.max(0, shotIndex - 1)].file : null;
  const tryIndex = countUpTo(build.tries, t);
  const tools = tryIndex ? build.tries[tryIndex - 1].tools : 0;
  const counts = [0, 0, 0, 0];
  for (let i = 0; i < n; i++) counts[build.calls[i].stage - 1]++;
  const shown = build.calls.slice(Math.max(0, n - 7), n);
  const quote = [...QUOTES].reverse().find((q) => local >= q.frame);
  const left = BX[0] + (BW - CW) / 2;
  const tile = (BW - 3 * 14) / 4;
  return (
    <AbsoluteFill style={{ opacity: Math.min(ease(local, 0, 10), 1 - ease(local, BUILD.len - 12, BUILD.len)) }}>
      <Heading subtitle={`First, ${build.model} builds its MCP server: ${Math.round(build.minutes)} minutes, ${build.calls.length} tool calls`} />
      <Label left={left} width={CW} name="Fruit Ninja, as the agent tests it" color={C.muted} time={mmss(t * 60)} done={done} />
      <Card left={left}>
        {shot ? <Img src={staticFile(shot)} style={{ width: "100%", height: "100%", objectFit: "cover" }} /> : null}
        {current ? (
          <div style={{ position: "absolute", left: 18, top: 16, display: "flex", alignItems: "center", gap: 10, padding: "7px 16px 7px 8px", borderRadius: 26, background: C.page, boxShadow: "0 4px 16px rgba(0,0,0,0.12)" }}>
            <div style={{ width: 28, height: 28, borderRadius: 14, background: STEPS[current.stage - 1].ink, color: "#fff", fontSize: 16, fontWeight: 700, display: "flex", alignItems: "center", justifyContent: "center" }}>{current.stage}</div>
            <span style={{ fontSize: 21, fontWeight: 600, color: C.text }}>{STEPS[current.stage - 1].name}</span>
          </div>
        ) : null}
      </Card>
      {done ? <div style={{ position: "absolute", left: left + CW - 80, top: TOP + 20 }}><Badge f={local - (BUILD_LEAD + BUILD_PLAY)} /></div> : null}

      <Label left={BX[1]} width={BW} name="The build, step by step" color={C.re} />
      <div style={{ position: "absolute", left: BX[1], top: TOP, width: BW, display: "flex", gap: 14 }}>
        {STEPS.map((s, i) => {
          const on = current?.stage === i + 1 && !done;
          return (
            <div key={s.name} style={{ width: tile, height: 112, borderRadius: 18, background: on ? s.fill : C.card, padding: "14px 16px", boxSizing: "border-box" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
                <div style={{ width: 26, height: 26, borderRadius: 13, background: s.ink, color: "#fff", fontSize: 15, fontWeight: 700, display: "flex", alignItems: "center", justifyContent: "center" }}>{i + 1}</div>
                <span style={{ fontSize: 22, fontWeight: 600, color: C.text }}>{s.name}</span>
              </div>
              <div style={{ marginTop: 10, fontSize: 19, color: C.muted, fontVariantNumeric: "tabular-nums" }}>
                <span style={{ fontSize: 30, fontWeight: 700, color: C.text, letterSpacing: -0.5 }}>{counts[i]}</span> calls
              </div>
            </div>
          );
        })}
      </div>
      <div style={{ position: "absolute", left: BX[1] + 4, top: TOP + 138, width: BW - 8, display: "flex", gap: 60 }}>
        {[["tool calls", String(n)], ["tools in the server", String(tools)]].map(([label, value]) => (
          <div key={label}>
            <div style={{ fontSize: 22, color: C.muted }}>{label}</div>
            <div style={{ fontSize: 62, fontWeight: 700, letterSpacing: -2, lineHeight: 1.1, color: C.text, fontVariantNumeric: "tabular-nums" }}>{value}</div>
          </div>
        ))}
      </div>
      <div style={{ position: "absolute", left: BX[1] + 4, top: TOP + 262, width: BW - 8, height: 7 * ROW + 6, overflow: "hidden", WebkitMaskImage: "linear-gradient(to bottom, transparent 0, #000 34px)" }}>
        <div style={{ position: "absolute", left: 0, right: 0, bottom: 0 }}>
          {shown.map((row, k) => {
            const last = k === shown.length - 1 && !done;
            const ink = STEPS[row.stage - 1].ink;
            return (
              <div key={row.t + row.tool + k} style={{ height: ROW, display: "flex", alignItems: "center", gap: 12, fontFamily: MONO, fontSize: 18, whiteSpace: "nowrap", overflow: "hidden" }}>
                <div style={{ width: 9, height: 9, borderRadius: 5, flex: "none", background: last ? ink : C.faint }} />
                <span style={{ color: last ? ink : C.text, fontWeight: 700, flex: "none" }}>{row.tool}</span>
                <span style={{ color: C.muted, overflow: "hidden", textOverflow: "ellipsis" }}>{row.args}</span>
              </div>
            );
          })}
        </div>
      </div>

      <div style={{ position: "absolute", left: BX[0] + 4, top: TOP + CH + 34, width: 1920 - 2 * BX[0] - 8 }}>
        <div style={{ position: "relative", height: 20, borderRadius: 10, overflow: "hidden", background: C.card }}>
          {SPANS.map((s, i) => (
            <div key={i} style={{ position: "absolute", top: 0, bottom: 0, left: `${(s.from / Math.max(build.minutes, 0.1)) * 100}%`, width: `${((s.to - s.from) / Math.max(build.minutes, 0.1)) * 100}%`, background: STEPS[s.stage - 1].fill, opacity: s.from <= t ? 1 : 0.28 }} />
          ))}
        </div>
        <div style={{ position: "absolute", top: -7, left: `${(t / Math.max(build.minutes, 0.1)) * 100}%`, width: 4, height: 34, marginLeft: -2, borderRadius: 2, background: C.text }} />
        <div style={{ display: "flex", justifyContent: "space-between", marginTop: 12, fontSize: 20, color: C.muted, fontVariantNumeric: "tabular-nums" }}>
          <span>0 min</span>
          <span style={{ display: "flex", gap: 26 }}>
            {STEPS.map((s, i) => (
              <span key={s.name} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <i style={{ width: 14, height: 14, borderRadius: 4, background: s.fill, display: "inline-block" }} />
                {`${i + 1} ${s.name}`}
              </span>
            ))}
          </span>
          <span>{`${Math.round(build.minutes)} min`}</span>
        </div>
      </div>
      {quote ? (
        <div style={{ position: "absolute", left: 150, right: 150, top: TOP + CH + 128, textAlign: "center", opacity: ease(local, quote.frame, quote.frame + 8) }}>
          <div style={{ fontSize: 27, lineHeight: 1.35, fontWeight: 500, color: C.text }}>{`“${quote.text}”`}</div>
          <div style={{ fontSize: 20, color: C.muted, marginTop: 8 }}>{`${build.model}, minute ${quote.t.toFixed(1)} of the build`}</div>
        </div>
      ) : null}
    </AbsoluteFill>
  );
};

// ---------- scene 4: both rounds on one clock, at 2x ----------
const Under: React.FC<{ g: Group; i: number; t: number }> = ({ g, i, t }) => {
  const score = scoreAt(g, t);
  const over = t >= g.round_s;
  const rows = g.calls.filter((c) => (c[0] as number) <= t).map((c) => ({ start: c[0] as number, tool: (c[2] as string) === "get_screenshot" ? "screenshot" : (c[2] as string) }));
  const shown = rows.slice(Math.max(0, rows.length - 4));
  const color = COLUMNS[i].color;
  return (
    <>
      <div style={{ position: "absolute", left: X[i] + 4, top: TOP + CH + 24, width: CW - 8 }}>
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 22, color: C.muted, fontVariantNumeric: "tabular-nums" }}>
          <span>score</span>
          <span style={{ color: C.text, fontWeight: 600 }}>{score}</span>
        </div>
        <div style={{ height: 8, marginTop: 8, borderRadius: 8, background: "#E8E8ED", overflow: "hidden" }}>
          <div style={{ height: "100%", width: `${Math.max(score ? 0.6 : 0, (score / maxFinal) * 100)}%`, borderRadius: 8, background: color }} />
        </div>
      </div>
      <div style={{ position: "absolute", left: X[i] + 4, top: TOP + CH + 92, width: CW - 8 }}>
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 22, color: C.muted, fontVariantNumeric: "tabular-nums", marginBottom: 4 }}>
          <span>tool calls</span>
          <span style={{ color: C.text, fontWeight: 600 }}>{rows.length}</span>
        </div>
        <div style={{ position: "relative", height: ROW * 4, overflow: "hidden", WebkitMaskImage: "linear-gradient(to bottom, transparent 0, #000 30px)" }}>
          <div style={{ position: "absolute", left: 0, right: 0, bottom: 0 }}>
            {shown.map((row, k) => {
              const last = k === shown.length - 1 && !over;
              return (
                <div key={row.start + row.tool + k} style={{ height: ROW, display: "flex", alignItems: "center", gap: 12, fontFamily: MONO, fontSize: 18, whiteSpace: "nowrap" }}>
                  <div style={{ width: 9, height: 9, borderRadius: 5, flex: "none", background: last ? color : C.faint }} />
                  <span style={{ color: last ? color : C.text, fontWeight: 700 }}>{row.tool}</span>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </>
  );
};

const Clock: React.FC<{ t: number; fast: boolean }> = ({ t, fast }) => (
  <div style={{ position: "absolute", top: 40, width: "100%", display: "flex", justifyContent: "center" }}>
    <div style={{ position: "relative", fontSize: 72, fontWeight: 600, color: C.text, letterSpacing: -1, fontVariantNumeric: "tabular-nums" }}>
      {mmss(t)}
      <div style={{ position: "absolute", left: "100%", top: "50%", padding: "4px 14px", borderRadius: 20, background: C.card, fontSize: 22, fontWeight: 600, letterSpacing: 0, whiteSpace: "nowrap", opacity: fast ? 1 : 0, transform: "translate(20px, -50%)", color: C.muted }}>{`×${SPEED}`}</div>
    </div>
  </div>
);

const Replay: React.FC<{ hold?: boolean }> = ({ hold }) => {
  const frame = useCurrentFrame();
  const t = hold ? REPLAY_SOURCE_S : (frame / FPS) * SPEED;
  return (
    <AbsoluteFill style={{ opacity: hold ? 1 : ease(frame, 0, 18) }}>
      <Clock t={t} fast={!hold} />
      {GROUPS.map((g, i) => (
        <React.Fragment key={g.key}>
          <Label left={X[i]} width={CW} name={COLUMNS[i].name} color={COLUMNS[i].color} time={mmss(Math.min(t, g.round_s))} done={t >= g.round_s} />
          <Card left={X[i]}>
            {hold ? (
              <Img src={staticFile(g.result)} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
            ) : (
              <OffthreadVideo src={staticFile(g.video)} startFrom={Math.round(g.video_start_s * FPS)} playbackRate={SPEED} muted style={{ width: "100%", height: "100%", objectFit: "cover" }} />
            )}
          </Card>
          <Under g={g} i={i} t={t} />
        </React.Fragment>
      ))}
    </AbsoluteFill>
  );
};

export const ShowcaseTwo: React.FC = () => (
  <AbsoluteFill style={{ background: C.page, fontFamily: SANS, color: C.text }}>
    <Sequence from={COMPARE.from} durationInFrames={COMPARE.len}><Compare /></Sequence>
    <Sequence from={STATS.from} durationInFrames={STATS.len}><Stats /></Sequence>
    <Sequence from={BUILD.from} durationInFrames={BUILD.len}><BuildScene /></Sequence>
    <Sequence from={REPLAY.from} durationInFrames={REPLAY.len}><Replay /></Sequence>
    <Sequence from={HOLD.from} durationInFrames={HOLD.len}><Replay hold /></Sequence>
  </AbsoluteFill>
);
