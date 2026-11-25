// The two Builder briefs: shared text, plus blocks only one variant receives.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { briefFor } from "../src/builder.js";

const contract = await readFile("src/CONTRACT.md", "utf8");

test("both briefs keep the shared text and no markers", () => {
  for (const variant of ["re", "no-re"] as const) {
    const brief = briefFor(contract, variant);
    assert.doesNotMatch(brief, /<!--|-->/);
    assert.doesNotMatch(brief, /\n{3}/);
    for (const shared of [
      "There is no limit on how much a single tool call may observe or do.",
      "{tool_output_characters}",
      "Write a tool to test a hypothesis with `try`",
      "/opt/frozen-mcp/launch.sh",
      "a second finish ends the run.",
      "A running round\nis the round to play",
      "a single call may observe, choose and act many times",
      "Judge the MCP by how well it plays",
    ])
      assert.ok(brief.includes(shared), `${variant}: ${shared}`);
  }
});

test("only the no-RE brief forbids reverse engineering, and it lists no RE tools", () => {
  const re = briefFor(contract, "re");
  const noRe = briefFor(contract, "no-re");
  assert.match(re, /read the game's memory/);
  assert.match(re, /Kuna/);
  assert.doesNotMatch(re, /Do not reverse engineer/);
  assert.match(noRe, /Do not\s+reverse engineer the game in any other way/);
  assert.match(noRe, /memory are closed to you/);
  assert.match(
    re,
    /Reverse engineer the game and build the MCP on what you recover/,
  );
  assert.match(
    re,
    /never by searching or filtering memory\s+for screen pixel values/,
  );
  assert.doesNotMatch(noRe, /Reverse engineer the game and build/);
  for (const word of [
    "Kuna",
    "rizin",
    "gdb",
    "Frida",
    "winedbg",
    "decompiled",
    "beacons",
  ])
    assert.doesNotMatch(noRe, new RegExp(word), word);
});
