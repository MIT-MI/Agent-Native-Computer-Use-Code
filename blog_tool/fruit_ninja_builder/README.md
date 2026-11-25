# Fruit Ninja Builder: GUI vs. agent-built MCP

Two ways for a model to play the original Windows **Fruit Ninja HD** in real time, under Wine:

- **GUI:** screenshots and timed mouse drags.
- **Agent-built MCP:** a Builder agent explores the game offline and writes an MCP server for it;
  a player model then plays through that server.

Both play the same game files from the same start state with the same player model, and an evaluator
reads the score from game memory. The Builder follows a `cua-world` style design: guidance instead
of restriction, evidence instead of verdicts, and a small harness. The MCP is not weakened for
parity, and the GUI keeps its screenshot-and-drag tools, prompt and loop.

```text
 Builder (model) ──tools──► builder desktop (offline): game + Kuna, rizin, gdb, strace, Frida, memtool
                  exec · files · GUI input · try · review · finish ──► workspace snapshot
 MCP player ──stdio──► solve desktop (offline): game + frozen MCP (as player for RE, agent for no-RE)
 GUI player ──stdio──► solve desktop: screenshot and drag tools
                        evaluator (root): score from /proc, recording, Arcade start
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for roles, rules and known gaps,
[src/CONTRACT.md](src/CONTRACT.md) for the Builder brief, and
[docs/RE-LESSONS.md](docs/RE-LESSONS.md) for how the RE and no-RE conditions were engineered.

## Setup and checks

The host needs Docker and Node.js 22. The game is downloaded and verified by hash; it is never
committed (see [NOTICE.md](NOTICE.md)).

```sh
make images   # runtime (solve), builder (RE), and no-re images
make setup    # npm ci, pinned game, DXVK, Wine prefix template
make check    # typecheck and formatting
make test     # unit tests
make lockcheck # verify no-RE lockdown as agent
make smoke    # model-free: scripted Builders (RE and no-RE), two MCP rounds, one GUI round
```

## Runs and experiments

The Builder, its reviewer and both players (MCP and GUI) default to
[config/kimi-k3.json](config/kimi-k3.json); the roles are set separately in
[src/config.json](src/config.json). `max_tokens` in the model config applies only to the GUI
player's replies. API keys are read from the environment variables named in the model configs and
never enter a container.

```sh
node --import tsx src/cli.ts build --out RUN/build                      # 3-hour RE Builder
node --import tsx src/cli.ts build --variant no-re --out RUN/build-no-re # same, locked without RE
node --import tsx src/cli.ts solve --mode mcp --mcp RUN/build/delivery.json --out RUN/round-1/mcp
node --import tsx src/cli.ts solve --mode gui --out RUN/round-1/gui
```

Run a full comparison experiment (RE and no-RE builds in parallel, followed by 3 groups × 3 rounds
in rotating order) and regenerate the summary tables:

```sh
make experiment EXPERIMENT_OUT=.local/runs/exp-01
python3 scripts/analyze.py .local/runs/exp-01
```

`--variant no-re` measures what reverse engineering adds. The Builder, its reviewer and its MCP run
as `agent` (uid 10002) on an image without analysis tools, so the game's memory and files are closed
to them; they can still control the game through `fn-app` (via `fn-appd`), see its window, send
input, `try` and `review`. The default `re` variant provides the analysis toolbox, `memtool`, and
read-only game memory via `fn-memd`.

`--smoke` uses the shorter budgets in [src/config.json](src/config.json), `--seconds N` overrides
the budget and `--mock` replaces the model with a scripted one. A build directory holds the
conversation, every tool output, `try` and `review` reports, workspace snapshots and
`delivery.json`. A round directory holds `result.json`, `video.mp4`, `result.png`, the player's
conversation and the evaluator's event log (`viewer-events.jsonl`).

## Code layout

| Path               | Responsibility                                                                |
| ------------------ | ----------------------------------------------------------------------------- |
| `config/`          | Model endpoints (`kimi-k3.json`, `deepseek-v4.1-flash.json`) and `game.json`  |
| `src/builder.ts`   | Builder tools: exec, files, GUI input, try, review, finish                    |
| `src/loop.ts`      | Agent loop shared by Builder, reviewer and MCP player                         |
| `src/solve.ts`     | One round: desktop, delivered MCP or GUI, player loop, result                 |
| `src/desktop.ts`   | Docker desktops and the deployment path shared by try, review, finish, solve  |
| `src/gui-agent.ts` | The GUI player's loop                                                         |
| `native/`          | Evaluator only: memory reader, Arcade start, GUI tools, recording, game setup |
| `sandbox/`         | Desktop, builder and no-re images; guest helpers and `memtool`                |
| `scripts/`         | `experiment.sh` (full 3-group runner) and `analyze.py` (table generator)      |
| `showcase/`        | Remotion comparison video source, render Dockerfile, and data prep script     |
| `tests/`           | Unit tests                                                                    |
