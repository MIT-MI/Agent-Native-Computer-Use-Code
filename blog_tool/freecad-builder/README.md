# FreeCAD Builder

The cua-world Builder, ported to this Docker host for FreeCAD. A task-blind
Builder gets FreeCAD on a desktop and delivers an MCP, either with analysis of the
application (`re`) or without it (`no-re`); see [Conditions](#conditions). The agent loop,
Builder tools (`exec`, `read_file`, `write_file`, GUI input, `try`, `review`,
`finish`), MCP transport, guest helpers, [Builder brief](CONTRACT.md), MCP solver
and the GUI executable policy are cua-world's. GUI solve uses the default GUI
tools: the Builder's GUI input (`screenshot`, clicks, `drag`, `type_text`, `key`,
`scroll`) on the same loop and prompt as the MCP solver, with no model-specific
harness. `--gui-agent opencode` runs an OpenCode agent with seven screenshot desktop tools
instead, as in the published round.

```text
cli.py (tmux) -> harness/driver.py -> systemd GNOME desktop container via docker exec
```

[IMPLEMENTATION.md](IMPLEMENTATION.md) explains how the pieces fit, what each condition can
reach, how that is checked, and the latest results.

## Hard rules

- Desktops are offline (`--network=none`). Model keys stay in the driver.
- Builder and reviewer are task-blind: no tasks, verifiers or past solve evidence.
- Deployed tool arguments are data, never code, scripts or arbitrary commands.
  Builder analysis may execute code.
- GUI solve runs only the task's application: no terminals, editors, interpreters
  or command entry outside it. Text that looks like code (`harness/code_guard.py`,
  the detector of the earlier FreeCAD GUI runs) is not typed and is recorded in
  the solve receipt; the GUI agent itself is unchanged.
- Both modes get the same model, task text, deadline and start image.
- Checks record and explain observations. They stop a run only for network
  access, key exposure, task material reaching the Builder, or an application
  that cannot start. Our agents get no defensive limits for the sake of parity.

## Experiment

| Group | Builder | Then |
|---|---|---|
| 1 | `re`: analysis is the expected path; toolbox, `apptool`, exact source package, FreeCAD's own process open to attach | MCP solve as the same identity |
| 2 | `no-re`: a separate user, closed off from the application; the window through `cua-input` only | MCP solve in the same closed setup |
| 3 | none | GUI solve with the default GUI tools |

As in the earlier FreeCAD runs: five tasks from `tasks/` (fixtures, briefs and
judge v3, hashed in `tasks/freeze.json`), each
with its own deadline in both modes and no step limit; three Builders per
Builder group, every delivery solving every task once; one GUI solve per task.
Each task's start image is the software image plus its fixture, with FreeCAD
opening the input file.

## Conditions

The two Builder groups differ by permissions, not only by instructions. `--profile`
picks one. The Builder's
commands and file tools, its reviewer, trial MCPs (`try`) and the delivered MCP at solve
time all run as the same identity (`harness/guest/mcp_boundary.py`).

**`re`** runs as the desktop user `ga` with no added capability. Instead FreeCAD opts in to
being debugged by its own user: `cua-app` starts it through `harness/guest/debuggable.py`,
which sets the Yama ptracer exception for its own process (`PR_SET_PTRACER_ANY`) and then
execs FreeCAD unchanged, on every start and restart. The host's `kernel.yama.ptrace_scope`
stays 1 and is only recorded. So `ga` can read FreeCAD's memory, attach gdb or Frida and run
code in that process, and nothing else: not the desktop's root-owned helpers, PID 1 or the
rest of its own session. The Builder also gets `apptool`
(`harness/re_tools/apptool.py`), a generic inspector that makes the method the default: it
finds the current process itself, lists C++ classes with their vtables and live objects, Qt
classes with signals, slots and properties, shows memory unfiltered, and records what runs
and what changes while an action happens in the window. The toolbox and `apptool` are absent
at solve time; FreeCAD's opt-in is not.

**`no-re`** runs as `agent` (uid 10002), set up when the desktop starts
(`harness/guest/isolate.py`), so saved images stay unchanged:

- FreeCAD keeps running as `ga`. The kernel refuses `agent` its memory, maps, debugger
  attach and signals.
- Closed to `agent`: the files of FreeCAD's declared packages and its install roots,
  `/home/ga`, the task work folder `/app/work` and the application logs.
- Removed: binary-analysis and debugging tools (`config.json` `isolation.analysis_tools`);
  setup fails if one remains.
- `agent` has no X authority, session bus or accessibility bus. `cua-app` controls the
  application; `cua-input` (`harness/guest/cua_input.py`, a root helper) offers the GUI
  agent's tools. The Builder's own GUI tools go through it too, so one filter sees every input.
