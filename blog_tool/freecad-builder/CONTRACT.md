# Builder brief

<!-- Blocks marked "only re" or "only no-re" reach only the Builder of that variant. -->

Build an MCP that another agent will later use offline for unknown tasks in this
software. Aim to cover what a user can do: work with objects, change settings,
open and save files, and obtain computed outputs. You receive the application,
not its future tasks. Do not seek task instructions, verifiers or past solve data.
The MCP's tool arguments describe application data and operations, never scripts,
expressions, command lines or code. Input a user could enter in the application,
such as a cell formula, counts as data.
Expose application operations, not generic screen primitives such as click, type or screenshot.

<!-- only re -->

Analyse the application and build the MCP on what you recover; this is the expected way to
build it. The running application already holds, exactly, what its window shows only in part:
its open documents and their objects with every property, the commands a user can trigger and
their parameters, its settings, the current selection, its views and its dialogs. Recover how
the application itself represents and changes these, and have the MCP read that state and
invoke those operations in the running application. Driving the window (clicks, keys, typed
text, screenshots) or editing document files directly without the application is a fallback:
use it only for what you could not recover, and say so in your notes.
Your analysis and your deployed MCP run as the application's user, with no further privileges.
The application lets its own user attach to its process, and to no other: they may execute
code, read the application's files, read the memory of the application's own process, attach
a debugger or an instrumentation tool to it, run code inside it, and use the desktop's X11
and accessibility interfaces.

<!-- only no-re -->

You and your deployed MCP run as a user other than the application's. The application's files,
settings, documents, logs and process are closed to you, and binary-analysis tools are absent.
Do not analyse the application in any other way: do not disassemble, decompile, debug, trace
or hook it, and do not inspect its files or memory. Learn the application from its window, as
a user would: what it shows and how it responds to input. `cua-input` is your access to the
window: `screenshot`, `click`, `right_click`, `double_click`, `drag`, `type_text`, `key` and
`scroll` (`cua-input --help`; a program may also use its socket). Text that looks like a
script or code is refused and not typed, whether it arrives at once, in parts or key by key:
operate the application through its own menus, controls and dialogs, not through a console
or macro editor. You have no X11, session-bus or
accessibility access of your own, and the application cannot read your files. You may execute
code of your own.

<!-- end -->

The solver sees your tool list (names, descriptions and argument schemas) and
your MCP server's initialize instructions, never the Builder's notes. Make the interface understandable
without that context. Explain units, accepted formats, save and restart behavior,
and what each tool returns. Describe errors and limitations honestly. Prefer
operations that let the solver inspect state, make a change and understand its
result, using unfamiliar data as well as the examples you tried.
Solvers see at most {tool_output_characters} characters of each tool result, keeping its head and tail; full output is retained in evidence.

<!-- only re -->

Check local evidence before guessing. Start from the layer the application itself uses to
carry out a user's action, not from its window: a command or action registry, an embedded
scripting or automation engine, plugin or extension interfaces, IPC such as D-Bus, and the
code that reads and writes its files. Names lead to that layer. C++ run-time type information
names classes and leads to their vtables, and a live object of a class starts with its vtable
pointer; Qt meta-objects name the signals, slots and properties of the objects behind menus,
actions and dialogs. Trigger one action in the window and observe what runs and what changes:
the functions called, the files and sockets used, the fields that change. Confirm every
finding on the running application, across several documents and moments, never on one
example. Process ids and heap addresses change across restarts: find the process again each
time. Offline source and installed documentation are under `/opt/cua-world-reference/`;
`index.json` records what is available. Find beacons in familiar formats, libraries and
algorithms, and reuse them when they explain observed behavior. Choose a concrete state to
reach: a setting, an object or a computed output.

<!-- only no-re -->

Check evidence before guessing. Run the application, watch it and measure what it shows: its
menus, dialogs, panels and status texts, where its controls are, which keys it answers to and
how the window changes after each input. Choose a concrete state to reach: a setting, an
object or a computed output.

<!-- end -->

Write a tool or interaction to test a hypothesis with `try`, then explore again.
These are thinking aids, not required stages. Compare computed outputs with the
application's own results; explain time-dependent fields and tolerances.
Check changes by saving, restarting and reopening.
Judge the MCP by what a fresh agent achieves with it on an unfamiliar task, not by whether it
runs, and use the whole budget: time left at `finish` is time not spent improving it.

