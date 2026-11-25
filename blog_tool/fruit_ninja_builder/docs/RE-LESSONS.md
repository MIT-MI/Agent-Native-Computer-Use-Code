# Reverse engineering vs. no reverse engineering: what made run 03 work

Run 03 (`windows-variants-03`, 2026-11-16) compared three ways to play Windows Fruit Ninja HD with
Kimi K3 as Builder, reviewer and player. Each Builder had a 3-hour budget; each group played three
Arcade rounds. The tables below can be regenerated from any experiment directory with
`python3 scripts/analyze.py <experiment_dir>`.

| Group                | In-round score (final score)    | Mean in-round (final) |
| -------------------- | ------------------------------- | --------------------- |
| MCP built with RE    | 202 (287), 243 (328), 229 (299) | **224.7** (304.7)     |
| MCP built without RE | 40 (115), 137 (198), 93 (109)   | 90 (140.7)            |
| GUI                  | 9 (74), 6 (66), 3 (63)          | 6 (67.7)              |

For reference, the RE group scored 69 in-round in run 02 with the same model. The hand-guided,
REA-built MCP (Codex) scored 261 in-round when DeepSeek played it.

The in-round score excludes end-of-round bonuses. Those bonuses reward odd behaviour, such as
"Bomb Lover" for hitting bombs and "No Bananas Sliced" for missing every banana, so the in-round
score is the primary measure.

This note records how we got there, so the next game starts from it.

## 1. Locking the no-RE group out of reverse engineering

Asking a Builder not to reverse engineer is not enough. Removing tools is not enough either. In run 02
the RE Builder did its RTTI scan and memory search in plain Python, with no toolbox at all. The
boundary has to be permissions, and every path has to be checked by trying it as the agent.

**Separate user.** The no-RE Builder's commands, its reviewer, trial MCPs and the delivered MCP at
play time all run as `agent` (uid 10002). The game runs as `player` (uid 10001). Without
`CAP_SYS_PTRACE`, the kernel refuses everything below across users:

- `/proc/<game>/mem`
- `/proc/<game>/maps`
- `ptrace`
- signals to the game

**Closed files.** The following are not readable by `agent`:

- the game's home, executable and Wine prefix (mode 0700);
- the game's logs;
- the game template, which is mounted under a root-only `/opt/fn-private`.

**No analysis tools.** The `no-re` image is the solve image with these removed:

- binutils' analysis tools: `objdump`, `readelf`, `strings`, `nm`, `addr2line`, `size`, `objcopy`,
  `c++filt`, `elfedit` and `gprof`, including the `i686-w64-mingw32-` versions;
- `winedbg`.

The Builder toolbox (Kuna, rizin, gdb, strace, ltrace, Frida, xxd and the rest) was never in that
image. The image build fails if any of these tools is still present.

**No memory service.** `fn-memd`, the read-only memory helper of the RE group, never starts in
no-RE desktops.

**Game control through one narrow door.** `fn-app start|stop|restart|status` reaches the game's user
through a root helper (`fn-appd`) that accepts only those four words.

**What remains, and why.** The agent keeps what playing through the window needs:

- the X display, for capture and input;
- Python with Pillow and numpy, ffmpeg and xdotool;
- Node with the MCP runtime;
- `/output`.

Wine and the MinGW compiler also remain. As `agent` they run in the agent's own prefix and
wineserver, and cannot reach the game.

**The lock holds at play time.** The no-RE MCP plays as `agent` on the `no-re` image. The brief and
the reviewer still ask for no reverse engineering. That request is now redundant, but it keeps
everyone's intent explicit.

We verified the lock on reef by attempting each path as `agent`:

- **Denied:** reading memory, reading maps, `ptrace`, listing or reading the game's directories,
  signalling the game.
- **Absent:** every analysis tool listed above.
- **Allowed:** screenshots, input, `/output`, and `fn-app` through the helper, which refuses any
  other input.

The no-RE Builder never used reverse engineering in any run.

## 2. Making the model actually reverse engineer

Run 02's RE Builder delivered a pixel-based MCP. Reading its transcript at the failure point, rather
than its summary, showed that it had been one step from success:

1. **It found the right objects.** It found the `Fruit` and `Bomb` vtables, `0x551be0` and
   `0x551b28`, which match the layout the evaluator uses. It also dumped the live fruit objects.
2. **It filtered out its own answer.** It kept only floats in `0 < v < 1024`, the screen's pixel
   range. The game stores positions in its own units: a centred origin, y pointing up, 480 units
   wide. The y field and every left-half x are therefore negative and were filtered away. The
   x values that remained (33.6, 221.5, ...) were read as "timers and scales".
3. **It measured a finished round.** It compared two snapshots after a Classic round had already
   ended, saw only two objects change, and concluded that the pool slots were not the on-screen
   fruit.
