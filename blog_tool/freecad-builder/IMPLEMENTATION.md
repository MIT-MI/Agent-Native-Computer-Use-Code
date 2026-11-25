# FreeCAD Builder: implementation report

The [README](README.md) keeps the hard rules and the commands; this report explains
how the pieces fit, what each condition can and cannot reach, how it is checked,
and what the latest runs showed.

## 1. Question and design

An agent (the Builder) receives FreeCAD 0.20.2 on a desktop, never the tasks, and
builds an MCP server. A second agent (the solver) then completes five FreeCAD tasks
through that MCP. The study compares three groups on the same tasks:

| Group | Builder | Solver |
|---|---|---|
| `re` | Analysis of the application is the expected path | MCP solve as the Builder's identity |
| `no-re` | A separate user, closed off from the application; the window only | MCP solve in the same closed setup |
| GUI | none | The default GUI tools on the shared loop |

Fairness means the same model, task text, deadline and start image in every mode.
Our own agents get no defensive limits for the sake of parity, and the GUI arm is not
strengthened on purpose (`AGENTS.md`).

The two Builder conditions differ by permissions, not only by instructions. Earlier
rounds differed by a toolbox and a sentence. Both Builders ran as FreeCAD's user and
reached FreeCAD's public Python interface the same way, so the scores were the same. The
redesign follows the lessons of the sibling Fruit Ninja project (`docs/RE-LESSONS.md`
there): lock the no-analysis condition with permissions, make analysis the expected path
with method and tools rather than prose, and report evidence rather than gate.

## 2. Architecture

```text
cli.py (tmux) -> harness/driver.py --docker exec--> systemd GNOME desktop container
                     |                                (offline, one per Builder, trial or solve)
                     +-- model endpoint (key stays in the driver)
                     +-- judge container (task runtime image, read-only work folder)
```

- **Driver** (`harness/driver.py`). Runs one run configuration: freeze checks, images, the
  selected checks, Builder, solves, grading and a receipt. `cli.py start` writes the
  configuration and starts the driver in tmux; `--repeat N` runs N independent runs one
  after another in one session.
- **Images** (`harness/images.py`, `harness/container.py`). A Debian 12 GNOME base is
  built once. Then:
  - the software image, with FreeCAD 0.20.2+dfsg1-4+deb12u1 (the same binary as every
    earlier FreeCAD run);
  - the toolbox image, which adds the analysis toolbox and an offline reference layer
    (exact source package and installed documentation);
  - one start image per task: the fixture in `/app/work`, with FreeCAD opening the input
    file.
  `--reuse-images RECEIPT` reuses them by ID. `container.py` mirrors the small part of
  Modal's image and exec API that the cua-world code uses.
- **Desktops** (`harness/desktop.py`). Each is a privileged systemd container with
  `--network=none`, 4 CPUs, 8 GB and a 1280x800 display. It is disposable and committed
  only when an image is made. Commands run through `docker exec`.
- **Guest helpers** (`harness/guest/`):
  - `appctl.py`: the `cua-app start|stop|restart|status` lifecycle;
  - `session.py`, `boot.py`, `ready.py`: the desktop session;
  - `exec.py`: Builder commands with waits and logs;
  - `scan.py`: file-change evidence;
  - `exec_policy.py`: the GUI executable policy;
  - `mcp_boundary.py`: the identity every Builder command and MCP runs as.

## 3. Builder

- **Loop** (`agent/kimi_loop.py`). It is shared by the Builder, reviewer and both
  solvers. Every observation carries the remaining time. Tool output is cut to its head
  and tail (30,000 characters), with the full text on disk. A reply without a tool call
  gets a reminder instead of an ending. Endpoint retries stay within the budget, and
  history is compacted against the endpoint's context window. Model output is capped at
  32,000 tokens per reply.
- **Tools.**
  - `exec`, `read_file`, `write_file`;
  - GUI input: `screenshot`, `click`, `right_click`, `double_click`, `drag`, `type_text`,
    `key`, `scroll`;
  - `try`: snapshot the workspace, deploy `mcp/` exactly as solvers get it, run calls, and
    return results, file changes, stderr and a screenshot;
  - `review`: a fresh reviewer on the same desktop, advice only;
  - `finish`: deliver the workspace if its MCP starts, otherwise the last snapshot that
    started.
