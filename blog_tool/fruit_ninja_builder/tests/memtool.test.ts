// memtool (RE Builder toolbox): RTTI names, motion classification and a live watch of a child process.
import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { test } from "node:test";

const env = { ...process.env, FN_APP_CONFIG: "sandbox/windows/app.json" };
const hasNumpy = (() => {
  try {
    execFileSync("python3", ["-c", "import numpy"], { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
})();
const python = (code: string) =>
  execFileSync(
    "python3",
    [
      "-c",
      `import importlib.machinery, importlib.util, json
loader = importlib.machinery.SourceFileLoader("memtool", "sandbox/re-tools/memtool")
spec = importlib.util.spec_from_loader("memtool", loader); m = importlib.util.module_from_spec(spec); loader.exec_module(m)
import numpy as np
${code}`,
    ],
    { env },
  ).toString();

test(
  "RTTI names demangle; motion is classified on the longest run",
  { skip: !hasNumpy },
  () => {
    const out = JSON.parse(
      python(`t = np.linspace(0, 2, 41)
quad = 100 - 360 * t * t
reset = np.where(t < 0.5, 5.0, 40 + 12 * t)
waits = np.where(t < 0.8, -231.0, -231 + 300 * (t - 0.8) - 258 * (t - 0.8) ** 2)
print(json.dumps([m.demangle(".?AVFruit@@"), m.demangle(".?AVEntity@Mortar@@"),
  m.classify(t, 3 + 7 * t), m.classify(t, quad), m.classify(t, np.full(41, 2.0)), m.classify(t, reset), m.classify(t, waits)]))`),
    );
    assert.equal(out[0], "Fruit");
    assert.equal(out[1], "Mortar::Entity");
    assert.equal(out[2][0], "linear");
    assert.ok(Math.abs(out[2][1] - 7) < 1e-6);
    assert.equal(out[3][0], "quadratic");
    assert.ok(Math.abs(out[3][1] + 720) < 1e-3);
    assert.equal(out[4][0], "constant");
    assert.equal(out[5][0], "linear");
    assert.equal(out[6][0], "quadratic");
    assert.ok(Math.abs(out[6][1] + 516) < 1);
  },
);

test(
  "watch finds a linear and an accelerating field in a live process, unfiltered by sign",
  { skip: !hasNumpy || !existsSync("/proc/self/mem") },
  async () => {
    const child = spawn("python3", [
      "-c",
      `import ctypes, sys, time
a = (ctypes.c_float * 4)()
print(ctypes.addressof(a), flush=True)
t0 = time.monotonic()
while time.monotonic() - t0 < 10:
    t = time.monotonic() - t0
    a[0] = -50 + 30 * t; a[1] = 20 - 360 * t * t; a[2] = 1.5; a[3] = -t
    time.sleep(0.002)`,
    ]);
    try {
      const address = await new Promise<string>((resolve) =>
        child.stdout.once("data", (d) => resolve(String(d).trim())),
      );
      let out: string;
      try {
        out = execFileSync(
          "python3",
          [
            "sandbox/re-tools/memtool",
            "--pid",
            String(child.pid),
            "watch",
            `0x${Number(address).toString(16)}`,
            "--len",
            "16",
            "--seconds",
            "1",
          ],
          { env, stdio: ["ignore", "pipe", "pipe"] },
        ).toString();
      } catch (error: any) {
        if (
          /Permission denied|Operation not permitted/.test(String(error.stderr))
        )
          return; // ptrace policy
        throw error;
      }
      const row = (offset: string) =>
        out.split("\n").find((l) => l.startsWith(offset)) ?? "";
      assert.match(row("+0x0000"), /velocity/);
      assert.match(row("+0x0004"), /acceleration/);
      assert.match(row("+0x0004"), /\[-\d/); // negative values are reported, not filtered
      assert.equal(row("+0x0008"), "");
      assert.match(row("+0x000c"), /velocity/);
    } finally {
      child.kill();
    }
  },
);