4. **It read the wrong function.** It decompiled the fruit initialiser instead of the motion
   function, although both were listed in the vtable.
5. **Smaller slips.** An early RTTI grep was cut off at 40 lines, so the game classes were missed.
   A hard-coded PID was used after a game restart, which led to the conclusion "too fragile".
6. **It settled.** Pixels already scored 110–160 and reverse engineering was optional, so it
   delivered pixels with 51 minutes of budget left.

Each failure got a fix in the code or the brief. None of the fixes contains an address, an offset or
the coordinate formula.

- **The brief makes RE the expected path.** It reads: "Reverse engineer the game and build the MCP
  on what you recover." It lists the state the game holds exactly (object kinds including every
  special, positions, motion, score, clock, screen) and allows pixels only for what could not be
  recovered.
- **The brief gives generic method aimed at the traps above:**
  - RTTI leads to vtables, and live objects start with a vtable pointer.
  - Use the collection the game itself iterates.
  - Confirm fields during play, across several objects and moments.
  - The game's own units, origin and axes mean values may be small or negative, so fit a transform
    from several observations and never search or filter memory for pixel values.
  - Process ids change across restarts; addresses in the executable do not.
- **`memtool` turns the method into the default.** It is a generic, read-only memory inspector in
  the RE toolbox:
  - It finds the game's current process on every call.
  - `rtti` lists every class with its vtables.
  - `dump` shows every word as hex, unsigned, signed and float, with no filtering.
  - `watch` samples objects during play and labels each changing field as constant velocity or
    constant acceleration. If nothing moves, it says to measure during play.

  On the live game, `memtool watch --vtable <Fruit>` immediately reports `+0x10` as linear (x),
  `+0x14` as quadratic (y, about −500/s²) and `+0x20` as linear (vertical velocity).

- **The memory access path works whatever the host allows.**
  - Before run 03, reef's `kernel.yama.ptrace_scope` had changed to 1. With that setting, even the
    game's own user can no longer read `/proc/<game>/mem` from a sibling process. The run would
    have failed silently.
  - We did not touch the shared host. Instead `fn-memd`, a root helper (`CAP_SYS_PTRACE` in those
    desktops only), serves `pid`, `maps` and `read ADDR LEN` for the game process only. It never
    writes and cannot reach any other process.
  - It runs only for RE builds and RE MCP rounds. `memtool` falls back to it, and the brief
    documents it for play time.
- **Evidence, not gates.**
  - For RE builds, `try` and `finish` report whether the MCP's files read the game's memory.
  - The reviewer is asked to flag pixels used where memory is exact.
  - The pattern first missed reads through `fn-mem.sock` (a false negative, fixed in `08ddab5`).
    The Builder ignored it, but evidence must match every access path we provide.
- **Use the whole budget.**
  - The brief asks the Builder to judge the MCP by measured play over whole rounds.
  - A first `finish` with more than 15% of the budget left only asks for confirmation.

**The result.** Run 03's RE Builder used `memtool` heavily: `find` 19 times, `dump` 4, `rtti` 2,
`watch` 2. It recovered:

- the 30-slot fruit and bomb pools, with their active flag, kind, sliced flag, position and velocity;
- the exact world-to-screen transform, `sx = 512 + 2.1333·wx`, `sy = 384 − 2.1333·wy`;
- the fruit-type table;
- the score global.

It delivered an MCP that reads memory through `fn-mem.sock`, after 2 h 33 min of its 3-hour budget.

## 3. Why the results were good

- **Exact state beats pixels.** From memory, the MCP knows what each object is, where it is and how
  it moves. Across three rounds it sliced every visible fruit (70/70, 79/79, 92/92), every special
  (6/6) and no bomb. The pixel-based no-RE MCP sliced 86–90% of fruit and hit 2–3 bombs per round.
  Each bomb costs 10 points (clamped at 0), hence its lower and noisier in-round score.
- **The player's latency is out of the loop.** The brief asks the Builder to decide which choices
  the slow player makes and which one call makes itself. The RE MCP's `auto_play` runs a 60 Hz
  control loop that predicts motion, seeks combos of three or more and avoids bombs. The player
  called it once or twice per round. GUI play, where every stroke waits for a model reply, stays
  in single digits.
- **Clean rounds.** Run 01 had MCPs that cycled pause and resume or restarted a running round. The
  brief now says a running round is the one to play and that pausing to win time changes the
  game's timing. Rounds record paused time and restarts: all nine rounds of run 03 had none.
- **A stronger Builder used its budget.** K3 replaced DeepSeek V4.1 Flash as Builder, and the
  early-finish confirmation kept both Builders working: the RE Builder used 85% of its budget and
  the no-RE Builder 89%.

