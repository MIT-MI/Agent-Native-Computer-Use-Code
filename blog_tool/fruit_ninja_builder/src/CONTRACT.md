# Builder brief

<!-- Blocks marked "only re" or "only no-re" reach only the Builder of that variant. -->

Build an MCP server that another agent will later use to play this software, the Windows game
Fruit Ninja HD, as well as it can: in real time, for as high a score as it can reach. You receive
the game, not the player's instructions or how its play will be judged. Do not seek evaluation
code, verifiers or past results.

The game runs under Wine on the X11 display `:7`. `fn-app start|stop|restart|status` controls it;
`/opt/fn/app.json` records the display, Wine prefix, game directory and environment.
The game runs in real time: nothing pauses it while anyone thinks or calls tools. When the player's
first tool call reaches your server, the game may be at a menu or already in a round. A running round
is the round to play: getting into play must never leave, restart or pause it.

<!-- only re -->

Reverse engineer the game and build the MCP on what you recover; this is the expected way to build it.
The game already holds, exactly, what its screen shows late and noisily: which objects are in flight
and what each one is (fruit, bomb and every special kind), where they are and how they move, the
score, the clock and which screen is up. Recover these from the executable and the live process, read
them from the game's memory at play time, and act through the game's own input. Use the screen only
for what you could not recover, and say so in your notes. Your analysis and your deployed MCP run as
the game's user: they may execute code, read the game's memory, use Wine and send X11 input. The
host's ptrace policy may refuse reading another process's memory directly; `/run/fn-mem.sock` then
serves read-only reads of the game's current process, to you and to your MCP at play time (requests,
one per line: `pid`, `maps`, `read ADDR LEN`, answered with a JSON line, a JSON line, and a 4-byte
little-endian count followed by the bytes). `memtool` uses it when it must.

<!-- only no-re -->

You and your deployed MCP run as a user other than the game's: the game's files, Wine prefix and
memory are closed to you, and binary-analysis tools are absent. Control the game with `fn-app`. Do not
reverse engineer the game in any other way: do not disassemble, decompile, debug, trace or hook it,
and do not inspect its files or memory. Learn the game from its window, as a player would: what it
draws and how it responds to input. You may execute code, capture the game's window and send X11
input.

<!-- end -->

There is no limit on how much a single tool call may observe or do. Tool arguments describe game
data and operations, never scripts, expressions, command lines or code. Play through the game's own
input; do not change its memory, rules, timing or files to alter an outcome. Pausing the game to win
thinking time changes its timing. Expose game operations rather than generic screen primitives such
as click, type or screenshot.

The player sees your tool list (names, descriptions and argument schemas) and your server's
initialize instructions, never your notes. Make the interface understandable without that context:
explain coordinates, units, timing and what each tool returns, and describe errors and limitations
honestly. The player sees at most {tool_output_characters} characters of each tool result, keeping
its head and tail.

The player is a language model that needs about a second or more per reply while the game keeps
moving. Whatever it decides between two of your calls, it decides late, from a stale view. Choose
which decisions deserve the player's judgement and which your server should make itself, on fresh
observations, within one call: a single call may observe, choose and act many times. Design the tools
so that the player's latency does not limit how well the game is played.

<!-- only re -->

Check local evidence before guessing. The game directory and Wine prefix hold the application's own
files. Decompile the executable early and read the code that spawns, moves, slices, scores and draws
objects. Class names in RTTI lead to vtables, and a live object of a class starts with its vtable
pointer; managers keep lists or pools of objects, so find the collection the game itself iterates
rather than stale pool slots. Confirm every field in the live process: watch it change while the
screen changes, across several objects and moments, during play rather than after a round has ended.
Process ids and heap addresses change across restarts; addresses inside the executable's image do
not. The game keeps its own units, origin and axis
directions, so its coordinates may be small or negative where the screen's are not: relate memory to
the screen by fitting a transform from several observations, never by searching or filtering memory
for screen pixel values. Find beacons in familiar formats, libraries and algorithms,
and reuse them when they explain observed behaviour. Choose a concrete state to reach: a menu, a
round, a score.

