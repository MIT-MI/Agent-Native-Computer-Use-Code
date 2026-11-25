// Guest helpers that return Builder evidence: output heads and tails, and text ranges.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

const files = (...args: string[]) =>
  JSON.parse(
    execFileSync("python3", ["sandbox/windows/files", ...args]).toString(),
  );

test("peek keeps a long log's head and tail and names the full file", async () => {
  const path = join(await mkdtemp(join(tmpdir(), "fnb-guest-")), "exec.log");
  await writeFile(path, "a".repeat(500) + "b".repeat(5000) + "c".repeat(500));
  const result = files("peek", path, "1000");
  assert.equal(result.bytes, 6000);
  assert.ok(
    result.text.startsWith("a".repeat(500)) &&
      result.text.endsWith("c".repeat(500)),
  );
  assert.match(
    result.text,
    new RegExp(`5000 bytes omitted; full output in ${path}`),
  );
  assert.equal(files("peek", path, "100000").text.length, 6000);
});

test("read returns a text range with the next offset and end of file", async () => {
  const path = join(await mkdtemp(join(tmpdir(), "fnb-guest-")), "notes.txt");
  await writeFile(path, "0123456789");
  assert.deepEqual(files("read", path, "3", "4"), {
    path,
    offset: 3,
    text: "3456",
    next_offset: 7,
    eof: false,
  });
  assert.equal(files("read", path, "7", "100").eof, true);
  assert.match(
    files("read", join(path, "missing"), "0", "1").error,
    /No such file|Not a directory/,
  );
});