- The typed-code filter is the GUI arm's detector (`harness/code_guard.py`), applied to what is
  being entered instead of to one call: text from `type_text` and characters from `key`
  presses (shifted keys and key codes resolved on the desktop's keyboard layout) since the
  last Enter, up to 512 characters. An input is refused if it looks like code by itself, or
  would make that line, or its part since the pointer or cursor last moved, look like code.
  Lock, latch, dead and compose keys are not passed on. The filter follows what is typed in
  order; it cannot see text rearranged with the cursor, pasted or completed by the application.
- `ga` gets the GUI arm's desktop policy (command-line lockdown and the executable policy),
  so no terminal or interpreter can be opened through the window. The `agent` group keeps
  ordinary programs through ACLs.
- Everything `agent` writes (`/output`, its home, `/tmp`, `/opt/frozen-mcp`) lives under
  `/srv/agent`, which `ga` cannot enter: FreeCAD cannot be made to open a script the agent
  wrote. This closes the one path typed-code filtering leaves to an agent that can write files.

`try` and `finish` report which routes the MCP uses (application internals, the window,
direct document editing) as evidence, listing its own files apart from the dependencies it
bundles under `mcp/deps`, and the reviewer of each variant is asked about them. In both variants the first `finish` with more than 15% of the budget left only
asks for confirmation.

Two model-free checks run before experiments and fail the run on any unexpected result:
`isolation-check` tries every path to the application as `agent` on a task desktop, as a
Builder command and inside the MCP runtime, including code entered key by key, by key codes
and split across calls. `analysis-check` reads FreeCAD's memory, attaches gdb and Frida and
runs `apptool` as the `re` identity, on the Builder desktop and from the MCP runtime of a
task desktop, before and after a restart; it also requires that the identity holds no
capability and cannot attach to or read any other process. The design follows the lessons of the sibling
Fruit Ninja project (its `docs/RE-LESSONS.md`).

`--ocr` (off by default) gives the Builder and the MCP of both variants the same offline OCR
at build and at solve time: Tesseract with English data and its Python binding `tesserocr` (`config.json`
`ocr`), which `no-re` uses on screenshots taken through `cua-input`. The brief mentions it
only when it is on, and the receipt records the switch (`run.ocr`). The packages are added
once to copies of the software and toolbox images (`run.ocr_images`, reused through
`--reuse-images`); the build fails if that changes FreeCAD's packages or brings an analysis
tool. The GUI arm never uses those images. With `--artifact`, a solve uses OCR exactly if
that MCP's Builder had it. Both model-free checks also run with `--ocr`, and then require
that the identity can read the window with OCR.

Each MCP solve saves `before-solve.png` and `after-solve.png` in its folder: the desktop just
before the solver starts and just after it ends, before grading.

## Differences from cua-world

| cua-world | Here | Why |
|---|---|---|
| Modal sandboxes and snapshots | Privileged systemd containers, `docker commit` | Same host as the FreeCAD tasks; no KVM needed |
| Ubuntu 22.04 desktop | Debian 12 GNOME (`config.json`) | Same FreeCAD 0.20.2+dfsg1-4+deb12u1 binary as earlier runs |
| Writes `kernel.yama.ptrace_scope` | Records it | Yama is not namespaced; a container would change the host |
| OpenJDK 21 from apt | Pinned Temurin 21 archive | Debian 12 packages Java 17; Ghidra 12 needs 21 |
| Gym-Anything exporter and verifier | The tasks' judge v3 on the saved work folder | These are the FreeCAD tasks' own verifiers |
| 6 h / 500-turn solve budget | Each task's deadline, no step limit | The FreeCAD tasks' existing settings |
| GUI may type any text | Typed code is dropped | As in the earlier FreeCAD GUI runs |

`harness/container.py` supplies the Docker equivalents of the Modal image and
exec calls, so the Builder, transport and guest code need no changes.

## Run

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-test.txt
.venv/bin/python cli.py start --evidence ~/workspace/runs/freecad-builder --checks toolbox-check compatibility noop allowlist
.venv/bin/python cli.py start --evidence ~/workspace/runs/freecad-builder --reuse-images RECEIPT --checks isolation-check analysis-check
.venv/bin/python cli.py start --evidence ~/workspace/runs/freecad-builder --reuse-images RECEIPT --profile re --checks build solve-mcp --repeat 3 --id group1
.venv/bin/python cli.py start --evidence ~/workspace/runs/freecad-builder --reuse-images RECEIPT --profile no-re --checks build solve-mcp --repeat 3 --id group2
.venv/bin/python cli.py start --evidence ~/workspace/runs/freecad-builder --reuse-images RECEIPT --profile no-re --ocr --checks build solve-mcp --repeat 3 --id group2-ocr
.venv/bin/python cli.py start --evidence ~/workspace/runs/freecad-builder --reuse-images RECEIPT --checks solve-gui
.venv/bin/python cli.py wait RUN_ID
```

`compatibility` exercises every Builder tool with a tiny hand-written MCP, as the
`--profile` variant, and `noop` scores each untouched start state; neither calls a model. Model runs read
the key file named in `config.json`. `--reuse-images RECEIPT` reuses built images
by ID; `--artifact RECEIPT` solves with another run's delivered MCP, on the desktop of
that MCP's variant; `--repeat N`
runs N independent runs one after another in one tmux session; `--model NAME` uses a
pinned model from `config.json` `models` (keys in `keys`, read by the driver only);
`--gui-agent` picks the GUI solver.

## Reproduce the published round

The published FreeCAD round (DeepSeek V4.1 Flash, three runs per group) used these commands;
`EV` is any evidence folder. The first builds every image and runs the model-free checks; the
others reuse its images. Scores vary between runs with the model's sampling.

```sh
.venv/bin/python cli.py start --evidence EV --ocr --checks isolation-check analysis-check noop --id prep
R=EV/prep/receipt.json; M=deepseek-v4.1-flash
.venv/bin/python cli.py start --evidence EV --reuse-images $R --model $M --profile re --checks build solve-mcp --repeat 3 --id c1-re
.venv/bin/python cli.py start --evidence EV --reuse-images $R --model $M --profile no-re --checks build solve-mcp --repeat 3 --id c1-no-re
.venv/bin/python cli.py start --evidence EV --reuse-images $R --model $M --profile re --ocr --checks build solve-mcp --repeat 3 --id c2-re
.venv/bin/python cli.py start --evidence EV --reuse-images $R --model $M --profile no-re --ocr --checks build solve-mcp --repeat 3 --id c2-no-re
.venv/bin/python cli.py start --evidence EV --reuse-images $R --model $M --checks solve-gui --gui-agent opencode --repeat 3 --id oc-gui
```
