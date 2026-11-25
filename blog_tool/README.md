# blog_tool

Material behind the blog: two Builders in which an agent builds its own MCP server for a program it
has only as installed software or compiled binaries.

| Directory | What it is | Runs on |
| --- | --- | --- |
| [`freecad-builder/`](freecad-builder/README.md) | Task-blind Builder for the Linux desktop application FreeCAD with and without reverse engineering (`re`, `no-re`), plus the MCP-vs-GUI solve and judge; Python | Docker (reef) |
| [`fruit_ninja_builder/`](fruit_ninja_builder/README.md) | Builder for the Windows game Fruit Ninja HD with and without reverse engineering, plus the MCP-vs-GUI evaluation, experiment, analysis and showcase; TypeScript | Docker (reef) |

Each directory is self-contained: its own README, dependencies, tests and `.gitignore`. Both share
the cua-world design (guide rather than restrict, evidence instead of verdicts, a small harness);
[`fruit_ninja_builder/docs/RE-LESSONS.md`](fruit_ninja_builder/docs/RE-LESSONS.md) records what made
reverse engineering work.

## Origin

Where each Builder comes from:

- `freecad-builder`: the code of the published FreeCAD round, from `mcps-reef-rea` branch
  `freecad-showcase` (MCP groups at `0eed63f`, GUI group at `ea503da`).
- `fruit_ninja_builder`: a standalone repository at `ac32906`, extracted from `fruit-ninja-mcp`
  branch `feat/windows-builder` at `bfd4ca4`.

New runs record this repository's commit in their run records (`run.json`, `commit` files,
receipts); the published FreeCAD runs name the `mcps-reef-rea` commits above.