- **Budget.** 3 hours, reviews included, with a 30-minute cap per review. The first
  `finish` with more than 15% of the budget left only asks for confirmation.
- **Brief** (`CONTRACT.md`). Shared text plus `<!-- only re -->` and `<!-- only no-re -->`
  blocks. The shared part sets three rules: tool arguments are application data, never
  code; expose application operations rather than screen primitives; write for a solver
  that sees only the tool list and the server's instructions.
- **Route evidence.** `try` and `finish` report which routes the MCP's files use:
  application internals, the window, or editing documents without the application. The
  MCP's own files are listed apart from bundled dependencies. The patterns are in
  `builder.py`, plus FreeCAD's names from `software/freecad.json`. Each variant's reviewer
  is asked about them.
- **Delivery.** `/output/mcp/launch.sh` plus server files and `deps/`. It deploys
  read-only as `/opt/frozen-mcp`, runs offline, and cannot see `/output`.

## 4. The `re` condition

- **Identity.** The Builder's commands, reviewer, trial MCPs and the delivered MCP run
  as FreeCAD's user `ga`, with no added capability.
- **Debug access to FreeCAD only.** `cua-app` starts FreeCAD through
  `harness/guest/debuggable.py`. It marks its own process as open to debuggers of the
  same user (Yama's `PR_SET_PTRACER_ANY`) and then execs FreeCAD unchanged, on every
  start and restart. The host keeps `kernel.yama.ptrace_scope=1`, which is only
  recorded. So `ga` can read FreeCAD's memory, attach gdb or Frida and run code inside
  FreeCAD. It cannot attach to PID 1, the root-owned helpers, or the rest of its own
  session.
- **Brief.** Analysis is the expected way to build the MCP. The application already
  holds, exactly, its documents and objects, its commands and parameters, its settings,
  selection, views and dialogs. Driving the window or editing files without the
  application are fallbacks, to be named in the notes. The method is generic:
  - start from the layer the application uses to carry out a user action;
  - use RTTI and vtables, and Qt meta-objects;
  - trigger an action and observe what runs and changes;
  - confirm on the live process across documents and moments;
  - find the process again after each restart.
  The brief contains no FreeCAD names.
- **Toolbox** (`toolbox.json`, at build time only):
  - Kuna, Ghidra 12 with Temurin 21, Rizin and binutils;
  - Frida 17.4.0, gdb, strace and ltrace;
  - AT-SPI, xdotool and xprop;
  - D-Bus monitor and introspection, mitmproxy and a DevTools client;
  - inotifywait, sqlite3, gsettings and dconf.
- **`apptool`** (`harness/re_tools/apptool.py`). A generic inspector in the spirit of
  Fruit Ninja's memtool. It finds the current process on every call. Commands:
  - `pid`, `modules`;
  - `rtti`: C++ classes and vtables;
  - `qt`: Qt classes with signals, slots, invokable methods and properties;
  - `find`: values, or live objects of a class;
  - `dump`: unfiltered;
  - `watch`: fields changing during a window action;
  - `trace`: calls and files during an action.
- **At solve time.** The toolbox is absent; what the MCP needs, such as Frida's Python
  package, is bundled in `mcp/deps`.

## 5. The `no-re` condition

- **Identity.** The Builder's commands, reviewer, trial MCPs and the delivered MCP run
  as `agent` (uid 10002). `harness/guest/isolate.py` creates this user when the desktop
  starts, so saved images stay unchanged.
- **Closed to `agent`:**
  - every file of FreeCAD's declared packages and its install roots;
  - `/home/ga`, the task work folder `/app/work` and the application logs.
  Because the user differs, the kernel also refuses `agent` FreeCAD's memory, maps,
  debugger attach and signals.
- **Removed.** 31 binary-analysis and debugging tools (`config.json`
  `isolation.analysis_tools`). Setup fails if one remains.
- **Window and app control.** `agent` has no X authority, session bus or accessibility
  bus. It controls FreeCAD with `cua-app`. It reaches the window only through
  `cua-input` (`harness/guest/cua_input.py`), a root helper with the GUI agent's actions.
  The Builder's own GUI tools go through it too.
- **Typed-code filter.** It uses the GUI arm's detector (`harness/code_guard.py`) and
  applies it to the line being entered (up to 512 characters since the last Enter):
  `type_text` plus characters from `key` presses, with shifted keys and key codes
  resolved on the desktop layout. An input is refused if it would make that line, or its
  part since the pointer or cursor last moved, look like code. Lock, latch, dead and
  compose keys are not passed on.
- **Desktop policy for `ga`.** `ga` gets the GUI arm's desktop policy: command-line
  lockdown and the executable policy. No terminal or interpreter can be opened through
  the window. `agent` keeps ordinary programs through ACLs.
- **Private writes.** Everything `agent` writes lives under `/srv/agent`, which `ga`
  cannot enter: `/output`, its home, `/tmp`, `/var/tmp` and `/opt/frozen-mcp`. FreeCAD
  therefore cannot be made to open a file the agent wrote.
- **Brief.** Learn the application from its window, as a user would. Typed code is
  refused, and analysis is not to be attempted, which is redundant but explicit.

## 6. OCR option

`--ocr`, off by default, gives the Builder and the MCP of both conditions the same
offline OCR at build and solve time: Tesseract with English data and `tesserocr`. It is
added once to copies of the software and toolbox images (`run.ocr_images`). The build
fails if this changes FreeCAD's packages or brings back an analysis tool. The brief
mentions OCR only when it is on, and the receipt records `run.ocr`.

## 7. Solving and grading

- **MCP solve.** The solve desktop follows the delivered MCP's condition and OCR. The
  solver uses the shared loop with an MCP-native prompt and the server's instructions.
  `before-solve.png` and `after-solve.png` are saved around it.
- **GUI solve.** By default it uses the shared loop, the MCP solver's prompt (worded for
  GUI tools) and the Builder's GUI tools, with the same turn rule as the MCP solver. It
  runs under the GUI desktop policy, and typed code is refused with the earlier GUI runs'
  message. `--gui-agent opencode` runs the earlier FreeCAD rounds' OpenCode agent with its
  seven screenshot desktop tools instead.