Use `exec`, `read_file`, `write_file`, GUI input, `try`, `review` and `finish`.
Start a minimal MCP early with `try` so there is always something to deliver, then extend it.
`try` snapshots your workspace and runs its deployed MCP and your calls.
Both `exec` and `try` require your `wait_seconds`; an unanswered trial stops its MCP.
You choose whether `try` restarts the application. Observations show remaining time and what would be delivered at expiry.
It returns changed paths after calls and, if chosen, after restart, plus a screenshot;
full home-directory and `/tmp` diffs stay in its report. It also returns
startup and call errors, and which of the MCP's files, and separately which of its bundled
dependencies, mention the application's internals, its window or direct document editing;
schema evidence is refreshed only when the tool list changes.
This is evidence for your hypothesis, not a verdict. It never blocks further
work or repairs your build. Aim to leave the application running with changes
visible. Keep notes short and full output on disk. Use any format for goals,
hypotheses and progress; the harness does not parse it. Leave enough context
for a fresh conversation to continue your reasoning.

<!-- only re -->

Available through `exec` while you build, absent at solve time:

- `apptool`: read-only inspection of the running application. It finds the application's
  current process itself. `apptool modules` lists what is loaded. `apptool rtti` lists C++
  classes with their vtables, and `apptool find --class` their live objects. `apptool qt`
  lists Qt classes with their signals, slots, methods and properties. `apptool find` and
  `apptool dump` show memory unfiltered, as integers, floats and what each word points to.
  `apptool watch` samples objects while you act in the window and reports the fields that
  change. `apptool trace` records system calls, files and sockets, or with `--calls` library
  functions, while you act in the window. Its source is `/opt/re-tools/apptool`; copy what
  you need into your MCP for solve time.
- Kuna: native binary decompilation.
- Headless Ghidra: static analysis and decompilation.
- Rizin: binary inspection and analysis.
- Binutils: symbols, sections, disassembly and strings.
- Frida: dynamic instrumentation.
- gdb: native debugging.
- strace: system calls.
- ltrace: library calls.
- `atspi-tree`: accessibility structure.
- inotifywait: filesystem changes.
- sqlite3: SQLite inspection.
- curl: local HTTP services.
- mitmproxy: local HTTP inspection.
- `cdp`: local browser inspection through the DevTools protocol.

Available both while you build and at solve time: Python 3 with numpy and Pillow; xdotool,
xprop and wmctrl; gsettings, dconf and the D-Bus command-line tools; and `cua-app`. The
application's own process stays open to attach at solve time, also after a restart.
Anything else your MCP needs at solve time, such as an instrumentation library, must be
bundled in `mcp/deps`.

<!-- only no-re -->

Available both while you build and at solve time: Python 3 with numpy and Pillow, `cua-input`
and `cua-app`. Anything else your MCP needs at solve time must be bundled in `mcp/deps`.

<!-- only ocr -->

Offline OCR is also available while you build and at solve time: Tesseract with English data
(`tesseract`) and its Python binding (`tesserocr`), for reading text in screenshots.

<!-- end -->

Deliver this layout:

```text
/output/
  intermediate/       # your notes and experiments, in any format
  mcp/
    launch.sh         # executable MCP launcher
    <server files>
    deps/             # any bundled runtime dependencies
```

Only `/output/mcp/` deploys, as `/opt/frozen-mcp/`. The deployed launch command is
`/opt/frozen-mcp/launch.sh` for trials and solves, run as the user your commands run as.
`/output` is hidden from the MCP, including during `try`.
Make paths relative to the launcher, not to `/output`. Speak MCP JSON-RPC on
stdin/stdout and send diagnostics to stderr. Run offline
without installation, keys, or access to `intermediate/`.

The runtime provides `cua-app start`, `stop`, `restart` and `status` for the
applications declared in software data. It preserves existing window frames
across restarts, without accepting pointer, key or text input. `cua-app request`
can send HTTP data to declared local service origins. Use `cua-app --help` for
its data arguments.
<!-- only re -->
The MCP can reach the desktop's X11 and accessibility interfaces;
its network is unshared and normal filesystem writes remain available.
<!-- only no-re -->
The MCP reaches the window through `cua-input` only. Its network is unshared, and it can
write in its home directory and `/tmp`.
<!-- end -->

Call `review` whenever independent observations would help. A fresh reviewer
uses this same desktop while you wait and phrases gaps as next goals. Review
consumes your remaining time. An incomplete review returns the reviewer’s messages
and transcript path; full tool output stays on disk. Consider reviewing before you finish; the choice
is yours. Finish delivers your latest work if it starts; budget expiry delivers
the last snapshot that started. With much of your budget left, the first finish only
asks you to confirm.
If finish finds nothing deliverable, it reports why and lets you continue; a second
finish ends the run anyway, preserving the workspace and startup errors.
