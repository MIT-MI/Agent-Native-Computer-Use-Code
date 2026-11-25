# Architecture

The Builder follows a `cua-world` design: guide rather than restrict, keep the harness small, return
evidence rather than verdicts, and never weaken the MCP or strengthen the GUI in the name of parity.
Fairness means the same player model, game files, start state and solve budget.

## Roles

| Role         | Runs as                                     | Reaches                                              | Does not reach                                   |
| ------------ | ------------------------------------------- | ---------------------------------------------------- | ------------------------------------------------ |
| Builder      | host loop; tools act in the builder desktop | game, Wine prefix, toolbox, `/output`, desktop input | network, evaluator code, past results, model key |
| Reviewer     | host loop on the same builder desktop       | the same tools, the last started MCP                 | the Builder's history and `/output`              |
| Deployed MCP | `player` (RE) or `agent` (no-RE), offline   | X11 input/screen, own files (+ memory/Wine for RE)   | `/output`, toolbox, evaluator code, model key    |
| GUI player   | host loop                                   | screenshot, drag and click tools                     | game state, memory                               |
| Evaluator    | root in the solve desktop                   | game memory via `/proc`, recording, Arcade start     | —                                                |

Desktops run with `--network none`. The model key stays in the host process. The evaluator's code is
mounted under `/opt/fn-eval`, which non-root users cannot enter.

## Configuration and data separation

Game-specific facts are kept in data files rather than scattered in code:

- [config/game.json](config/game.json): public game metadata (title, executable, display, viewport,
  target archive URL/hashes, DXVK pin).
- [sandbox/windows/app.json](sandbox/windows/app.json): guest runtime configuration copied to
  `/opt/fn/app.json`.
- [native/re-layout.json](native/re-layout.json): evaluator-only memory addresses, offsets, menu and
  result-screen coordinates, and analysis thresholds. Neither Builder variant ever sees `native/`.
- [src/config.json](src/config.json): model configs, budgets, image names and container labels.

## Builder

The brief is [src/CONTRACT.md](src/CONTRACT.md). Tools:

- `exec` runs a shell command as the desktop's agent user and waits at most `wait_seconds`; a
  command still running keeps running with its log on disk.
- `read_file`, `write_file` and GUI input (`screenshot`, clicks, `drag`, `type_text`, `key`,
  `scroll`).
- `try` snapshots `/output`, deploys only `mcp/` through the same function as finish and solve, runs
  the Builder's calls, and returns results, changed files under the home directory and `/tmp` (after
  calls and after an optional game restart), the MCP's stderr tail, a screenshot, memory-read
  evidence (for RE builds), and whether the player endpoint accepts the tool schemas. `/output` is
  hidden while a trial MCP runs.
- `review` starts a fresh reviewer with the Builder's remaining time; its advice is not approval.
- `finish` asks for confirmation on the first call if more than 15% of the budget remains. When
  accepted, it delivers the current workspace if its MCP starts, otherwise the last snapshot that
  started (`last_started`). Budget expiry delivers `last_started` without a new attempt.

The Builder, reviewer and MCP player share [src/loop.ts](src/loop.ts): no call caps, remaining time
in every observation, tool output cut to its head and tail with the full text on disk, a reminder
instead of an ending when a Builder or reviewer reply has no tool call, retries within the budget,
and summary compaction against the endpoint's context window.

## Solve

Each round starts a fresh desktop from the runtime (or no-RE) image and the frozen game template.
The evaluator ([native/eval-session.mjs](native/eval-session.mjs)) reads the game's memory directly
from `/proc/<pid>/mem`, using the layout in [native/re-layout.json](native/re-layout.json). On the
player's first tool call it enters Arcade through the visible menu, the same for both interfaces,
then records the game window until the result screen settles.

- **MCP:** the delivered `mcp/` runs as `player` (RE) or `agent` (no-RE) and is relayed unchanged.
  There is no limit on what one call may observe or do.
- **GUI:** screenshot, drag and click tools with the GUI player loop ([src/gui-agent.ts](src/gui-agent.ts)).

## Rules and records

A run fails only when the game cannot start or the evaluator cannot read a result. Everything else
is recorded for the owner to judge, never scored automatically: the delivered tool list and calls,
whether the MCP's files mention memory-writing or memory-reading APIs, paused time and restarts,
and the reviewer's observations. The brief asks the Builder to play through the game's own input and
not to change its memory, rules, timing or files.

The `re` variant's brief asks the Builder to reverse engineer the game and play from the state it
recovers, with generic method (RTTI and vtables, the collection the game iterates, live confirmation,
a fitted screen transform); its reviewer flags pixels used where memory is exact. `fn-memd` serves
read-only game memory over `/run/fn-mem.sock` so memory reads work regardless of the host's
`yama.ptrace_scope`.

The `no-re` variant is locked, not only asked. The Builder, its reviewer, trial MCPs and the
delivered MCP at play time run as `agent` (uid 10002), not as the game's `player`: the kernel denies
them the game's memory, maps, ptrace and signals; the game's home, Wine prefix, logs and template
are closed. They work on the `no-re` image, which lacks the builder toolbox, binutils' analysis
tools (`objdump`, `readelf`, `strings`, `nm`, ...) and `winedbg`. What remains is what playing
needs: the X display for capture and input, Python with Pillow and numpy, ffmpeg, xdotool, Node with
the MCP runtime, `/output`, and `fn-app`, which reaches the game's user through a root helper
(`fn-appd`) that accepts only `start`, `stop`, `restart` and `status`.

## Known gaps

- The game has no seed; rounds differ, so a few rounds do not establish a stable ratio.
- The score reader depends on the memory layout documented in [native/RE-NOTES.md](native/RE-NOTES.md).
- Memory writes by an MCP are recorded, not blocked.
- The Builder toolbox has Kuna instead of headless Ghidra; Ghidra 12 needs JDK 21, which Debian
  bookworm lacks.
- Rendering is software Vulkan on the CPU; host load affects frame rate equally in both modes.