- **Budgets.** Each task keeps its own deadline in both modes: 20, 20, 20, 40 and
  90 minutes for board-plate, cable-slot, service-standoffs, lid-vent-pattern and
  enclosure-release. There is no step limit.
- **Grading** (`harness/scorer.py`). The work folder is archived and judged by the
  tasks' own judge v3 bundles in the task runtime image (`tasks.judge_image`). The judge
  container runs offline and read-only, with no capabilities, as the host user. The
  task files are hashed in `tasks/freeze.json` and checked before every run. The
  untouched start states score 3/9, 2/10, 2/10, 2/13 and 2/21.

## 8. Model-free checks

| Check | What it verifies |
|---|---|
| `toolbox-check` | Every toolbox entry runs offline, including decompiling a stripped binary |
| `compatibility` | Every Builder tool, with a small hand-written MCP, as the `--profile` condition |
| `noop` | The judge's score for each untouched start state |
| `allowlist` | Under the GUI policy FreeCAD starts and restarts; sh, python3, the terminal and the text editor do not |
| `isolation-check` | 103 probes as `agent`, both as a command and inside the MCP runtime, on a task desktop. Expected to fail: FreeCAD's files, home, work folder and logs; FreeCAD's modules and binaries; memory, maps, attach and signals; X directly; the session and accessibility buses; every analysis tool; typed code key by key, by key codes and split across calls. Expected to work: `cua-app`, `cua-input`, `/output`, plain values, and OCR when on |
| `analysis-check` | As the `re` identity: FreeCAD's memory, gdb, Frida and `apptool` work, before and after a restart, as a command and from the MCP runtime. The identity holds no capability and cannot read or attach to any other process |