**Caveats.**

- Each group had one build and three rounds, and the game has no seed. The direction is clear; the
  ratios are not yet stable.
- All three groups used K3 as player, so these GUI numbers are not comparable with the earlier
  DeepSeek GUI numbers.

## Checklist for the next target

1. Lock a no-RE condition with permissions (a separate user, closed files, no memory service), not
   with instructions or tool removal alone. Verify every path by trying it as the agent.
2. Before each run, verify the real access path end to end on the actual host: memory reads, input,
   capture. Host policies such as ptrace scope change under you.
3. When a Builder fails, read its commands and outputs at the failure point. The cause is usually
   one concrete mistake, not a lack of ability.
4. Prefer tools that make the right method the default over more prose: unfiltered dumps, live
   motion classification, current-pid lookup.
5. Keep game-specific knowledge out of briefs and tools. Teach methods, not answers.
6. Report the in-round score, and record pauses and restarts.
7. Make evidence checks match every access path you provide.

## Scores

In-round score excludes end-of-round bonuses; final = in-round + bonus. Two RE rounds scored their
last point in the same evaluator sample that showed the result screen; `result.json` records 201 and
242 there, and the tables use the corrected 202 and 243 (fixed in the evaluator afterwards).

**Run 03, every round.** Rounds ran one at a time after both builds, the group order rotating per
round.

| Group                | Round | In-round | Bonus | Final |
| -------------------- | ----: | -------: | ----: | ----: |
| MCP built with RE    |     1 |      202 |    85 |   287 |
|                      |     2 |      243 |    85 |   328 |
|                      |     3 |      229 |    70 |   299 |
| MCP built without RE |     1 |       40 |    75 |   115 |
|                      |     2 |      137 |    61 |   198 |
|                      |     3 |       93 |    16 |   109 |
| GUI                  |     1 |        9 |    65 |    74 |
|                      |     2 |        6 |    60 |    66 |
|                      |     3 |        3 |    60 |    63 |

**Run 03, how each MCP played.** Counted by `scripts/analyze.py` from the evaluator's memory event
log (`viewer-events.jsonl`). "Visible fruit" counts completed moving fruit episodes that entered the
arena before the round ended (disappearing above the bottom edge counts as sliced; falling past the
bottom edge counts as dropped). "Specials" are the Arcade bananas (kind 17 and above). Bombs are
estimated from score decreases during play (bombs subtract 10 points, clamped at 0, and are the only
penalty in Arcade; zero-to-zero hits are invisible). The GUI's event log holds no entities by design,
so its coverage is not counted; it made 23–26 tool calls per round and had score drops in two of its
rounds.

| Group                | Round | Visible fruit sliced | Specials sliced | Bombs hit | Player's calls                 |
| -------------------- | ----: | -------------------- | --------------- | --------: | ------------------------------ |
| MCP built with RE    |     1 | 70 / 70 (100%)       | 1 / 1           |         0 | `auto_play` ×1                 |
|                      |     2 | 79 / 79 (100%)       | 2 / 2           |         0 | `auto_play` ×1                 |
|                      |     3 | 92 / 92 (100%)       | 3 / 3           |         0 | `auto_play` ×2, `get_state` ×1 |
| MCP built without RE |     1 | 70 / 81 (86%)        | 3 / 3           |        ~3 | `play` ×1                      |
|                      |     2 | 95 / 106 (90%)       | 3 / 5           |        ~3 | `play` ×1                      |
|                      |     3 | 94 / 108 (87%)       | 3 / 4           |        ~2 | `play` ×1                      |

**Across runs.** Mean in-round score (mean final score) over three rounds per group.

| Run                                    | Builder / player            | No-RE condition         | RE-built MCP      | No-RE-built MCP | GUI       |
| -------------------------------------- | --------------------------- | ----------------------- | ----------------- | --------------- | --------- |
| Reference: REA-built MCP (hand-guided) | Codex / DeepSeek V4.1 Flash | —                       | 261 (295.0)       | —               | 10 (78.7) |
| Run 01                                 | DeepSeek V4.1 Flash / same  | instruction, no toolbox | 72 (106.3)        | 2 (100.7)       | 11 (72.3) |
| Run 02                                 | Kimi K3 / same              | instruction, no toolbox | 69 (132.7)        | 41 (154.7)      | 16 (78.0) |
| Run 03                                 | Kimi K3 / same              | locked                  | **224.7 (304.7)** | 90 (140.7)      | 6 (67.7)  |

Run 01's RE MCP lost about 18 game seconds per round to its own pause-and-resume start bug; run 01's
no-RE MCP restarted the round twice. Run 02 and run 03 had no pauses or restarts. Players differ
between the reference, run 01 and runs 02–03, so compare columns within a run.