<!-- only no-re -->

Check evidence before guessing. Run the game, watch it and measure what it shows: colours, shapes,
positions over time, and how quickly it responds to input. Choose a concrete state to reach: a menu,
a round, a score.

<!-- end -->

Write a tool to test a hypothesis with `try`, then explore again. These are thinking aids, not
required stages. Check behaviour across game restarts.

Judge the MCP by how well it plays, not by whether it runs. Play whole rounds through it with `try`,
calling its tools as the player would, and measure the score, how many targets each action reaches,
which special objects appear and what they do, and what earns and costs points. Improve the weakest
part and measure again. Use the whole budget: time left at
`finish` is time not spent improving play.

Use `exec`, `read_file`, `write_file`, GUI input, `try`, `review` and `finish`. Start a minimal MCP
early with `try` so there is always something to deliver, then extend it. `exec` and `try` require
your `wait_seconds`; a command still running at that wait keeps running, with its log on disk.
`try` snapshots `/output`, deploys only `/output/mcp` exactly as the player will get it, runs your
calls, and returns their results, files changed under the home directory and `/tmp` (after the calls
and after an optional game restart), startup errors, the MCP's stderr tail and a screenshot. It is
evidence for your hypothesis, not a verdict; it never blocks work or repairs your build. While a
trial MCP runs, `/output` is hidden from every process. Observations show your remaining time.
Keep notes short and full output on disk, in any format; the harness does not parse them. Leave
enough context for a fresh conversation to continue your reasoning.

<!-- only re -->

Available through `exec` while you build, absent at play time:

- Kuna: native decompilation (`kuna decompile-project`, `kuna decompile`, `kuna xrefs`, `kuna strings`).
- `memtool`: read-only inspection of the running game. It finds the game's current process itself.
  `memtool rtti` lists every class with RTTI and its vtables; `memtool find` and `memtool dump` show
  memory as hex, integers and floats, unfiltered; `memtool watch` samples objects during play and
  reports which fields change and whether they move at constant velocity or acceleration. Its source
  is `/opt/re-tools/memtool`; copy what you need into your MCP for play time.
- rizin and binutils, including `i686-w64-mingw32-objdump`: binary inspection.
- gdb, strace, ltrace and Frida: dynamic analysis.
- inotifywait and sqlite3.

Available both while you build and at play time: Node.js 22 with `@modelcontextprotocol/sdk` and
`zod` importable from any directory; Python 3 with Pillow and numpy; Wine, winedbg and a mingw-w64
i686 compiler; xdotool and xprop.

<!-- only no-re -->

Available both while you build and at play time: Node.js 22 with `@modelcontextprotocol/sdk` and
`zod` importable from any directory; Python 3 with Pillow and numpy; ffmpeg; xdotool and xprop.

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

Only `/output/mcp/` deploys, read-only, as `/opt/frozen-mcp/`, started with
`/opt/frozen-mcp/launch.sh` as the user your commands run as, with `HOME`, `DISPLAY` and `PATH` set
(and `WINEPREFIX` for the game's user).
`/output` is not visible to it. Make paths relative to the launcher. Speak MCP JSON-RPC on
stdin/stdout and send diagnostics to stderr. Run offline, without installation.

Call `review` whenever independent observations would help. A fresh reviewer explores this same
desktop and your last started MCP while you wait, and phrases gaps as next goals. Review consumes
your remaining time; the choice is yours. `finish` delivers your current workspace if its MCP starts,
otherwise the last snapshot that started; budget expiry delivers the last snapshot that started.
With much of your budget left, the first finish only asks you to confirm. If finish finds nothing
deliverable, it reports why and you can continue; a second finish ends the run.