A run stops only for network access, key exposure, task material reaching the Builder,
an application that cannot start, or an isolation or analysis check that does not hold.
Everything else is recorded.

## 9. Evidence

Each run folder holds:
- `run-config.json`, `receipt.json` and `driver.log`;
- the image package manifests;
- per desktop: exec records, a fanotify audit, binary hashes and the ptrace setting;
- the Builder's conversation, requests, trials, reviews, workspace archives and
  `delivery.json` with route evidence;
- per solve: the conversation, MCP stdio and stderr, screenshots, the score and the
  judge's output.

Model keys never enter desktops, evidence or git. A key found in evidence stops the run.

## 10. Latest results (DeepSeek V4.1 Flash, 2026-11-16/17)

Temperature 1.0 and top_p 0.95. Four groups, three Builders each, every delivery
solving all five tasks once. Points out of 63 per run:

| Group | Run 1 | Run 2 | Run 3 | Builder minutes |
|---|---|---|---|---|
| `re`, no OCR | 63 | 63 | 63 | 37, 30, 85 |
| `no-re`, no OCR | 11 | 11 | 11 | 161, 168, 179 |
| `re`, OCR | 63 | 63 | 63 | 109, 46, 60 |
| `no-re`, OCR | 11 | 11 | 12 | 163, 182, 158 |

- **`re` analysed the application.** Every `re` Builder used the toolbox: Frida in all
  six (17–89 mentions), plus gdb, `apptool`, and in some runs rizin, Kuna, strace and
  ltrace. Every delivered MCP works inside FreeCAD's process through bundled Frida. Each
  task took 16–146 seconds to solve.
- **`no-re` stayed on the window.** Every `no-re` MCP worked only through `cua-input`; no
  file of any MCP touches internals or edits documents directly. They scored the start
  state; the one extra point is board-plate's stock dimensions in one run. Reading
  FreeCAD's model tree from pixels was unreliable, so solvers could not find their
  objects, with or without OCR.
- **Typed-code filter.** It refused three inputs, all in the OCR group. The one with
  surviving text was a probe path, `/app/work/__nope__.FCStd`, caught by the detector's
  dunder rule.

The difference measures access to the application's internals. FreeCAD's internals
include a complete Python interface, so `re` needed little decompilation.

## 11. Known limits and open items

- **Typed-code filter.** It follows input in order. It cannot see text rearranged with
  the cursor, pasted, or completed by FreeCAD; the brief and reviewer discourage these
  routes. The refused texts stay on the desktop; only counts reach the receipt.
- **Start screen.** Desktops start in GNOME's Activities overview, as the owner decided.
  Window-driven approaches must leave it first.
- **Scope of the results.** One model and three runs per group.
- **`apptool`.** `rtti` needs exported vtable symbols, and `qt` decodes Qt 5 only, which
  FreeCAD 0.20 uses.

## 12. Code map

| Path | Role |
|---|---|
| `cli.py` | Start, status, wait and stop runs in tmux |
| `config.json` | Model pins, budgets, isolation, OCR, platform and tasks |
| `CONTRACT.md` | Builder brief with condition blocks |
| `harness/driver.py` | Run orchestration, checks, Builder, solves, receipts |
| `harness/builder.py` | Builder tools, review, finish, route evidence |
| `harness/solve.py`, `opencode_gui.py` | MCP solve, default GUI solve, OpenCode GUI solve |
| `harness/desktop.py`, `container.py`, `images.py` | Desktops and images on Docker |
| `harness/mcp_transport.py` | MCP deployment and stdio transport |
| `harness/scorer.py` | Work-folder archive and judge run |
| `harness/access_check.py`, `guest/access_probe.py` | Isolation and analysis checks |
| `harness/guest/isolate.py`, `cua_input.py`, `debuggable.py` | `no-re` setup, window helper, `re` debug opt-in |
| `harness/re_tools/apptool.py` | Generic application inspector for `re` |
| `agent/` | Shared loop, solver roles, the pinned upstream Kimi agent the loop builds on |
| `software/`, `environments/`, `tasks/`, `toolbox.json` | Data: application, desktop, frozen tasks, toolbox |
